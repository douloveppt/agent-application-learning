from pathlib import Path
import sqlite3
import tempfile
import unittest

import lab


class ReliabilityLabTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="ch26-test-")
        self.addCleanup(self.directory.cleanup)
        base = Path(self.directory.name)
        self.runtime_path = base / "runtime.db"
        self.provider_path = base / "provider.db"
        self.runtime = lab.RuntimeStore(self.runtime_path)
        self.provider = lab.ProviderStore(self.provider_path)
        self.runtime.initialize()
        self.provider.initialize()
        self.scope = lab.Scope("tenant-a", "alice")
        self.payload = {"title": "VPN 无法连接", "category": "vpn"}

    def prepare(self, key="request-1", payload=None, scope=None):
        return self.runtime.prepare(
            scope or self.scope, key, payload or self.payload
        )[0]

    def test_operation_and_outbox_roll_back_together(self):
        with self.assertRaises(lab.InjectedFailure):
            self.runtime.prepare(
                self.scope,
                "atomic-1",
                self.payload,
                fault="after_operation_insert",
            )
        self.assertEqual(self.runtime.counts(), {"operations": 0, "outbox": 0})

    def test_same_scoped_key_reuses_normalized_payload_and_rejects_change(self):
        first = self.prepare()
        reopened = lab.RuntimeStore(self.runtime_path)
        same, created = reopened.prepare(
            self.scope,
            "request-1",
            {"title": "  VPN   无法连接 ", "category": " VPN "},
        )
        self.assertEqual((same, created), (first, False))
        with self.assertRaises(lab.Conflict):
            reopened.prepare(
                self.scope,
                "request-1",
                {"title": "VPN 很慢", "category": "vpn"},
            )
        self.assertEqual(reopened.counts(), {"operations": 1, "outbox": 1})

    def test_same_client_key_in_different_trusted_scope_is_independent(self):
        first = self.prepare()
        second = self.prepare(scope=lab.Scope("tenant-a", "bob"))
        third = self.prepare(scope=lab.Scope("tenant-b", "alice"))
        self.assertEqual(len({first, second, third}), 3)
        keys = {
            self.runtime.snapshot(operation_id)["operation_key"]
            for operation_id in (first, second, third)
        }
        self.assertEqual(len(keys), 3)

    def test_outbox_message_binds_scope_operation_and_payload(self):
        operation_id = self.prepare()
        state = self.runtime.snapshot(operation_id)
        message = state["message"]
        self.assertEqual(message["operation_key"], state["operation_key"])
        self.assertEqual(message["payload_fp"], state["payload_fp"])
        self.assertEqual(message["tenant_id"], self.scope.tenant_id)
        self.assertEqual(message["requester_id"], self.scope.requester_id)
        self.assertEqual(lab.fingerprint(message), state["message_fp"])

    def test_provider_ticket_and_inbox_roll_back_together(self):
        message = self.runtime.snapshot(self.prepare())["message"]
        with self.assertRaises(lab.InjectedFailure):
            self.provider.consume(message, fault="after_ticket_insert")
        self.assertEqual(self.provider.counts(), {"tickets": 0, "inbox": 0})

    def test_duplicate_delivery_has_one_inbox_receipt_and_one_ticket(self):
        message = self.runtime.snapshot(self.prepare())["message"]
        first = self.provider.consume(message)
        duplicate = self.provider.consume(message)
        self.assertEqual(duplicate, first)
        self.assertEqual(self.provider.counts(), {"tickets": 1, "inbox": 1})

    def test_distinct_logical_event_for_same_operation_reuses_business_result(self):
        message = self.runtime.snapshot(self.prepare())["message"]
        first = self.provider.consume(message)
        redelivery = {**message, "event_id": "event-redelivery-2"}
        second = self.provider.consume(redelivery)
        self.assertEqual(second, first)
        self.assertEqual(self.provider.counts(), {"tickets": 1, "inbox": 2})

    def test_event_id_reuse_with_different_message_conflicts(self):
        message = self.runtime.snapshot(self.prepare())["message"]
        self.provider.consume(message)
        changed_payload = {"title": "VPN 很慢", "category": "vpn"}
        changed = {
            **message,
            "payload": changed_payload,
            "payload_fp": lab.fingerprint(changed_payload),
        }
        with self.assertRaises(lab.Conflict):
            self.provider.consume(changed)
        self.assertEqual(self.provider.counts(), {"tickets": 1, "inbox": 1})

    def test_provider_success_with_lost_receipt_recovers_by_read_only_lookup(self):
        operation_id = self.prepare()
        with self.assertRaises(lab.SimulatedCrash):
            lab.dispatch(
                self.runtime,
                self.provider,
                operation_id,
                crash_at="after_provider_commit",
            )
        crashed = self.runtime.snapshot(operation_id)
        self.assertEqual(crashed["state"], "DISPATCHING")
        self.assertIsNone(crashed["receipt"])
        self.assertEqual(crashed["attempts"], 1)
        self.assertEqual(self.provider.counts(), {"tickets": 1, "inbox": 1})

        reopened_runtime = lab.RuntimeStore(self.runtime_path)
        reopened_provider = lab.ProviderStore(self.provider_path)
        self.assertEqual(
            lab.reconcile(reopened_runtime, reopened_provider, operation_id),
            "SUCCEEDED",
        )
        recovered = reopened_runtime.snapshot(operation_id)
        self.assertEqual(recovered["state"], "SUCCEEDED")
        self.assertEqual(recovered["outbox_state"], "DELIVERED")
        self.assertEqual(recovered["attempts"], 1)
        self.assertIsNotNone(recovered["receipt"])
        self.assertEqual(reopened_provider.counts(), {"tickets": 1, "inbox": 1})
        self.assertEqual(
            lab.reconcile(reopened_runtime, reopened_provider, operation_id),
            "SUCCEEDED",
        )

    def test_negative_lookup_stays_unknown_and_never_redispatches(self):
        operation_id = self.prepare()
        with self.assertRaises(lab.SimulatedCrash):
            lab.dispatch(
                self.runtime,
                self.provider,
                operation_id,
                crash_at="before_provider",
            )
        reopened_runtime = lab.RuntimeStore(self.runtime_path)
        reopened_provider = lab.ProviderStore(self.provider_path)
        for _ in range(3):
            self.assertEqual(
                lab.reconcile(reopened_runtime, reopened_provider, operation_id),
                "STILL_UNKNOWN",
            )
        state = reopened_runtime.snapshot(operation_id)
        self.assertEqual(state["state"], "UNKNOWN")
        self.assertEqual(state["outbox_state"], "UNKNOWN")
        self.assertEqual(state["attempts"], 1)
        self.assertIsNone(state["receipt"])
        self.assertEqual(reopened_provider.counts(), {"tickets": 0, "inbox": 0})

    def test_different_client_key_is_a_real_new_operation(self):
        first = self.prepare("request-1")
        second = self.prepare("request-2")
        self.assertEqual(lab.dispatch(self.runtime, self.provider, first), "SUCCEEDED")
        self.assertEqual(lab.dispatch(self.runtime, self.provider, second), "SUCCEEDED")
        self.assertNotEqual(
            self.runtime.snapshot(first)["operation_key"],
            self.runtime.snapshot(second)["operation_key"],
        )
        self.assertEqual(self.provider.counts(), {"tickets": 2, "inbox": 2})

    def test_reconciliation_rejects_provider_result_with_wrong_owner(self):
        operation_id = self.prepare()
        with self.assertRaises(lab.SimulatedCrash):
            lab.dispatch(
                self.runtime,
                self.provider,
                operation_id,
                crash_at="before_provider",
            )
        state = self.runtime.snapshot(operation_id)
        with sqlite3.connect(self.provider_path) as db:
            db.execute(
                """INSERT INTO tickets
                   (ticket_id, operation_key, tenant_id, requester_id, action,
                    payload_fp, payload_json, status)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 'OPEN')""",
                (
                    "ticket-tampered",
                    state["operation_key"],
                    "wrong-tenant",
                    state["requester_id"],
                    state["action"],
                    state["payload_fp"],
                    lab.encode(state["payload"]),
                ),
            )
        reopened = lab.RuntimeStore(self.runtime_path)
        self.assertEqual(
            lab.reconcile(reopened, lab.ProviderStore(self.provider_path), operation_id),
            "MANUAL_REVIEW",
        )
        checked = reopened.snapshot(operation_id)
        self.assertEqual(checked["state"], "UNKNOWN")
        self.assertEqual(checked["review_required"], 1)
        self.assertIsNone(checked["receipt"])
        self.assertEqual(checked["attempts"], 1)
        self.assertEqual(
            lab.reconcile(reopened, lab.ProviderStore(self.provider_path), operation_id),
            "MANUAL_REVIEW",
        )
        self.assertEqual(reopened.snapshot(operation_id)["state"], "UNKNOWN")

    def test_completed_receipt_survives_reopen(self):
        operation_id = self.prepare()
        lab.dispatch(self.runtime, self.provider, operation_id)
        before = self.runtime.snapshot(operation_id)
        after = lab.RuntimeStore(self.runtime_path).snapshot(operation_id)
        self.assertEqual(after["state"], "SUCCEEDED")
        self.assertEqual(after["receipt"], before["receipt"])
        self.assertEqual(after["attempts"], 1)


if __name__ == "__main__":
    unittest.main()
