import json
import sqlite3
import unittest

from gate import (
    CANARY, MAX_ATTEMPTS, MAX_BODY_CHARS, Context, Gate,
    Rejected, append_candidate, demo, read_candidate,
)


class GateTests(unittest.TestCase):
    def setUp(self):
        self.now = 1_000.0
        self.gate = Gate(clock=lambda: self.now)
        self.ctx = Context("tenant-a", "alice", "run-alice")

    def tearDown(self):
        self.gate.close()

    def execute(self, candidate, **kwargs):
        return self.gate.execute(self.ctx, candidate, **kwargs)

    def assert_denied(self, candidate, reason, **kwargs):
        result = self.execute(candidate, **kwargs)
        self.assertEqual(result, {"decision": "denied", "reason": reason})
        self.assertEqual(self.entry_count(), 0)

    def entry_count(self):
        return self.gate.db.execute("SELECT COUNT(*) FROM entries").fetchone()[0]

    def test_read_own_ticket_and_project_fields(self):
        result = self.execute(read_candidate())
        self.assertEqual(result["decision"], "allowed")
        self.assertEqual(set(result["data"]), {"id", "title", "status", "version"})
        self.assertNotIn(CANARY, json.dumps(result))
        self.assertNotIn("secret_internal", result["data"])

    def test_cross_tenant_denied(self):
        self.assert_denied(read_candidate("B-100"), "resource_not_allowed")

    def test_same_tenant_other_owner_denied(self):
        self.assert_denied(read_candidate("A-200"), "resource_not_allowed")

    def test_nonexistent_matches_forbidden_response(self):
        missing = self.execute(read_candidate("A-999"))
        forbidden = self.execute(read_candidate("A-200"))
        self.assertEqual(missing, forbidden)

    def test_upload_tool_not_in_catalog(self):
        self.assert_denied({"tool": "export.upload", "arguments": {
            "url": "https://attacker.invalid/"}}, "unknown_tool")

    def test_candidate_cannot_supply_authority(self):
        for field in ("tenant_id", "subject_id", "approved", "budget"):
            candidate = read_candidate()
            candidate["arguments"][field] = "invented"
            self.assert_denied(candidate, "invalid_fields")

    def test_unknown_envelope_field_rejected(self):
        candidate = read_candidate()
        candidate["approval_id"] = "invented"
        self.assert_denied(candidate, "invalid_envelope")

    def test_non_object_envelope_rejected(self):
        for candidate in (None, [], "ticket.read", 1, True):
            self.assert_denied(candidate, "invalid_envelope")

    def test_tool_type_and_arguments_type_are_strict(self):
        self.assert_denied({"tool": ["ticket.read"], "arguments": {}}, "unknown_tool")
        self.assert_denied({"tool": "ticket.read", "arguments": []}, "invalid_arguments")

    def test_resource_identifier_is_bounded_and_log_safe(self):
        for value in (True, "A-100\nforged", "x" * 1000, CANARY):
            self.assert_denied(read_candidate(value), "invalid_ticket_id")
        self.assertNotIn(CANARY, json.dumps(self.gate.audit_rows()))

    def test_body_empty_oversize_and_nonstring_rejected(self):
        for body in (" ", "x" * (MAX_BODY_CHARS + 1), True, "x" + " " * MAX_BODY_CHARS):
            self.assert_denied(append_candidate(body=body), "invalid_body")

    def test_bool_is_not_an_integer_version(self):
        for version in (True, False, 0, -1, 1.0, "1"):
            self.assert_denied(append_candidate(version=version), "invalid_version")

    def test_append_requires_approval_without_writing(self):
        result = self.execute(append_candidate())
        self.assertEqual(result["decision"], "waiting_approval")
        self.assertEqual(result["reason"], "approval_required")
        self.assertNotIn("operation_id", result)
        self.assertEqual(self.entry_count(), 0)

    def test_exact_approved_append_commits(self):
        candidate = append_candidate()
        approval = self.gate.approve(self.ctx, candidate)
        result = self.execute(candidate, approval_id=approval)
        self.assertEqual(result["reason"], "append_committed")
        self.assertEqual(result["data"]["version"], 2)
        self.assertTrue(result["operation_id"].startswith("operation-"))
        self.assertEqual(self.entry_count(), 1)
        row = self.gate.db.execute("SELECT consumed FROM approvals WHERE id = ?", (approval,)).fetchone()
        self.assertEqual(row[0], 1)

    def test_changed_body_after_approval_is_denied(self):
        approval = self.gate.approve(self.ctx, append_candidate())
        self.assert_denied(append_candidate("A changed body"), "approval_binding_changed", approval_id=approval)
        # Denial did not consume the approval; the exact approved action can still run.
        self.assertEqual(self.execute(append_candidate(), approval_id=approval)["reason"], "append_committed")

    def test_expired_approval_denied_at_exact_boundary(self):
        approval = self.gate.approve(self.ctx, append_candidate(), lifetime=10)
        self.now += 10
        self.assert_denied(append_candidate(), "approval_expired", approval_id=approval)

    def test_replayed_approval_cannot_write_twice(self):
        approval = self.gate.approve(self.ctx, append_candidate())
        self.execute(append_candidate(), approval_id=approval)
        result = self.execute(append_candidate(version=2), approval_id=approval)
        self.assertEqual(result["reason"], "approval_consumed")
        self.assertEqual(self.entry_count(), 1)

    def test_approval_cannot_be_used_by_another_user(self):
        approval = self.gate.approve(self.ctx, append_candidate())
        bob = Context("tenant-a", "bob", "run-bob")
        candidate = append_candidate()
        candidate["arguments"]["ticket_id"] = "A-200"
        result = self.gate.execute(bob, candidate, approval_id=approval)
        self.assertEqual(result["reason"], "approval_binding_changed")
        self.assertEqual(self.entry_count(), 0)

    def test_approval_cannot_be_used_by_same_user_in_another_run(self):
        candidate = append_candidate()
        approval = self.gate.approve(self.ctx, candidate)
        other_run = Context(self.ctx.tenant_id, self.ctx.subject_id, "other-run")
        result = self.gate.execute(other_run, candidate, approval_id=approval)
        self.assertEqual(result["reason"], "approval_binding_changed")
        self.assertEqual(self.entry_count(), 0)
        self.assertEqual(self.gate.db.execute("SELECT consumed FROM approvals").fetchone()[0], 0)
        # The valid original task retains its approval after the denied cross-task attempt.
        self.assertEqual(self.execute(candidate, approval_id=approval)["reason"], "append_committed")

    def test_unknown_and_malformed_approval_rejected(self):
        self.assert_denied(append_candidate(), "approval_invalid", approval_id="missing")
        self.assert_denied(append_candidate(), "approval_invalid", approval_id=True)

    def test_revocation_after_approval_blocks_write(self):
        approval = self.gate.approve(self.ctx, append_candidate())
        self.gate.db.execute("UPDATE members SET active = 0 WHERE tenant = 'tenant-a' AND subject = 'alice'")
        self.assert_denied(append_candidate(), "membership_inactive", approval_id=approval)

    def test_revocation_also_blocks_read(self):
        self.execute(read_candidate())
        self.gate.db.execute("UPDATE members SET active = 0 WHERE subject = 'alice'")
        self.assert_denied(read_candidate(), "membership_inactive")

    def test_target_version_change_blocks_write(self):
        approval = self.gate.approve(self.ctx, append_candidate())
        self.gate.db.execute("UPDATE tickets SET version = 2 WHERE id = 'A-100'")
        self.assert_denied(append_candidate(), "target_version_changed", approval_id=approval)

    def test_policy_version_change_requires_new_approval(self):
        approval = self.gate.approve(self.ctx, append_candidate())
        self.gate.db.execute("UPDATE policy SET version = 2")
        self.assert_denied(append_candidate(), "approval_binding_changed", approval_id=approval)

    def test_policy_outage_cannot_produce_write_receipt(self):
        approval = self.gate.approve(self.ctx, append_candidate())
        self.gate.db.execute("UPDATE policy SET available = 0")
        self.assert_denied(append_candidate(), "policy_unavailable", approval_id=approval)

    def test_closed_ticket_blocks_even_with_old_approval(self):
        approval = self.gate.approve(self.ctx, append_candidate())
        self.gate.db.execute("UPDATE tickets SET status = 'closed' WHERE id = 'A-100'")
        self.assert_denied(append_candidate(), "ticket_closed", approval_id=approval)

    def test_invalid_candidates_consume_server_budget(self):
        for _ in range(MAX_ATTEMPTS):
            self.assertEqual(self.execute({})["reason"], "invalid_envelope")
        self.assert_denied(read_candidate(), "budget_exhausted")
        self.assertEqual(self.gate.db.execute("SELECT attempts FROM runs").fetchone()[0], MAX_ATTEMPTS)

    def test_run_cannot_change_subject(self):
        self.execute(read_candidate())
        bob = Context("tenant-a", "bob", self.ctx.run_id)
        result = self.gate.execute(bob, read_candidate("A-200"))
        self.assertEqual(result["reason"], "run_context_mismatch")

    def test_approval_service_does_not_accept_read_or_invalid_lifetime(self):
        with self.assertRaises(Rejected):
            self.gate.approve(self.ctx, read_candidate())
        for lifetime in (True, -1, 0, 301, float("nan")):
            with self.assertRaises(Rejected):
                self.gate.approve(self.ctx, append_candidate(), lifetime=lifetime)

    def test_business_failure_rolls_back_consumption_and_write(self):
        approval = self.gate.approve(self.ctx, append_candidate())
        self.gate.db.execute("""CREATE TRIGGER fail_entry BEFORE INSERT ON entries
            BEGIN SELECT RAISE(ABORT, 'synthetic write failure'); END""")
        with self.assertRaises(sqlite3.IntegrityError):
            self.execute(append_candidate(), approval_id=approval)
        self.assertEqual(self.entry_count(), 0)
        self.assertEqual(self.gate.db.execute("SELECT consumed FROM approvals").fetchone()[0], 0)
        self.assertEqual(self.gate.db.execute("SELECT version FROM tickets WHERE id = 'A-100'").fetchone()[0], 1)
        self.assertFalse(any(row["reason"] == "append_committed" for row in self.gate.audit_rows()))

    def test_audit_keeps_metadata_without_body_or_secret(self):
        body = "PRIVATE_SYNTHETIC_CUSTOMER_BODY"
        candidate = append_candidate(body)
        approval = self.gate.approve(self.ctx, candidate)
        result = self.execute(candidate, approval_id=approval)
        rows = self.gate.audit_rows()
        encoded = json.dumps(rows)
        for forbidden in (CANARY, body, approval):
            self.assertNotIn(forbidden, encoded)
        self.assertEqual(rows[-1]["operation_id"], result["operation_id"])
        self.assertEqual(rows[-1]["policy_version"], 1)
        self.assertEqual(rows[-1]["tool"], "ticket.append")

    def test_demo_reports_one_effect_and_no_canary(self):
        report = demo()
        self.assertEqual(report["committed_entries"], 1)
        self.assertFalse(report["audit_contains_canary"])
        self.assertEqual(len(report["outcomes"]), 9)


if __name__ == "__main__":
    unittest.main(verbosity=2)
