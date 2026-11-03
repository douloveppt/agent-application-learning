from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from lab import (ArtifactRejected, Context, Denied, FakeRuntime, PERMISSION,
                 PROFILE, collect_report, execute, prepare, read_artifact)


class SandboxLabTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ch18-test-")
        self.addCleanup(self.temp.cleanup)
        self.output = Path(self.temp.name)
        self.context = Context("tenant-a", "employee-7", frozenset({PERMISSION}))
        self.arguments = {"profile": "analysis-v1", "recipe": "vpn-log-summary"}

    def task(self):
        return prepare(self.context, "tenant-a", self.arguments)

    def run_task(self, runtime):
        task = self.task()
        execute(self.context, task, runtime, self.output)
        return task

    def test_identity_and_task_id_come_from_host(self):
        first, second = self.task(), self.task()
        self.assertEqual((first.tenant_id, first.user_id), ("tenant-a", "employee-7"))
        self.assertNotEqual(first.task_id, second.task_id)

    def test_cross_tenant_preparation_denied(self):
        with self.assertRaises(Denied):
            prepare(self.context, "tenant-b", self.arguments)

    def test_permission_missing_denied(self):
        with self.assertRaises(Denied):
            prepare(Context("tenant-a", "employee-7", frozenset()), "tenant-a", self.arguments)

    def test_model_cannot_override_identity_network_mount_or_budget(self):
        for field, value in (("tenant_id", "tenant-b"), ("task_id", "chosen"),
                             ("network", "host"), ("mount", "/"), ("wall_seconds", 1000)):
            with self.subTest(field=field), self.assertRaises(Denied):
                prepare(self.context, "tenant-a", {**self.arguments, field: value})

    def test_unadmitted_profile_and_recipe_denied(self):
        for arguments in ({"profile": "privileged", "recipe": "vpn-log-summary"},
                          {"profile": "analysis-v1", "recipe": "shell"}, {}):
            with self.subTest(arguments=arguments), self.assertRaises(Denied):
                prepare(self.context, "tenant-a", arguments)

    def test_other_tenant_or_user_cannot_start_task(self):
        for tenant, user in (("tenant-b", "employee-7"), ("tenant-a", "employee-8")):
            runtime = FakeRuntime()
            with self.subTest(tenant=tenant, user=user), self.assertRaises(Denied):
                execute(Context(tenant, user, frozenset({PERMISSION})), self.task(), runtime, self.output)
            self.assertEqual(runtime.calls, [])

    def test_success_after_stop_collection_destroy_with_untrusted_label(self):
        runtime = FakeRuntime()
        task = self.run_task(runtime)
        self.assertEqual(task.status, "succeeded")
        self.assertEqual(runtime.calls, ["start", "wait", "stop", "destroy"])
        self.assertEqual(task.artifacts[0]["trust"], "untrusted_content")
        self.assertEqual(task.artifacts[0]["sha256"], hashlib.sha256(runtime.artifact).hexdigest())

    def test_failed_timeout_cancelled_never_collect_outputs(self):
        for outcome in ("failed", "timed_out", "cancelled"):
            with self.subTest(outcome=outcome), patch("lab.collect_report") as collector:
                runtime = FakeRuntime(outcome=outcome)
                task = self.run_task(runtime)
                self.assertEqual(task.status, outcome)
                self.assertEqual(task.artifacts, [])
                self.assertEqual(runtime.calls[-2:], ["stop", "destroy"])
                collector.assert_not_called()

    def test_unconfirmed_stop_never_collects_or_publishes(self):
        runtime = FakeRuntime(stop_confirmed=False)
        with patch("lab.collect_report") as collector:
            task = self.run_task(runtime)
        collector.assert_not_called()
        self.assertEqual(task.status, "cleanup_pending")
        self.assertEqual(task.artifacts, [])
        self.assertNotIn("destroy", runtime.calls)

    def test_unconfirmed_destroy_discards_staged_output(self):
        task = self.run_task(FakeRuntime(destroy_confirmed=False))
        self.assertEqual(task.status, "cleanup_pending")
        self.assertEqual(task.execution_outcome, "ok")
        self.assertEqual(task.artifacts, [])

    def test_stop_exception_is_cleanup_pending(self):
        runtime = FakeRuntime()
        with patch.object(runtime, "stop", side_effect=OSError("daemon offline")):
            task = self.run_task(runtime)
        self.assertEqual(task.status, "cleanup_pending")
        self.assertEqual(task.artifacts, [])

    def test_destroy_exception_is_cleanup_pending(self):
        runtime = FakeRuntime()
        with patch.object(runtime, "destroy", side_effect=OSError("daemon offline")):
            task = self.run_task(runtime)
        self.assertEqual(task.status, "cleanup_pending")
        self.assertEqual(task.artifacts, [])

    def test_start_unknown_is_not_success_and_still_cleans_up(self):
        runtime = FakeRuntime()
        with patch.object(runtime, "start", side_effect=TimeoutError("creation result unknown")):
            task = self.run_task(runtime)
        self.assertEqual(task.status, "unknown")
        self.assertEqual(runtime.calls, ["stop", "destroy"])
        self.assertEqual(task.artifacts, [])

    def test_same_task_cannot_be_blindly_replayed(self):
        runtime = FakeRuntime(destroy_confirmed=False)
        task = self.run_task(runtime)
        previous = runtime.calls[:]
        with self.assertRaises(Denied):
            execute(self.context, task, runtime, self.output)
        self.assertEqual(runtime.calls, previous)

    def test_bad_schema_rejected_and_runtime_destroyed(self):
        for payload in (b"not JSON", b'{"summary":"bad"}',
                        b'{"summary":"bad","error_count":true}',
                        b'{"summary":"bad","error_count":0,"token":"extra"}'):
            with self.subTest(payload=payload):
                runtime = FakeRuntime(artifact=payload)
                task = self.run_task(runtime)
                self.assertEqual(task.status, "artifact_rejected")
                self.assertEqual(task.artifacts, [])
                self.assertEqual(runtime.calls[-1], "destroy")

    def test_artifact_size_limit(self):
        task = self.run_task(FakeRuntime(artifact=b"x" * (PROFILE.output_bytes + 1)))
        self.assertEqual(task.status, "artifact_rejected")

    def test_deep_json_rejected_and_destroy_still_attempted(self):
        payload = b"[" * 1500 + b"0" + b"]" * 1500
        self.assertLess(len(payload), PROFILE.output_bytes)
        runtime = FakeRuntime(artifact=payload)
        task = self.run_task(runtime)
        self.assertEqual(task.status, "artifact_rejected")
        self.assertEqual(task.artifacts, [])
        self.assertEqual(runtime.calls[-1], "destroy")

    def test_unexpected_collector_error_still_destroys_and_does_not_publish(self):
        runtime = FakeRuntime()
        with patch("lab.collect_report", side_effect=RuntimeError("collector fault")):
            task = self.run_task(runtime)
        self.assertEqual(task.status, "artifact_collection_failed")
        self.assertEqual(task.artifacts, [])
        self.assertEqual(runtime.calls[-1], "destroy")

    def test_output_enumeration_stops_at_budget_plus_one_and_closes(self):
        class BoundedScan:
            reads = 0
            closed = False

            def __enter__(self):
                return self

            def __exit__(self, *args):
                self.closed = True

            def __iter__(self):
                return self

            def __next__(self):
                self.reads += 1
                if self.reads > PROFILE.output_files + 1:
                    raise AssertionError("unbounded enumeration")
                return SimpleNamespace(name="entry-" + str(self.reads))

        scanner = BoundedScan()
        with patch("lab.os.scandir", return_value=scanner), self.assertRaises(ArtifactRejected):
            collect_report(self.output, PROFILE)
        self.assertEqual(scanner.reads, PROFILE.output_files + 1)
        self.assertTrue(scanner.closed)

    def test_unexpected_output_file_rejected(self):
        (self.output / "extra.txt").write_text("unexpected")
        task = self.run_task(FakeRuntime())
        self.assertEqual(task.status, "artifact_rejected")
        self.assertEqual(task.artifacts, [])

    def test_absolute_traversal_and_ambiguous_paths_rejected(self):
        for name in ("/etc/passwd", "../secret", "a/../../secret", "./report.json", "a//b", "a\\b", ""):
            with self.subTest(name=name), self.assertRaises(ArtifactRejected):
                read_artifact(self.output, name, byte_limit=4096)

    def test_symbolic_link_leaf_rejected(self):
        with tempfile.TemporaryDirectory() as elsewhere:
            secret = Path(elsewhere) / "secret"
            secret.write_text("do not export")
            (self.output / "report.json").symlink_to(secret)
            with self.assertRaises(ArtifactRejected):
                collect_report(self.output, PROFILE)

    def test_symbolic_link_parent_rejected(self):
        with tempfile.TemporaryDirectory() as elsewhere:
            (Path(elsewhere) / "secret").write_text("do not export")
            (self.output / "linked").symlink_to(elsewhere, target_is_directory=True)
            with self.assertRaises(ArtifactRejected):
                read_artifact(self.output, "linked/secret", byte_limit=4096)

    def test_symbolic_link_root_rejected(self):
        root_link = self.output / "link"
        root_link.symlink_to(self.output, target_is_directory=True)
        with self.assertRaises(ArtifactRejected):
            read_artifact(root_link, "report.json", byte_limit=4096)

    def test_hard_link_rejected(self):
        with tempfile.TemporaryDirectory() as elsewhere:
            secret = Path(elsewhere) / "secret"
            secret.write_text("do not export")
            os.link(secret, self.output / "report.json")
            with self.assertRaises(ArtifactRejected):
                collect_report(self.output, PROFILE)

    def test_non_regular_file_rejected_without_blocking(self):
        os.mkfifo(self.output / "report.json")
        with self.assertRaises(ArtifactRejected):
            collect_report(self.output, PROFILE)


if __name__ == "__main__":
    unittest.main()
