"""Cross-process crash tests verify durable rows and actual provider invocations."""
from pathlib import Path
import sqlite3
import tempfile
import unittest

import lab


class DurableExecutionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="durable-test-")
        self.addCleanup(self.directory.cleanup)
        self.runtime = Path(self.directory.name) / "runtime.db"
        self.provider = Path(self.directory.name) / "provider.db"
        lab.initialize(self.runtime, self.provider)
        self.run = "run-1"
        self.payload = {"title": "开通测试环境", "scope": "test"}
        self.digest = lab.create_run(self.runtime, self.run, self.payload)

    def approve(self):
        return lab.approve(self.runtime, self.run, "approval-1", self.digest)

    def state(self):
        return lab.snapshot(self.runtime, self.run)

    def counts(self):
        return lab.provider_stats(self.provider)

    def crash(self, boundary):
        result = lab.launch_worker(self.runtime, self.provider, self.run, boundary)
        self.assertEqual(result.returncode, lab.CRASH_EXIT, result.stderr)

    def test_crash_before_dispatch_preserves_identity_and_executes_once(self):
        self.approve()
        before = self.state()
        self.crash("prepared")
        self.assertEqual(self.state()["action_status"], "PREPARED")
        self.assertEqual(self.counts()["create_calls"], 0)
        self.assertEqual(lab.advance(self.runtime, self.provider, self.run), "COMPLETED")
        after = self.state()
        self.assertEqual(after["operation_key"], before["operation_key"])
        self.assertEqual(after["action_id"], before["action_id"])
        self.assertEqual(after["action_status"], "SUCCEEDED")
        self.assertEqual(self.counts(), {"tickets": 1, "create_calls": 1, "lookup_calls": 0})

    def test_provider_commit_without_local_receipt_recovers_by_lookup_only(self):
        self.approve()
        before = self.state()
        self.crash("after_provider")
        crashed = self.state()
        self.assertEqual(crashed["action_status"], "DISPATCHING")
        self.assertIsNone(crashed["receipt"])
        self.assertEqual(self.counts(), {"tickets": 1, "create_calls": 1, "lookup_calls": 0})
        self.assertEqual(lab.advance(self.runtime, self.provider, self.run), "COMPLETED")
        self.assertEqual(self.state()["operation_key"], before["operation_key"])
        self.assertEqual(self.state()["action_status"], "SUCCEEDED")
        self.assertIsNotNone(self.state()["receipt"])
        self.assertEqual(self.counts(), {"tickets": 1, "create_calls": 1, "lookup_calls": 1})
        with sqlite3.connect(self.provider) as db:
            queried = db.execute("SELECT operation_key FROM lookup_calls").fetchone()[0]
        self.assertEqual(queried, before["operation_key"])

    def test_waiting_approval_survives_process_exit_without_a_write(self):
        self.crash("waiting")
        self.assertEqual(self.state()["run_status"], "WAITING_APPROVAL")
        self.assertIsNone(self.state()["action_id"])
        self.assertEqual(lab.advance(self.runtime, self.provider, self.run), "WAITING_APPROVAL")
        self.assertEqual(self.counts()["create_calls"], 0)
        self.approve()
        self.assertEqual(lab.advance(self.runtime, self.provider, self.run), "COMPLETED")
        self.assertEqual(self.counts()["tickets"], 1)

    def test_ambiguous_dispatch_with_no_receipt_requires_human_and_never_replays(self):
        self.approve()
        self.crash("dispatching")
        self.assertEqual(self.state()["action_status"], "DISPATCHING")
        for _ in range(2):
            self.assertEqual(lab.advance(self.runtime, self.provider, self.run), "HUMAN_REQUIRED")
            self.assertEqual(self.state()["action_status"], "UNKNOWN")
            self.assertEqual(self.state()["run_status"], "HUMAN_REQUIRED")
        self.assertEqual(self.counts(), {"tickets": 0, "create_calls": 0, "lookup_calls": 2})

    def test_duplicate_approval_is_consumed_once(self):
        self.assertEqual(self.approve(), "ACCEPTED")
        version = self.state()["version"]
        self.assertEqual(self.approve(), "DUPLICATE")
        self.assertEqual(self.state()["version"], version)
        with sqlite3.connect(self.runtime) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM inbox").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM actions").fetchone()[0], 1)
        lab.advance(self.runtime, self.provider, self.run)
        self.assertEqual(self.counts()["create_calls"], 1)

    def test_approval_for_previous_payload_is_rejected(self):
        revised = {**self.payload, "scope": "staging"}
        self.assertTrue(lab.revise_payload(self.runtime, self.run, 0, revised))
        self.assertEqual(self.approve(), "STALE_APPROVAL")
        self.assertEqual(self.state()["run_status"], "WAITING_APPROVAL")
        self.assertIsNone(self.state()["action_id"])
        self.assertEqual(self.counts()["create_calls"], 0)
        result = lab.approve(self.runtime, self.run, "approval-2", lab.payload_hash(revised))
        self.assertEqual(result, "ACCEPTED")
        lab.advance(self.runtime, self.provider, self.run)
        self.assertEqual(self.state()["payload_hash"], lab.payload_hash(revised))
        self.assertEqual(self.counts()["tickets"], 1)

    def test_cas_rejects_stale_revision_without_overwriting(self):
        revised = {**self.payload, "scope": "staging"}
        self.assertTrue(lab.revise_payload(self.runtime, self.run, 0, revised))
        self.assertFalse(lab.revise_payload(self.runtime, self.run, 0, {"scope": "prod"}))
        self.assertEqual(self.state()["version"], 1)
        self.assertEqual(self.state()["payload_hash"], lab.payload_hash(revised))

    def test_confirmed_success_is_reused_without_provider_calls(self):
        self.approve()
        lab.advance(self.runtime, self.provider, self.run)
        before = self.state()
        for _ in range(3):
            self.assertEqual(lab.advance(self.runtime, self.provider, self.run), "COMPLETED")
        self.assertEqual(self.state(), before)
        self.assertEqual(self.counts(), {"tickets": 1, "create_calls": 1, "lookup_calls": 0})

    def test_payload_cannot_change_after_approval(self):
        self.approve()
        version = self.state()["version"]
        self.assertFalse(lab.revise_payload(self.runtime, self.run, version, {"scope": "prod"}))
        self.assertEqual(self.state()["payload_hash"], self.digest)

    def test_mismatched_provider_payload_cannot_mark_action_successful(self):
        self.approve()
        self.crash("dispatching")
        lab.provider_create(self.provider, self.state()["operation_key"], "different-payload")
        self.assertEqual(lab.advance(self.runtime, self.provider, self.run), "HUMAN_REQUIRED")
        self.assertEqual(self.state()["action_status"], "UNKNOWN")
        self.assertIsNone(self.state()["receipt"])
        self.assertEqual(self.counts(), {"tickets": 1, "create_calls": 1, "lookup_calls": 1})


if __name__ == "__main__":
    unittest.main()
