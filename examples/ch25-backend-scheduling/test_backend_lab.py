from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sqlite3
import tempfile
import unittest

from backend_lab import Busy, Conflict, ManualClock, NotFound, Principal, Rejected, StaleLease, Store


class BackendLabTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ch25-test-")
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "runs.sqlite"
        self.clock = ManualClock()
        self.store = Store(self.path, clock=self.clock)
        self.alice = Principal("tenant-a", "alice")
        self.bob = Principal("tenant-a", "bob")
        self.other = Principal("tenant-b", "alice")
        self.session = self.store.create_session(self.alice)

    def submit(self, key="report-1", topic="weekly"):
        return self.store.submit(self.alice, self.session, key, {"topic": topic})[0]

    def test_duplicate_request_reuses_durable_run_after_reopen(self):
        first = self.submit()
        reopened = Store(self.path, clock=self.clock)
        same, created = reopened.submit(self.alice, self.session, "report-1", {"topic": " weekly "})
        self.assertEqual(same, first)
        self.assertFalse(created)
        self.assertEqual(reopened.get_run(self.alice, first)["state"], "queued")

    def test_same_key_different_body_or_session_conflicts(self):
        self.submit()
        with self.assertRaises(Conflict):
            self.submit(topic="different")
        new_session = self.store.create_session(self.alice)
        with self.assertRaises(Conflict):
            self.store.submit(self.alice, new_session, "report-1", {"topic": "weekly"})

    def test_same_tenant_other_user_cannot_discover_request(self):
        self.submit()
        bob_session = self.store.create_session(self.bob)
        messages = []
        for topic in ("weekly", "different"):
            with self.assertRaises(Conflict) as caught:
                self.store.submit(self.bob, bob_session, "report-1", {"topic": topic})
            messages.append(str(caught.exception))
        self.assertEqual(messages, ["request key unavailable"] * 2)

    def test_same_key_other_tenant_is_independent(self):
        first = self.submit()
        session = self.store.create_session(self.other)
        second, created = self.store.submit(self.other, session, "report-1", {"topic": "weekly"})
        self.assertTrue(created)
        self.assertNotEqual(first, second)

    def test_session_run_and_artifact_require_owner(self):
        run_id = self.submit()
        lease = self.store.claim("tenant-a", "worker")
        artifact = self.store.complete(lease, b"report")
        for principal in (self.bob, self.other):
            with self.assertRaises(NotFound):
                self.store.submit(principal, self.session, "new", {"topic": "weekly"})
            with self.assertRaises(NotFound):
                self.store.get_run(principal, run_id)
            with self.assertRaises(NotFound):
                self.store.request_cancel(principal, run_id)
            with self.assertRaises(NotFound):
                self.store.download(principal, artifact)

    def test_competing_claims_and_duplicate_wakeups_obey_tenant_cap(self):
        self.submit("a")
        self.submit("b")
        with ThreadPoolExecutor(max_workers=6) as pool:
            leases = list(pool.map(lambda i: self.store.claim("tenant-a", f"w-{i}"), range(6)))
        self.assertEqual(sum(lease is not None for lease in leases), 1)
        self.assertIsNone(self.store.claim("tenant-a", "duplicate"))

    def test_active_cap_is_per_tenant(self):
        self.submit()
        session = self.store.create_session(self.other)
        self.store.submit(self.other, session, "one", {"topic": "weekly"})
        self.assertIsNotNone(self.store.claim("tenant-a", "worker-a"))
        self.assertIsNotNone(self.store.claim("tenant-b", "worker-b"))

    def test_expired_lease_reclaims_same_run_and_fences_old_worker(self):
        run_id = self.submit()
        old = self.store.claim("tenant-a", "same-worker", lease_seconds=10)
        self.clock.advance(10)
        new = self.store.claim("tenant-a", "same-worker")
        self.assertEqual(new.run_id, run_id)
        self.assertEqual(new.epoch, old.epoch + 1)
        with self.assertRaises(StaleLease):
            self.store.complete(old, b"old")
        with self.assertRaises(StaleLease):
            self.store.heartbeat(old)
        self.assertEqual(self.store.download(self.alice, self.store.complete(new, b"new")), b"new")

    def test_expiry_rejects_write_even_before_reclaim(self):
        self.submit()
        lease = self.store.claim("tenant-a", "worker", lease_seconds=10)
        self.clock.advance(10)
        with self.assertRaises(StaleLease):
            self.store.complete(lease, b"late")
        with self.assertRaises(StaleLease):
            self.store.heartbeat(lease)

    def test_heartbeat_extends_current_lease(self):
        self.submit()
        lease = self.store.claim("tenant-a", "worker", lease_seconds=10)
        self.clock.advance(9)
        updated = self.store.heartbeat(lease, lease_seconds=20)
        self.assertEqual(updated.epoch, lease.epoch)
        self.clock.advance(5)
        self.assertIsNone(self.store.claim("tenant-a", "other"))
        self.store.complete(updated, b"done")

    def test_queued_cancel_never_claimed(self):
        run_id = self.submit()
        self.assertEqual(self.store.request_cancel(self.alice, run_id), "cancelled")
        self.assertIsNone(self.store.claim("tenant-a", "worker"))

    def test_running_cancel_blocks_publish_and_keeps_slot_until_ack(self):
        first = self.submit("a")
        lease = self.store.claim("tenant-a", "worker")
        self.submit("b")
        self.assertEqual(self.store.request_cancel(self.alice, first), "cancel_requested")
        with self.assertRaises(StaleLease):
            self.store.complete(lease, b"late")
        with self.assertRaises(StaleLease):
            self.store.heartbeat(lease)
        self.assertIsNone(self.store.claim("tenant-a", "other"))
        self.store.acknowledge_cancel(lease)
        self.assertEqual(self.store.get_run(self.alice, first)["state"], "cancelled")
        self.assertIsNotNone(self.store.claim("tenant-a", "other"))

    def test_cancelled_expired_run_is_retired_instead_of_redispatched(self):
        run_id = self.submit()
        lease = self.store.claim("tenant-a", "worker", lease_seconds=10)
        self.store.request_cancel(self.alice, run_id)
        self.clock.advance(10)
        self.assertIsNone(self.store.claim("tenant-a", "recovery"))
        self.assertEqual(self.store.get_run(self.alice, run_id)["state"], "cancelled")
        with self.assertRaises(StaleLease):
            self.store.acknowledge_cancel(lease)

    def test_completion_wins_before_later_cancel_and_cannot_be_overwritten(self):
        run_id = self.submit()
        lease = self.store.claim("tenant-a", "worker")
        artifact = self.store.complete(lease, b"committed")
        self.assertEqual(self.store.request_cancel(self.alice, run_id), "too_late")
        with self.assertRaises(StaleLease):
            self.store.complete(lease, b"overwrite")
        self.assertEqual(self.store.download(self.alice, artifact), b"committed")

    def test_small_artifact_and_success_are_atomic(self):
        run_id = self.submit()
        lease = self.store.claim("tenant-a", "worker")
        with self.store.connection() as db:
            db.execute("""CREATE TRIGGER reject_completion BEFORE UPDATE ON runs
                WHEN NEW.state='succeeded' BEGIN SELECT RAISE(ABORT,'injected failure'); END""")
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.complete(lease, b"must roll back")
        with self.store.connection() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM artifacts").fetchone()[0], 0)
        self.assertEqual(self.store.get_run(self.alice, run_id)["state"], "running")

    def test_artifact_outlives_workspace_and_reopen(self):
        run_id = self.submit()
        lease = self.store.claim("tenant-a", "worker")
        with tempfile.TemporaryDirectory(dir=self.tmp.name) as workspace:
            path = Path(workspace) / "draft.md"
            path.write_bytes(b"report-v1")
            artifact = self.store.complete(lease, path.read_bytes())
        self.assertFalse(Path(workspace).exists())
        reopened = Store(self.path, clock=self.clock)
        self.assertEqual(reopened.get_run(self.alice, run_id)["artifact_id"], artifact)
        self.assertEqual(reopened.download(self.alice, artifact), b"report-v1")

    def test_open_run_backpressure_still_allows_idempotent_lookup(self):
        bounded = Store(self.path, clock=self.clock, max_open_runs=1)
        run_id, _ = bounded.submit(self.alice, self.session, "one", {"topic": "weekly"})
        self.assertEqual(bounded.submit(self.alice, self.session, "one", {"topic": "weekly"}),
                         (run_id, False))
        with self.assertRaises(Busy):
            bounded.submit(self.alice, self.session, "two", {"topic": "weekly"})

    def test_payload_and_artifact_size_contracts(self):
        for payload in ({"topic": ""}, {"topic": "x", "tenant_id": "other"}, {"topic": 1}):
            with self.assertRaises(Rejected):
                self.store.submit(self.alice, self.session, "one", payload)
        self.submit()
        lease = self.store.claim("tenant-a", "worker")
        with self.assertRaises(Rejected):
            self.store.complete(lease, b"x" * 65537)
        self.assertEqual(self.store.get_run(self.alice, lease.run_id)["state"], "running")


if __name__ == "__main__":
    unittest.main()
