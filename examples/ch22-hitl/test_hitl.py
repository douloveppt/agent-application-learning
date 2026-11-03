import concurrent.futures
import tempfile
import threading
import unittest
from pathlib import Path

from hitl import (ALICE, BOB, CAROL, ApprovalService, Context, Refused,
                  TicketSystem, approved, connect, pending, transaction)


class ApprovalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ch22-test-")
        self.root = Path(self.tmp.name)
        self.now = 1000.0
        self.provider = TicketSystem(self.root / "external.sqlite")
        self.service = ApprovalService(self.root / "approval.sqlite", self.provider, lambda: self.now)

    def tearDown(self):
        self.tmp.cleanup()

    def decide(self, row, decision="approve", decision_id="decision-1", ctx=ALICE):
        return self.service.decide(row["approval_id"], row["revision"], row["version"],
                                   decision_id, decision, ctx)

    def execute(self, row, **kwargs):
        return self.service.execute(ALICE, row["approval_id"], row["revision"], **kwargs)

    def assert_no_write(self):
        self.assertEqual(self.provider.counts(), {"external_calls": 0, "committed_entries": 0})

    def test_approval_is_not_business_completion(self):
        row = self.decide(pending(self.service))
        self.assertEqual(row["state"], "approved")
        self.assert_no_write()
        result = self.execute(row)
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(self.provider.counts(), {"external_calls": 1, "committed_entries": 1})

    def test_draft_cannot_be_approved_or_executed(self):
        row = self.service.create(ALICE, "draft")
        with self.assertRaisesRegex(Refused, "not_pending"):
            self.decide(row)
        with self.assertRaisesRegex(Refused, "not_approved"):
            self.execute(row)
        self.assert_no_write()

    def test_rejection_blocks_write(self):
        row = self.decide(pending(self.service), "reject")
        with self.assertRaisesRegex(Refused, "not_approved"):
            self.execute(row)
        self.assertEqual(row["state"], "rejected")
        self.assert_no_write()

    def test_edit_creates_revision_and_invalidates_old_tab(self):
        old = pending(self.service)
        new = self.service.edit(ALICE, old["approval_id"], 1, old["version"], "changed")
        self.assertEqual(new["revision"], 2)
        self.assertEqual(new["state"], "draft")
        self.assertEqual(self.service.view(ALICE, old["approval_id"], 1)["state"], "superseded")
        with self.assertRaisesRegex(Refused, "stale_or_not_pending"):
            self.decide(old)
        self.assert_no_write()

    def test_approved_revision_edit_requires_new_approval(self):
        old = self.decide(pending(self.service))
        new = self.service.edit(ALICE, old["approval_id"], 1, old["version"], "new content")
        with self.assertRaisesRegex(Refused, "not_approved"):
            self.execute(old)
        new = self.service.submit(ALICE, new["approval_id"], 2, new["version"])
        new = self.decide(new, decision_id="new-decision")
        self.execute(new)
        db = connect(self.provider.path)
        try:
            self.assertEqual(db.execute("SELECT body FROM entries").fetchone()[0], "new content")
        finally:
            db.close()

    def test_identical_decision_replays_original_response(self):
        pending_row = pending(self.service)
        result = self.decide(pending_row)
        self.execute(result)
        self.assertEqual(self.decide(pending_row), result)

    def test_decision_id_cannot_change_meaning(self):
        row = pending(self.service)
        self.decide(row)
        with self.assertRaisesRegex(Refused, "decision_id_conflict"):
            self.decide(row, "reject")

    def test_decision_id_is_scoped_to_tenant(self):
        alice_pending = pending(self.service)
        alice_result = self.decide(alice_pending, decision_id="shared-client-id")
        with transaction(self.provider.path) as db:
            db.execute("UPDATE tickets SET tenant='tenant-b',owner='carol' WHERE id='A-100'")
        carol_draft = self.service.create(CAROL, "Carol's authorized explanation")
        carol_pending = self.service.submit(CAROL, carol_draft["approval_id"], 1, carol_draft["version"])
        carol_result = self.decide(carol_pending, decision_id="shared-client-id", ctx=CAROL)
        self.assertEqual(carol_result["state"], "approved")
        self.assertNotEqual(alice_result["approval_id"], carol_result["approval_id"])
        with transaction(self.provider.path) as db:
            db.execute("UPDATE tickets SET tenant='tenant-a',owner='alice' WHERE id='A-100'")
        self.assertEqual(self.decide(alice_pending, decision_id="shared-client-id"), alice_result)
        self.assert_no_write()

    def test_second_distinct_decision_cannot_overwrite_first(self):
        row = pending(self.service)
        self.decide(row, "reject")
        with self.assertRaisesRegex(Refused, "stale_or_not_pending"):
            self.decide(row, decision_id="decision-2")
        self.assert_no_write()

    def test_stale_row_version_fails(self):
        row = pending(self.service)
        row["version"] -= 1
        with self.assertRaisesRegex(Refused, "stale_or_not_pending"):
            self.decide(row)

    def test_other_user_and_tenant_cannot_approve(self):
        row = pending(self.service)
        for ctx in (BOB, CAROL):
            with self.subTest(ctx=ctx), self.assertRaisesRegex(Refused, "not_allowed"):
                self.decide(row, ctx=ctx)
        self.assert_no_write()

    def test_same_user_other_run_cannot_approve(self):
        row = pending(self.service)
        with self.assertRaisesRegex(Refused, "context_mismatch"):
            self.decide(row, ctx=Context("tenant-a", "alice", "run-2"))

    def test_expiry_never_auto_approves(self):
        row = pending(self.service)
        self.now += 601
        result = self.decide(row)
        self.assertEqual(result["state"], "expired")
        with self.assertRaisesRegex(Refused, "not_approved"):
            self.execute(row)
        self.assert_no_write()

    def test_expiry_after_approval_blocks_dispatch(self):
        row = approved(self.service)
        self.now += 601
        self.assertEqual(self.execute(row), {"status": "expired"})
        self.assertEqual(self.service.view(ALICE, row["approval_id"], 1)["state"], "expired")
        self.assert_no_write()

    def test_membership_revocation_blocks_dispatch(self):
        row = approved(self.service)
        self.service.revoke()
        with self.assertRaisesRegex(Refused, "not_allowed"):
            self.execute(row)
        self.assert_no_write()

    def test_resource_version_change_blocks_dispatch(self):
        row = approved(self.service)
        self.provider.change_version()
        with self.assertRaisesRegex(Refused, "resource_changed"):
            self.execute(row)
        self.assert_no_write()

    def test_policy_version_change_blocks_dispatch(self):
        row = approved(self.service)
        self.service.change_policy()
        with self.assertRaisesRegex(Refused, "policy_changed"):
            self.execute(row)
        self.assert_no_write()

    def test_cancel_before_dispatch_blocks_write(self):
        row = approved(self.service)
        result = self.service.cancel(ALICE, row["approval_id"], 1, row["version"])
        self.assertEqual(result["state"], "cancelled")
        with self.assertRaisesRegex(Refused, "not_approved"):
            self.execute(row)
        self.assert_no_write()

    def test_cancel_cannot_claim_recall_after_dispatch(self):
        row = approved(self.service)
        self.execute(row, fault="lost_receipt")
        current = self.service.view(ALICE, row["approval_id"], 1)
        with self.assertRaisesRegex(Refused, "too_late_or_stale"):
            self.service.cancel(ALICE, row["approval_id"], 1, current["version"])

    def test_repeat_execution_returns_original_receipt_without_dispatch(self):
        row = approved(self.service)
        first = self.execute(row)
        self.assertEqual(self.execute(row), first)
        self.assertEqual(self.provider.counts(), {"external_calls": 1, "committed_entries": 1})

    def test_receipt_loss_restart_reconciles_without_resend(self):
        row = approved(self.service)
        lost = self.execute(row, fault="lost_receipt")
        self.assertEqual(lost["status"], "unknown")
        self.service = ApprovalService(self.service.path, TicketSystem(self.provider.path), lambda: self.now)
        self.assertEqual(self.execute(row)["status"], "unknown")
        recovered = self.service.reconcile(ALICE, row["approval_id"], 1)
        self.assertEqual(recovered["status"], "succeeded")
        self.assertEqual(recovered["operation_id"], lost["operation_id"])
        self.assertEqual(self.execute(row), recovered)
        self.assertEqual(self.provider.counts(), {"external_calls": 1, "committed_entries": 1})

    def test_intent_without_send_stays_unknown_if_no_receipt(self):
        row = approved(self.service)
        result = self.execute(row, fault="before_dispatch")
        self.service = ApprovalService(self.service.path, self.provider, lambda: self.now)
        self.assertEqual(self.execute(row)["operation_id"], result["operation_id"])
        self.assertEqual(self.service.reconcile(ALICE, row["approval_id"], 1)["status"], "unknown")
        self.assert_no_write()

    def test_consumed_approval_cannot_be_edited_to_bypass_unknown(self):
        row = approved(self.service)
        self.execute(row, fault="before_dispatch")
        current = self.service.view(ALICE, row["approval_id"], 1)
        with self.assertRaisesRegex(Refused, "not_editable"):
            self.service.edit(ALICE, row["approval_id"], 1, current["version"], "changed")

    def test_new_approval_cannot_bypass_unknown_in_same_run(self):
        row = approved(self.service)
        self.execute(row, fault="before_dispatch")
        with self.assertRaisesRegex(Refused, "unresolved_operation_in_run"):
            self.service.create(ALICE, "same purpose new approval")
        self.assert_no_write()

    def test_preexisting_other_approval_cannot_bypass_unknown(self):
        first = approved(self.service)
        second = approved(self.service)
        self.execute(first, fault="before_dispatch")
        with self.assertRaisesRegex(Refused, "unresolved_operation_in_run"):
            self.execute(second)
        self.assert_no_write()

    def test_concurrent_different_decisions_have_one_winner(self):
        row = pending(self.service)
        barrier = threading.Barrier(2)

        def worker(decision):
            service = ApprovalService(self.service.path, self.provider, lambda: self.now)
            barrier.wait(timeout=5)
            try:
                return service.decide(row["approval_id"], 1, row["version"],
                                      "concurrent-" + decision, decision, ALICE)["state"]
            except Refused as exc:
                return str(exc)

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(worker, ["approve", "reject"]))
        self.assertEqual(sum(x in ("approved", "rejected") for x in results), 1)
        self.assertIn("stale_or_not_pending", results)
        self.assert_no_write()

    def test_concurrent_identical_decision_reuses_response(self):
        row = pending(self.service)
        barrier = threading.Barrier(2)

        def worker(_):
            service = ApprovalService(self.service.path, self.provider, lambda: self.now)
            barrier.wait(timeout=5)
            return service.decide(row["approval_id"], 1, row["version"], "same-id", "approve", ALICE)

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(worker, range(2)))
        self.assertEqual(results[0], results[1])

    def test_concurrent_execution_reserves_once(self):
        row = approved(self.service)
        entered, release = threading.Event(), threading.Event()
        original = self.provider.append

        def delayed(operation_id, action):
            entered.set()
            if not release.wait(timeout=5):
                raise RuntimeError("test release timed out")
            return original(operation_id, action)

        self.provider.append = delayed
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(self.execute, row)
            try:
                self.assertTrue(entered.wait(timeout=5))
                second = self.execute(row)
                self.assertEqual(second["status"], "unknown")
            finally:
                release.set()
            self.assertEqual(first.result(timeout=5)["status"], "succeeded")
        self.assertEqual(self.provider.counts(), {"external_calls": 1, "committed_entries": 1})

    def test_downstream_precondition_closes_resource_race(self):
        row = approved(self.service)
        original = self.provider.append

        def changed_before_append(operation_id, action):
            self.provider.change_version()
            return original(operation_id, action)

        self.provider.append = changed_before_append
        result = self.execute(row)
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["reason"], "resource_changed")
        self.assertEqual(result["ticket_id"], "A-100")
        self.assertTrue(result["operation_id"])
        self.assertEqual(self.execute(row), result)
        self.assertEqual(self.provider.counts(), {"external_calls": 1, "committed_entries": 0})

    def test_first_dispatch_validates_receipt_before_claiming_completion(self):
        original = self.provider.append
        corruptions = [
            lambda receipt: None,
            lambda receipt: {**receipt, "operation_id": "wrong-operation"},
            lambda receipt: {**receipt, "ticket_id": "wrong-ticket"},
            lambda receipt: {**receipt, "entry_id": ""},
            lambda receipt: {**receipt, "entry_id": False},
            lambda receipt: {**receipt, "reason": "contradictory_rejection"},
        ]
        for corrupt in corruptions:
            with self.subTest(corrupt=corrupt):
                row = approved(self.service)

                def corrupt_after_commit(operation_id, action):
                    return corrupt(original(operation_id, action))

                self.provider.append = corrupt_after_commit
                result = self.execute(row)
                self.assertEqual(result["status"], "unknown")
                self.assertEqual(result["reason"], "invalid_receipt")
                # The real downstream write did succeed. Only its original
                # independently queried receipt can establish completion now.
                recovered = self.service.reconcile(ALICE, row["approval_id"], 1)
                self.assertEqual(recovered["status"], "succeeded")
        self.assertEqual(self.provider.counts(), {"external_calls": 6, "committed_entries": 6})

    def test_arbitrary_non_success_receipt_does_not_prove_no_write(self):
        row = approved(self.service)
        original = self.provider.append

        def ambiguous_after_commit(operation_id, action):
            original(operation_id, action)
            return {"status": "rejected", "operation_id": operation_id,
                    "ticket_id": "A-100", "reason": "operation_payload_conflict"}

        self.provider.append = ambiguous_after_commit
        self.assertEqual(self.execute(row)["status"], "unknown")
        self.assertEqual(self.execute(row)["status"], "unknown")
        self.assertEqual(self.service.reconcile(ALICE, row["approval_id"], 1)["status"], "succeeded")
        self.assertEqual(self.provider.counts(), {"external_calls": 1, "committed_entries": 1})

    def test_invalid_receipt_does_not_claim_success(self):
        row = approved(self.service)
        self.execute(row, fault="before_dispatch")
        self.provider.lookup = lambda operation_id: {"status": "succeeded", "operation_id": "wrong"}
        self.assertEqual(self.service.reconcile(ALICE, row["approval_id"], 1)["status"], "unknown")

    def test_snapshot_corruption_fails_closed(self):
        row = approved(self.service)
        with transaction(self.service.path) as db:
            db.execute("UPDATE approvals SET action_hash='tampered' WHERE id=?", (row["approval_id"],))
        with self.assertRaisesRegex(Refused, "snapshot_corrupt"):
            self.execute(row)
        self.assert_no_write()

    def test_audit_does_not_store_body(self):
        body = "SYNTHETIC-PRIVATE-CONTENT"
        row = self.decide(pending(self.service, body))
        self.execute(row)
        db = connect(self.service.path)
        try:
            audit = [tuple(r) for r in db.execute("SELECT * FROM audit")]
            self.assertNotIn(body, repr(audit))
            self.assertTrue(any("succeeded" in r for r in audit))
        finally:
            db.close()

    def test_version_and_body_types_are_strict(self):
        for body in (None, "", " ", "a" * 501):
            with self.subTest(body=body), self.assertRaises(Refused):
                self.service.create(ALICE, body)
        row = pending(self.service)
        with self.assertRaisesRegex(Refused, "invalid_version"):
            self.service.decide(row["approval_id"], True, row["version"], "x", "approve", ALICE)


if __name__ == "__main__":
    unittest.main()
