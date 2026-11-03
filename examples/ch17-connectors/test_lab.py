"""Behavior tests for the tenant-bound connector and its durable ledger."""
from pathlib import Path
import json
import sqlite3
import tempfile
import unittest

import lab


class ConnectorTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="connector-test-")
        self.addCleanup(self.directory.cleanup)
        self.ledger = Path(self.directory.name) / "ledger.db"
        self.provider_db = Path(self.directory.name) / "provider.db"
        lab.initialize(self.ledger, self.provider_db)
        self.context = lab.RequestContext(
            "tenant-a", "employee-7", frozenset({lab.CREATE_OWN})
        )
        self.arguments = {"title": "VPN 无法连接", "description": "证书已过期"}

    def connector(self, mode="success"):
        return lab.TicketConnector(
            self.ledger, "tenant-a", lab.FakeProvider(self.provider_db, mode)
        )

    def stats(self):
        return lab.provider_stats(self.provider_db)

    def persist_dispatched(self, operation_id):
        """Build the exact durable boundary left by a crash before local receipt."""
        payload = {
            "tenant_id": self.context.tenant_id,
            "requester_user_id": self.context.user_id,
            "title": self.arguments["title"],
            "description": self.arguments["description"],
        }
        payload_hash = lab.digest(payload)
        with lab.database(self.ledger) as db:
            db.execute(
                """INSERT INTO operations (
                    operation_id, connection_tenant, requester_user_id,
                    payload_json, payload_hash, status
                ) VALUES (?, ?, ?, ?, ?, 'dispatched')""",
                (
                    operation_id,
                    "tenant-a",
                    self.context.user_id,
                    lab.encode(payload),
                    payload_hash,
                ),
            )
        return payload, payload_hash

    def test_success_uses_trusted_requester_and_persists_receipt(self):
        result = self.connector().submit(self.context, "op-1", self.arguments)
        self.assertEqual(result.status, "succeeded")
        self.assertEqual(result.ticket_id, "TICKET-000001")
        state = lab.operation_snapshot(self.ledger, "op-1")
        self.assertEqual(state["payload"]["requester_user_id"], "employee-7")
        with sqlite3.connect(self.provider_db) as db:
            requester = db.execute("SELECT requester_user_id FROM tickets").fetchone()[0]
        self.assertEqual(requester, "employee-7")
        self.assertEqual(self.stats(), {"tickets": 1, "create_calls": 1, "lookup_calls": 0})

    def test_model_cannot_supply_requester_identity(self):
        injected = {**self.arguments, "requester_user_id": "admin"}
        with self.assertRaises(lab.InvalidArguments):
            self.connector().submit(self.context, "op-identity", injected)
        self.assertEqual(lab.operation_count(self.ledger), 0)
        self.assertEqual(self.stats()["create_calls"], 0)

    def test_cross_tenant_context_is_rejected_before_ledger_or_provider(self):
        other = lab.RequestContext(
            "tenant-b", "employee-7", frozenset({lab.CREATE_OWN})
        )
        with self.assertRaises(lab.AuthorizationDenied):
            self.connector().submit(other, "op-cross-tenant", self.arguments)
        self.assertEqual(lab.operation_count(self.ledger), 0)
        self.assertEqual(self.stats()["create_calls"], 0)

    def test_missing_permission_is_rejected_before_dispatch(self):
        no_permission = lab.RequestContext("tenant-a", "employee-7", frozenset())
        with self.assertRaises(lab.AuthorizationDenied):
            self.connector().submit(no_permission, "op-forbidden", self.arguments)
        self.assertEqual(lab.operation_count(self.ledger), 0)
        self.assertEqual(self.stats()["create_calls"], 0)

    def test_confirmed_success_is_reused_without_provider_calls(self):
        connector = self.connector()
        first = connector.submit(self.context, "op-reuse", self.arguments)
        second = connector.submit(self.context, "op-reuse", self.arguments)
        self.assertEqual((first.status, second.status), ("succeeded", "succeeded"))
        self.assertEqual(second.source, "ledger")
        self.assertEqual(self.stats(), {"tickets": 1, "create_calls": 1, "lookup_calls": 0})

    def test_operation_id_cannot_be_reused_for_different_input(self):
        connector = self.connector()
        connector.submit(self.context, "op-conflict", self.arguments)
        with self.assertRaises(lab.OperationConflict):
            connector.submit(
                self.context,
                "op-conflict",
                {**self.arguments, "description": "另一项故障"},
            )
        self.assertEqual(self.stats()["create_calls"], 1)

    def test_commit_then_timeout_is_unknown_and_does_not_replay(self):
        connector = self.connector("commit_then_timeout")
        result = connector.submit(self.context, "op-timeout", self.arguments)
        self.assertEqual(result.status, "unknown")
        self.assertEqual(lab.operation_snapshot(self.ledger, "op-timeout")["status"], "unknown")
        self.assertEqual(self.stats(), {"tickets": 1, "create_calls": 1, "lookup_calls": 0})
        recovered = connector.submit(self.context, "op-timeout", self.arguments)
        self.assertEqual(recovered.status, "succeeded")
        self.assertEqual(recovered.source, "provider_lookup")
        self.assertEqual(self.stats(), {"tickets": 1, "create_calls": 1, "lookup_calls": 1})

    def test_restart_recovers_unknown_operation_by_lookup_only(self):
        first = self.connector("commit_then_timeout")
        self.assertEqual(first.submit(self.context, "op-restart", self.arguments).status, "unknown")
        restarted = lab.launch_worker(
            self.ledger,
            self.provider_db,
            "tenant-a",
            self.context,
            "op-restart",
            self.arguments,
        )
        self.assertEqual(restarted.returncode, 0, restarted.stderr)
        recovered = json.loads(restarted.stdout)
        self.assertEqual(recovered["status"], "succeeded")
        self.assertEqual(recovered["ticket_id"], "TICKET-000001")
        self.assertEqual(self.stats(), {"tickets": 1, "create_calls": 1, "lookup_calls": 1})

    def test_dispatched_before_provider_call_recovers_by_lookup_without_create(self):
        self.persist_dispatched("op-before-provider")
        self.assertEqual(
            lab.operation_snapshot(self.ledger, "op-before-provider")["status"],
            "dispatched",
        )
        recovered = self.connector().submit(
            self.context, "op-before-provider", self.arguments
        )
        self.assertEqual(recovered.status, "unknown")
        self.assertEqual(recovered.source, "provider_lookup")
        self.assertEqual(
            lab.operation_snapshot(self.ledger, "op-before-provider")["status"],
            "unknown",
        )
        self.assertEqual(
            self.stats(), {"tickets": 0, "create_calls": 0, "lookup_calls": 1}
        )

    def test_provider_commit_while_ledger_dispatched_recovers_same_ticket(self):
        payload, payload_hash = self.persist_dispatched("op-after-provider")
        original_receipt = lab.FakeProvider(self.provider_db).create_ticket(
            "op-after-provider", payload, payload_hash
        )
        self.assertEqual(
            lab.operation_snapshot(self.ledger, "op-after-provider")["status"],
            "dispatched",
        )
        self.assertEqual(
            self.stats(), {"tickets": 1, "create_calls": 1, "lookup_calls": 0}
        )

        recovered = self.connector().submit(
            self.context, "op-after-provider", self.arguments
        )
        self.assertEqual(recovered.status, "succeeded")
        self.assertEqual(recovered.source, "provider_lookup")
        self.assertEqual(recovered.ticket_id, original_receipt["ticket_id"])
        self.assertEqual(
            self.stats(), {"tickets": 1, "create_calls": 1, "lookup_calls": 1}
        )

    def test_lookup_absence_stays_unknown_and_never_replays(self):
        connector = self.connector("timeout_before_commit")
        self.assertEqual(connector.submit(self.context, "op-absent", self.arguments).status, "unknown")
        for _ in range(2):
            result = connector.submit(self.context, "op-absent", self.arguments)
            self.assertEqual(result.status, "unknown")
            self.assertEqual(result.source, "provider_lookup")
        self.assertEqual(self.stats(), {"tickets": 0, "create_calls": 1, "lookup_calls": 2})

    def test_explicit_business_rejection_is_terminal_and_reused(self):
        connector = self.connector("business_reject")
        first = connector.submit(self.context, "op-rejected", self.arguments)
        second = connector.submit(self.context, "op-rejected", self.arguments)
        self.assertEqual(first.status, "rejected")
        self.assertEqual(first.rejection_code, "QUEUE_CLOSED")
        self.assertEqual(second.source, "ledger")
        self.assertEqual(self.stats(), {"tickets": 0, "create_calls": 1, "lookup_calls": 0})

    def test_unproven_or_mismatched_rejection_is_unknown(self):
        mutations = {
            "wrong tenant": lambda response: response.update(tenant_id="tenant-b"),
            "wrong requester": lambda response: response.update(
                requester_user_id="another-user"
            ),
            "wrong payload hash": lambda response: response.update(
                payload_hash="wrong-hash"
            ),
            "missing no-effect evidence": lambda response: response.pop("effect"),
        }

        class MutatedRejectionProvider(lab.FakeProvider):
            def __init__(self, provider_db, mutate):
                super().__init__(provider_db, "business_reject")
                self.mutate = mutate

            def create_ticket(self, client_reference, payload, payload_hash):
                response = super().create_ticket(
                    client_reference, payload, payload_hash
                )
                self.mutate(response)
                return response

        for index, (case, mutate) in enumerate(mutations.items()):
            with self.subTest(case=case):
                operation_id = f"op-unproven-rejection-{index}"
                connector = lab.TicketConnector(
                    self.ledger,
                    "tenant-a",
                    MutatedRejectionProvider(self.provider_db, mutate),
                )
                result = connector.submit(
                    self.context, operation_id, self.arguments
                )
                self.assertEqual(result.status, "unknown")
                state = lab.operation_snapshot(self.ledger, operation_id)
                self.assertEqual(state["status"], "unknown")
                self.assertIsNone(state["rejection_code"])
        self.assertEqual(
            self.stats(), {"tickets": 0, "create_calls": 4, "lookup_calls": 0}
        )

    def test_malformed_receipt_becomes_unknown_then_reconciles(self):
        connector = self.connector("malformed_receipt")
        first = connector.submit(self.context, "op-malformed", self.arguments)
        self.assertEqual(first.status, "unknown")
        self.assertIn("client_reference", lab.operation_snapshot(self.ledger, "op-malformed")["last_error"])
        recovered = connector.submit(self.context, "op-malformed", self.arguments)
        self.assertEqual(recovered.status, "succeeded")
        self.assertEqual(self.stats(), {"tickets": 1, "create_calls": 1, "lookup_calls": 1})

    def test_business_resource_id_is_distinct_from_operation_id(self):
        result = self.connector().submit(self.context, "op-client-123", self.arguments)
        self.assertNotEqual(result.ticket_id, result.operation_id)
        self.assertEqual(result.ticket_id, "TICKET-000001")
        state = lab.operation_snapshot(self.ledger, "op-client-123")
        self.assertEqual(state["receipt"]["client_reference"], "op-client-123")

    def test_normalized_whitespace_reuses_the_same_operation(self):
        connector = self.connector()
        padded = {"title": "  VPN 无法连接  ", "description": "  证书已过期\n"}
        connector.submit(self.context, "  op-normalized  ", padded)
        result = connector.submit(self.context, "op-normalized", self.arguments)
        self.assertEqual(result.source, "ledger")
        self.assertEqual(self.stats()["create_calls"], 1)

    def test_invalid_arguments_create_no_durable_intent(self):
        invalid_cases = (
            {"title": "", "description": "x"},
            {"title": "x", "description": " "},
            {"title": 7, "description": "x"},
            {"title": "x", "description": "y", "priority": "urgent"},
        )
        for index, arguments in enumerate(invalid_cases):
            with self.subTest(arguments=arguments):
                with self.assertRaises(lab.InvalidArguments):
                    self.connector().submit(self.context, f"op-invalid-{index}", arguments)
        self.assertEqual(lab.operation_count(self.ledger), 0)
        self.assertEqual(self.stats()["create_calls"], 0)

    def test_mismatched_lookup_record_cannot_confirm_success(self):
        connector = self.connector("timeout_before_commit")
        connector.submit(self.context, "op-mismatch", self.arguments)
        state = lab.operation_snapshot(self.ledger, "op-mismatch")
        with lab.database(self.provider_db) as db:
            db.execute(
                """INSERT INTO tickets (
                    client_reference, tenant_id, requester_user_id, title, description, payload_hash
                ) VALUES (?, ?, ?, ?, ?, ?)""",
                ("op-mismatch", "tenant-a", "employee-7", "x", "y", "wrong-hash"),
            )
        result = connector.submit(self.context, "op-mismatch", self.arguments)
        self.assertEqual(result.status, "unknown")
        self.assertEqual(lab.operation_snapshot(self.ledger, "op-mismatch")["payload_hash"], state["payload_hash"])
        self.assertEqual(self.stats(), {"tickets": 1, "create_calls": 1, "lookup_calls": 1})

    def test_lookup_record_for_wrong_requester_cannot_confirm_success(self):
        connector = self.connector("timeout_before_commit")
        connector.submit(self.context, "op-wrong-requester", self.arguments)
        state = lab.operation_snapshot(self.ledger, "op-wrong-requester")
        with lab.database(self.provider_db) as db:
            db.execute(
                """INSERT INTO tickets (
                    client_reference, tenant_id, requester_user_id, title, description, payload_hash
                ) VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    "op-wrong-requester",
                    "tenant-a",
                    "another-user",
                    "x",
                    "y",
                    state["payload_hash"],
                ),
            )
        result = connector.submit(self.context, "op-wrong-requester", self.arguments)
        self.assertEqual(result.status, "unknown")
        self.assertEqual(lab.operation_snapshot(self.ledger, "op-wrong-requester")["status"], "unknown")
        self.assertEqual(self.stats(), {"tickets": 1, "create_calls": 1, "lookup_calls": 1})


if __name__ == "__main__":
    unittest.main()
