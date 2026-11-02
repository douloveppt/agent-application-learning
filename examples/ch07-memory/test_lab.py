from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import sqlite3
import unittest
from zoneinfo import ZoneInfo

from lab import MemoryLabError, MemoryProjection, MemoryStore, ProjectionEntry, Scope


class MemoryStoreTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.store = MemoryStore(self.db)
        self.now = datetime(2026, 11, 2, 9, tzinfo=timezone.utc)
        self.scope = Scope("company-a", "alice", self.now)
        self.expiry = self.now + timedelta(days=1)

    def put(
        self,
        *,
        event_key="event-001",
        value="先给结论。",
        expected_version=0,
        scope=None,
        expires_at=None,
        confirmed=True,
    ):
        return self.store.put_preference(
            scope or self.scope,
            event_key=event_key,
            slot="response_style",
            value=value,
            expected_version=expected_version,
            source_ref="turn-001",
            confirmed=confirmed,
            expires_at=expires_at,
        )

    def assert_error(self, code, operation):
        with self.assertRaises(MemoryLabError) as caught:
            operation()
        self.assertEqual(caught.exception.code, code)
        self.assertEqual(str(caught.exception), code)

    def test_same_event_and_payload_returns_exact_receipt_once(self):
        first = self.put()
        replay = self.put()
        self.assertEqual(replay, first)
        self.assertEqual(
            self.store.counts(),
            {
                "memory_rows": 1,
                "idempotency_rows": 1,
                "outbox_rows": 1,
                "tombstone_rows": 0,
            },
        )

    def test_same_event_with_different_payload_conflicts_without_mutation(self):
        first = self.put()
        self.assert_error(
            "idempotency_conflict",
            lambda: self.put(value="换成详细说明。"),
        )
        self.assertEqual(self.store.get_preference(self.scope, "response_style")["value"], "先给结论。")
        self.assertEqual(self.store.counts()["outbox_rows"], 1)
        self.assertEqual(first["version"], 1)

    def test_cas_allows_correction_and_rejects_stale_writer(self):
        first = self.put()
        corrected = self.put(event_key="event-002", value="给三步操作。", expected_version=1)
        self.assertEqual(corrected["version"], 2)
        self.assert_error(
            "version_conflict",
            lambda: self.put(event_key="event-stale", value="旧值", expected_version=1),
        )
        current = self.store.get_preference(self.scope, "response_style")
        self.assertEqual((current["value"], current["version"]), ("给三步操作。", 2))
        self.assertEqual(self.store.counts()["idempotency_rows"], 2)
        self.assertEqual(first["memory_id"], corrected["memory_id"])

    def test_memory_and_outbox_rollback_together_when_outbox_fails(self):
        self.db.execute(
            """CREATE TRIGGER reject_outbox BEFORE INSERT ON outbox
               BEGIN SELECT RAISE(ABORT, 'synthetic failure'); END"""
        )
        self.assert_error("storage_failure", self.put)
        self.assertEqual(
            self.store.counts(),
            {
                "memory_rows": 0,
                "idempotency_rows": 0,
                "outbox_rows": 0,
                "tombstone_rows": 0,
            },
        )

    def test_caller_transaction_is_rejected_without_rolling_it_back(self):
        self.db.execute("CREATE TABLE caller_work(value TEXT)")
        self.db.execute("BEGIN")
        self.db.execute("INSERT INTO caller_work VALUES('still-pending')")
        self.assert_error("invalid_transaction_state", self.put)
        self.assertTrue(self.db.in_transaction)
        self.assertEqual(self.db.execute("SELECT value FROM caller_work").fetchone()[0], "still-pending")
        self.db.execute("ROLLBACK")

    def test_unconfirmed_or_invalid_time_input_is_rejected(self):
        self.assert_error("unconfirmed_memory", lambda: self.put(confirmed=False))
        self.assert_error(
            "invalid_scope_time",
            lambda: self.put(scope=replace(self.scope, now=datetime(2026, 11, 2, 9))),
        )
        self.assert_error("invalid_expiry", lambda: self.put(expires_at=datetime(2026, 11, 3, 9)))
        self.assertEqual(self.store.counts()["memory_rows"], 0)

    def test_expiry_boundary_compares_instants_across_timezones(self):
        self.put(expires_at=self.expiry)
        before = replace(self.scope, now=self.expiry - timedelta(microseconds=1))
        at_boundary = replace(self.scope, now=self.expiry.astimezone(timezone(timedelta(hours=8))))
        self.assertIsNotNone(self.store.get_preference(before, "response_style"))
        self.assertIsNone(self.store.get_preference(at_boundary, "response_style"))

    def test_dst_fold_compares_utc_instants_instead_of_equal_zone_wall_time(self):
        new_york = ZoneInfo("America/New_York")
        later_now = Scope("company-a", "alice", datetime(2026, 11, 1, 1, 30, fold=1, tzinfo=new_york))
        earlier_expiry = datetime(2026, 11, 1, 1, 45, fold=0, tzinfo=new_york)
        self.assert_error(
            "invalid_expiry",
            lambda: self.put(scope=later_now, expires_at=earlier_expiry),
        )

        before_fallback = Scope(
            "company-a", "alice", datetime(2026, 11, 1, 0, 30, fold=0, tzinfo=new_york)
        )
        fold_expiry = datetime(2026, 11, 1, 1, 30, fold=1, tzinfo=new_york)
        self.put(event_key="event-fold", scope=before_fallback, expires_at=fold_expiry)
        same_instant_utc = replace(before_fallback, now=datetime(2026, 11, 1, 6, 30, tzinfo=timezone.utc))
        self.assertIsNone(self.store.get_preference(same_instant_utc, "response_style"))

    def test_expired_success_replays_before_freshness_check_but_new_event_is_rejected(self):
        first = self.put(expires_at=self.expiry)
        expired_scope = replace(self.scope, now=self.expiry)
        replay = self.put(scope=expired_scope, expires_at=self.expiry)
        self.assertEqual(replay, first)
        before = self.store.counts()
        self.assert_error(
            "invalid_expiry",
            lambda: self.put(event_key="event-new", scope=expired_scope, expires_at=self.expiry),
        )
        self.assertEqual(self.store.counts(), before)

    def test_scope_isolation_and_errors_do_not_expose_denied_identity_or_value(self):
        self.put(value="ALICE_PRIVATE")
        bob = Scope("company-a", "bob", self.now)
        self.assertIsNone(self.store.get_preference(bob, "response_style"))
        try:
            self.store.delete_preference(
                bob,
                event_key="delete-001",
                slot="response_style",
                expected_version=1,
                source_ref="turn-002",
                confirmed=True,
            )
        except MemoryLabError as error:
            rendered = str(error)
        else:
            self.fail("cross-user delete unexpectedly succeeded")
        self.assertEqual(rendered, "version_conflict")
        self.assertNotIn("alice", rendered)
        self.assertNotIn("ALICE_PRIVATE", rendered)

    def test_same_actor_name_in_another_tenant_is_isolated_on_read_projection_and_delete(self):
        other_tenant = Scope("company-b", "alice", self.now)
        remote = self.put(scope=other_tenant, value="OTHER_TENANT_PRIVATE")
        projection = MemoryProjection()
        self.store.project_outbox_event(remote["outbox_event_id"], projection, self.now)
        self.assertIsNone(self.store.get_preference(self.scope, "response_style"))
        visible = projection.retrieve(self.store, self.scope, "response_style")
        self.assertEqual(visible, {"items": [], "rejected_count": 1})
        self.assert_error(
            "version_conflict",
            lambda: self.store.delete_preference(
                self.scope, event_key="delete-tenant", slot="response_style",
                expected_version=1, source_ref="turn-002", confirmed=True,
            ),
        )
        self.assertEqual(
            self.store.get_preference(other_tenant, "response_style")["value"],
            "OTHER_TENANT_PRIVATE",
        )

    def test_projection_rechecks_scope_version_status_and_expiry(self):
        first = self.put(expires_at=self.expiry)
        projection = MemoryProjection()
        self.store.project_outbox_event(first["outbox_event_id"], projection, self.now)
        duplicate = self.store.project_outbox_event(first["outbox_event_id"], projection, self.now)
        self.assertFalse(duplicate["applied"])
        other_scope = Scope("company-a", "bob", self.now)
        other = self.put(event_key="event-bob", value="BOB_PRIVATE", scope=other_scope, expires_at=self.expiry)
        self.store.project_outbox_event(other["outbox_event_id"], projection, self.now)

        visible = projection.retrieve(self.store, self.scope, "response_style")
        self.assertEqual([item["value"] for item in visible["items"]], ["先给结论。"])
        self.assertEqual(visible["rejected_count"], 1)
        self.put(event_key="event-002", value="新版本", expected_version=1, expires_at=self.expiry)
        stale = projection.retrieve(self.store, self.scope, "response_style")
        self.assertEqual(stale["items"], [])
        self.assertEqual(stale["rejected_count"], 2)
        expired = projection.retrieve(self.store, replace(self.scope, now=self.expiry), "response_style")
        self.assertEqual(expired["items"], [])

    def test_delete_tombstone_and_projection_fence_reject_late_active_event(self):
        first = self.put(expires_at=self.expiry)
        projection = MemoryProjection()
        self.store.project_outbox_event(first["outbox_event_id"], projection, self.now)
        delayed = ProjectionEntry(
            first["memory_id"],
            self.scope.tenant_id,
            self.scope.actor_id,
            "response_style",
            "先给结论。",
            1,
            "active",
            self.expiry,
        )
        deleted = self.store.delete_preference(
            self.scope,
            event_key="delete-001",
            slot="response_style",
            expected_version=1,
            source_ref="turn-002",
            confirmed=True,
        )
        # Even before projection consumes the delete event, authoritative readback hides it.
        self.assertEqual(projection.retrieve(self.store, self.scope, "response_style")["items"], [])
        self.store.project_outbox_event(deleted["outbox_event_id"], projection, self.now)
        self.assertFalse(projection.apply(delayed))
        self.assertEqual(projection.fence_version(first["memory_id"]), 2)
        self.assertEqual(self.store.counts()["tombstone_rows"], 1)
        self.assertIsNone(self.store.get_preference(self.scope, "response_style"))

    def test_deleted_slot_replays_old_receipt_but_rejects_every_new_write(self):
        first = self.put()
        self.store.delete_preference(
            self.scope, event_key="delete-001", slot="response_style",
            expected_version=1, source_ref="turn-002", confirmed=True,
        )
        deleted_counts = self.store.counts()
        self.assertEqual(self.put(), first)
        self.assertIsNone(self.store.get_preference(self.scope, "response_style"))
        for key, version in (("restore-create", 0), ("restore-versioned", 2)):
            with self.subTest(version=version):
                self.assert_error(
                    "version_conflict",
                    lambda key=key, version=version: self.put(
                        event_key=key, value="不得恢复", expected_version=version,
                    ),
                )
        self.assertEqual(self.store.counts(), deleted_counts)

    def test_outbox_contains_only_opaque_id_version_status_and_timestamps(self):
        self.put(value="PRIVATE_PREFERENCE_TEXT")
        columns = [row[1] for row in self.db.execute("PRAGMA table_info(outbox)")]
        self.assertEqual(columns, ["event_id", "memory_id", "version", "status", "created_at", "published_at"])
        rendered = json.dumps([dict(row) for row in self.db.execute("SELECT * FROM outbox")])
        self.assertNotIn("PRIVATE_PREFERENCE_TEXT", rendered)
        self.assertNotIn("alice", rendered)
        self.assertNotIn("response_style", rendered)


if __name__ == "__main__":
    unittest.main()
