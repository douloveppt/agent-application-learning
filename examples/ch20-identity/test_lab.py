from dataclasses import replace
import json
import sqlite3
import unittest

from lab import IdentityLab, Rejected, SCENARIOS, ToolCall, scenario


class IdentityTests(unittest.TestCase):
    def setUp(self):
        self.clock = [1000.0]
        self.lab = IdentityLab(now=lambda: self.clock[0])
        self.addCleanup(self.lab.close)
        self.ctx = self.lab.authenticate("session-alice-a")
        self.call = ToolCall("ticket-own", "Synthetic resolution: no real customer data")

    def denied(self, reason, fn, *args):
        with self.assertRaisesRegex(Rejected, f"^{reason}$"):
            fn(*args)

    def approval(self):
        return self.lab.approve(self.ctx, self.call)

    def assert_unwritten(self):
        row = self.lab.db.execute("SELECT * FROM tickets WHERE tenant = 'tenant-a' AND id = 'ticket-own'").fetchone()
        self.assertEqual((row["status"], row["version"]), ("open", 1))
        self.assertEqual(self.lab.db.execute("SELECT count(*) FROM receipts").fetchone()[0], 0)

    def test_server_session_ignores_model_actor_tenant_and_role(self):
        ctx = self.lab.authenticate("session-alice-a", {"tenant_id": "tenant-b", "actor_id": "carol", "role": "tenant_admin"})
        self.assertEqual((ctx.tenant_id, ctx.actor_id, ctx.role_snapshot), ("tenant-a", "alice", "employee"))

    def test_edited_context_is_not_an_identity_credential(self):
        for ctx in (replace(self.ctx, tenant_id="tenant-b"), replace(self.ctx, actor_id="bob")):
            self.denied("identity_unavailable", self.lab.read_ticket, ctx, "ticket-own")
            event = self.lab.audit_events()[-1]
            self.assertIsNone(event["tenant"])
            self.assertIsNone(event["actor"])
            self.assertNotEqual(event["request_id"], ctx.request_id)

    def test_cross_tenant_resource_id_is_not_visible(self):
        self.denied("resource_unavailable", self.lab.read_ticket, self.ctx, "ticket-carol")
        carol = self.lab.authenticate("session-carol-b")
        self.assertEqual(self.lab.read_ticket(carol, "ticket-carol")["owner"], "carol")

    def test_same_tenant_does_not_grant_other_owner_access(self):
        self.denied("resource_unavailable", self.lab.read_ticket, self.ctx, "ticket-bob")
        bob = self.lab.authenticate("session-bob-a")
        self.assertEqual(self.lab.read_ticket(bob, "ticket-bob")["owner"], "bob")

    def test_tenant_admin_control_role_does_not_grant_other_owner_access(self):
        self.lab.change_membership("tenant-a", "alice", role="tenant_admin")
        self.denied("resource_unavailable", self.lab.read_ticket, self.ctx, "ticket-bob")
        other_call = replace(self.call, resource_id="ticket-bob")
        self.denied("resource_unavailable", self.lab.approve, self.ctx, other_call)
        self.denied("resource_unavailable", self.lab.execute_close, self.ctx, other_call, "invented", "op-1")
        self.assert_unwritten()

    def test_tenant_admin_control_role_does_not_imply_own_ticket_write_permission(self):
        approval = self.approval()
        self.lab.change_membership("tenant-a", "alice", role="tenant_admin")
        self.assertEqual(self.lab.read_ticket(self.ctx, "ticket-own")["owner"], "alice")
        self.denied("action_denied", self.lab.approve, self.ctx, self.call)
        self.denied("action_denied", self.lab.queue, self.ctx, self.call, approval, "op-1")
        self.denied("action_denied", self.lab.execute_close, self.ctx, self.call, approval, "op-1")
        self.assert_unwritten()

    def test_same_local_id_is_resolved_inside_active_tenant(self):
        other = self.lab.authenticate("session-alice-b")
        self.assertEqual(self.lab.read_ticket(self.ctx, "ticket-own")["tenant"], "tenant-a")
        self.assertEqual(self.lab.read_ticket(other, "ticket-own")["tenant"], "tenant-b")

    def test_current_membership_blocks_queued_job_after_revocation(self):
        job = self.lab.queue(self.ctx, self.call, self.approval(), "op-1")
        self.lab.change_membership("tenant-a", "alice", active=False)
        self.denied("membership_revoked", self.lab.run_job, job)
        self.assert_unwritten()
        self.assertEqual(self.lab.audit_events()[-1]["decision"], "denied")

    def test_role_snapshot_cannot_preserve_old_write_authority(self):
        job = self.lab.queue(self.ctx, self.call, self.approval(), "op-1")
        self.lab.change_membership("tenant-a", "alice", role="viewer")
        self.assertEqual(self.ctx.role_snapshot, "employee")
        self.denied("action_denied", self.lab.run_job, job)
        self.assert_unwritten()
        self.assertEqual(self.lab.read_ticket(self.ctx, "ticket-own")["status"], "open")

    def test_revoked_session_blocks_previously_queued_job(self):
        job = self.lab.queue(self.ctx, self.call, self.approval(), "op-1")
        self.lab.db.execute("UPDATE sessions SET active = 0 WHERE id = ?", (self.ctx.session_id,))
        self.denied("identity_unavailable", self.lab.run_job, job)
        self.assert_unwritten()

    def test_forged_approval_never_creates_a_receipt(self):
        self.denied("approval_unavailable", self.lab.execute_close, self.ctx, self.call, "model-said-approved", "op-1")
        self.assert_unwritten()

    def assert_no_jobs(self):
        self.assertEqual(self.lab.db.execute("SELECT count(*) FROM jobs").fetchone()[0], 0)

    def test_queue_admission_validates_without_consuming_approval(self):
        approval = self.approval()
        job = self.lab.queue(self.ctx, self.call, approval, "op-1")
        self.assertIsNone(self.lab.db.execute("SELECT consumed_at FROM approvals WHERE id = ?", (approval,)).fetchone()[0])
        self.assert_unwritten()
        self.assertEqual(self.lab.run_job(job)["status"], "closed")

    def test_queue_rejects_forged_approval_without_persisting_a_job(self):
        self.denied("approval_unavailable", self.lab.queue, self.ctx, self.call, "invented", "op-1")
        self.assert_no_jobs()
        self.assert_unwritten()

    def test_queue_rejects_cross_tenant_actor_session_and_resource_bindings(self):
        approval = self.approval()
        self.lab.db.execute("INSERT INTO sessions(id, tenant, actor) VALUES ('session-alice-extra', 'tenant-a', 'alice')")
        self.lab.db.execute("INSERT INTO tickets(tenant, id, owner) VALUES ('tenant-a', 'ticket-extra', 'alice')")
        cases = (
            (self.lab.authenticate("session-alice-b"), self.call),
            (self.lab.authenticate("session-bob-a"), replace(self.call, resource_id="ticket-bob")),
            (self.lab.authenticate("session-alice-extra"), self.call),
            (self.ctx, replace(self.call, resource_id="ticket-extra")),
        )
        for ctx, call in cases:
            with self.subTest(tenant=ctx.tenant_id, actor=ctx.actor_id, resource=call.resource_id):
                self.denied("approval_scope_mismatch", self.lab.queue, ctx, call, approval, "op-1")
                self.assert_no_jobs()
        self.assert_unwritten()

    def test_queue_rejects_changed_arguments_without_persisting_a_job(self):
        approval = self.approval()
        self.denied("approval_scope_mismatch", self.lab.queue, self.ctx, replace(self.call, resolution="changed"), approval, "op-1")
        self.assert_no_jobs()
        self.assert_unwritten()

    def test_queue_rejects_expired_approval_without_persisting_a_job(self):
        approval = self.approval()
        self.clock[0] += 60
        self.denied("approval_expired", self.lab.queue, self.ctx, self.call, approval, "op-1")
        self.assert_no_jobs()
        self.assert_unwritten()

    def test_queue_rejects_stale_membership_version_without_persisting_a_job(self):
        approval = self.approval()
        self.lab.change_membership("tenant-a", "alice", role="employee")
        self.denied("approval_stale_membership", self.lab.queue, self.ctx, self.call, approval, "op-1")
        self.assert_no_jobs()
        self.assert_unwritten()

    def test_queue_rejects_stale_policy_version_without_persisting_a_job(self):
        approval = self.approval()
        self.lab.change_policy("tenant-a")
        self.denied("approval_stale_policy", self.lab.queue, self.ctx, self.call, approval, "op-1")
        self.assert_no_jobs()
        self.assert_unwritten()

    def test_queue_rejects_stale_resource_version_without_persisting_a_job(self):
        approval = self.approval()
        self.lab.db.execute("UPDATE tickets SET version = version + 1 WHERE tenant = 'tenant-a' AND id = 'ticket-own'")
        self.denied("approval_stale_resource", self.lab.queue, self.ctx, self.call, approval, "op-1")
        self.assert_no_jobs()
        self.assertEqual(self.lab.db.execute("SELECT count(*) FROM receipts").fetchone()[0], 0)

    def test_queue_rejects_consumed_approval_without_persisting_a_job(self):
        approval = self.approval()
        self.lab.execute_close(self.ctx, self.call, approval, "op-1")
        self.denied("approval_consumed", self.lab.queue, self.ctx, self.call, approval, "op-2")
        self.assert_no_jobs()
        self.assertEqual(self.lab.db.execute("SELECT count(*) FROM receipts").fetchone()[0], 1)

    def test_queue_rejects_invalid_operation_key_without_persisting_a_job(self):
        self.denied("invalid_operation_key", self.lab.queue, self.ctx, self.call, self.approval(), "")
        self.assert_no_jobs()
        self.assert_unwritten()

    def test_approval_binds_actor_tenant_and_session(self):
        approval = self.approval()
        other = self.lab.authenticate("session-alice-b")
        self.denied("approval_scope_mismatch", self.lab.execute_close, other, self.call, approval, "op-1")
        bob = self.lab.authenticate("session-bob-a")
        self.denied("approval_scope_mismatch", self.lab.execute_close, bob, ToolCall("ticket-bob", self.call.resolution), approval, "op-1")
        self.assert_unwritten()

    def test_changed_arguments_require_new_approval(self):
        approval = self.approval()
        changed = replace(self.call, resolution="Changed after approval")
        self.denied("approval_scope_mismatch", self.lab.execute_close, self.ctx, changed, approval, "op-1")
        self.assert_unwritten()
        # A failed attempt does not consume the original valid approval.
        self.assertEqual(self.lab.execute_close(self.ctx, self.call, approval, "op-1")["status"], "closed")

    def test_resource_version_change_invalidates_approval(self):
        approval = self.approval()
        self.lab.db.execute("UPDATE tickets SET version = version + 1 WHERE tenant = 'tenant-a' AND id = 'ticket-own'")
        self.denied("approval_stale_resource", self.lab.execute_close, self.ctx, self.call, approval, "op-1")
        self.assertEqual(self.lab.db.execute("SELECT count(*) FROM receipts").fetchone()[0], 0)

    def test_membership_revision_change_requires_new_approval(self):
        approval = self.approval()
        self.lab.change_membership("tenant-a", "alice", role="employee")
        self.denied("approval_stale_membership", self.lab.execute_close, self.ctx, self.call, approval, "op-1")
        self.assert_unwritten()

    def test_current_policy_can_disable_writes(self):
        approval = self.approval()
        self.lab.change_policy("tenant-a", writes_enabled=False)
        self.denied("action_denied", self.lab.execute_close, self.ctx, self.call, approval, "op-1")
        self.assert_unwritten()

    def test_policy_revision_change_requires_new_approval(self):
        approval = self.approval()
        self.lab.change_policy("tenant-a")
        self.denied("approval_stale_policy", self.lab.execute_close, self.ctx, self.call, approval, "op-1")
        self.assert_unwritten()

    def test_approval_expires_before_execution(self):
        approval = self.approval()
        self.clock[0] += 60
        self.denied("approval_expired", self.lab.execute_close, self.ctx, self.call, approval, "op-1")
        self.assert_unwritten()

    def test_invalid_tool_fields_are_rejected_before_dispatch(self):
        for call, reason in ((replace(self.call, resource_id=123), "invalid_resource_id"),
                             (replace(self.call, resolution=""), "invalid_resolution"),
                             (replace(self.call, resolution="x" * 201), "invalid_resolution"),
                             (replace(self.call, action="tenant.export"), "unsupported_action")):
            with self.subTest(reason=reason):
                self.denied(reason, self.lab.approve, self.ctx, call)
        self.assert_unwritten()

    def test_storage_failure_rolls_back_approval_consumption_and_business_write(self):
        approval = self.approval()
        self.lab.db.executescript("""
            CREATE TEMP TRIGGER simulate_storage_failure BEFORE UPDATE ON tickets
            BEGIN SELECT RAISE(ABORT, 'synthetic storage failure'); END;
        """)
        with self.assertRaisesRegex(sqlite3.IntegrityError, "synthetic storage failure"):
            self.lab.execute_close(self.ctx, self.call, approval, "op-1")
        self.assert_unwritten()
        self.assertIsNone(self.lab.db.execute("SELECT consumed_at FROM approvals WHERE id = ?", (approval,)).fetchone()[0])
        self.assertFalse(any(event["reason"] == "committed" for event in self.lab.audit_events()))

    def test_single_use_approval_cannot_authorize_another_operation(self):
        approval = self.approval()
        self.lab.execute_close(self.ctx, self.call, approval, "op-1")
        self.denied("approval_consumed", self.lab.execute_close, self.ctx, self.call, approval, "op-2")
        self.assertEqual(self.lab.db.execute("SELECT count(*) FROM receipts").fetchone()[0], 1)

    def test_same_operation_retrieves_receipt_without_second_write(self):
        approval = self.approval()
        first = self.lab.execute_close(self.ctx, self.call, approval, "op-1")
        # Expired/consumed approval does not trigger a second action for an already committed key.
        self.clock[0] += 100
        second = self.lab.execute_close(self.ctx, self.call, approval, "op-1")
        self.assertEqual(first["receipt_id"], second["receipt_id"])
        self.assertTrue(second["replayed_receipt"])
        self.assertEqual(second["version"], 2)
        self.assertEqual(self.lab.db.execute("SELECT count(*) FROM receipts").fetchone()[0], 1)

    def test_same_key_with_changed_contract_is_rejected(self):
        approval = self.approval()
        self.lab.execute_close(self.ctx, self.call, approval, "op-1")
        self.denied("idempotency_conflict", self.lab.execute_close, self.ctx, replace(self.call, resolution="changed"), approval, "op-1")

    def test_same_key_is_independent_between_tenants(self):
        first = self.lab.execute_close(self.ctx, self.call, self.approval(), "same-key")
        other = self.lab.authenticate("session-alice-b")
        second = self.lab.execute_close(other, self.call, self.lab.approve(other, self.call), "same-key")
        self.assertNotEqual(first["receipt_id"], second["receipt_id"])
        self.assertEqual(self.lab.db.execute("SELECT count(*) FROM receipts").fetchone()[0], 2)

    def test_receipt_replay_still_rechecks_current_authority(self):
        approval = self.approval()
        self.lab.execute_close(self.ctx, self.call, approval, "op-1")
        self.lab.change_membership("tenant-a", "alice", active=False)
        self.denied("membership_revoked", self.lab.execute_close, self.ctx, self.call, approval, "op-1")

    def test_retrieval_filters_before_returning_bodies(self):
        rows = self.lab.retrieve(self.ctx, "VPN")
        self.assertEqual({row["id"] for row in rows}, {"doc-team", "doc-alice"})
        joined = json.dumps(rows)
        self.assertNotIn("Bob private", joined)
        self.assertNotIn("tenant-b", joined)

    def test_memory_has_both_tenant_and_actor_scope(self):
        self.assertEqual(self.lab.read_memory(self.ctx, "memory-self"), "Alice in tenant-a")
        self.denied("resource_unavailable", self.lab.read_memory, self.ctx, "memory-bob")
        other = self.lab.authenticate("session-alice-b")
        self.assertEqual(self.lab.read_memory(other, "memory-self"), "Alice in tenant-b")

    def test_cached_body_is_not_returned_after_acl_revocation(self):
        self.lab.cached_document(self.ctx, "doc-team")
        self.lab.cached_document(self.ctx, "doc-team")
        self.assertEqual(self.lab.cache_hits, 1)
        self.lab.privatize_document("tenant-a", "doc-team")
        self.denied("resource_unavailable", self.lab.cached_document, self.ctx, "doc-team")
        self.assertEqual(self.lab.cache_hits, 1)

    def test_document_cache_is_scoped_by_tenant_and_actor(self):
        a_body = self.lab.cached_document(self.ctx, "doc-team")
        other = self.lab.authenticate("session-alice-b")
        b_body = self.lab.cached_document(other, "doc-team")
        self.assertIn("tenant-a", a_body)
        self.assertIn("tenant-b", b_body)
        self.assertEqual(len(self.lab.cache), 2)

    def test_audit_excludes_content_and_credential_handles(self):
        secret_text = "synthetic-secret-123"
        call = replace(self.call, resolution=secret_text)
        approval = self.lab.approve(self.ctx, call)
        self.lab.execute_close(self.ctx, call, approval, "op-1")
        events = self.lab.audit_events()
        serialized = json.dumps(events)
        for forbidden in (secret_text, self.ctx.session_id, approval):
            self.assertNotIn(forbidden, serialized)
        self.assertEqual(events[-1]["reason"], "committed")
        self.assertEqual(set(events[-1]), {"sequence", "at", "request_id", "tenant", "actor", "action", "resource", "decision", "reason"})

    def test_every_documented_demo_scenario_has_expected_outcome(self):
        allowed = {"success": "closed", "tenant_spoof": "server_scope_preserved",
                   "retrieval_filter": "scoped", "idempotency_scope": "two_scoped_receipts"}
        for name in SCENARIOS:
            with self.subTest(name=name):
                result = scenario(name)
                self.assertEqual(result["status"], allowed.get(name, "denied"))
                if result["status"] == "denied" and name != "approval_replay":
                    self.assertEqual(result["receipt_count"], 0)


if __name__ == "__main__":
    unittest.main()
