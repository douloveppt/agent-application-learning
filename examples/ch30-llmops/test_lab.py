from dataclasses import FrozenInstanceError, asdict, replace
import hashlib
import unittest

from lab import (Controller, OfflineEvidence, Observation, Rejected, Release,
                 Resources, bucket, canonical, check_compatibility, demo,
                 evaluate, fixtures)


class ReleaseLabTests(unittest.TestCase):
    def setUp(self):
        self.old, self.new = fixtures()
        self.controller = Controller(self.old)

    def stage(self):
        self.controller.stage(self.new, OfflineEvidence(self.new.digest, True), expected=0)

    def rows(self, **changes):
        return [Observation(self.new.digest, f"run-{number}", **changes) for number in range(40)]

    def test_manifest_digest_is_canonical_and_content_sensitive(self):
        data = asdict(self.new)
        reversed_data = dict(reversed(list(data.items())))
        self.assertEqual(canonical(data), canonical(reversed_data))
        self.assertEqual(self.new.digest, hashlib.sha256(canonical(data)).hexdigest())
        self.assertNotEqual(self.new.digest, replace(self.new, prompt="prompt-changed").digest)

    def test_manifest_is_immutable_and_rejects_mutable_readers(self):
        with self.assertRaises(FrozenInstanceError):
            self.new.prompt = "overwrite"
        with self.assertRaises(Rejected):
            replace(self.new, state_readers=["state-v1"])

    def test_offline_failure_or_wrong_release_never_starts_canary(self):
        for evidence in (OfflineEvidence(self.new.digest, False),
                         OfflineEvidence(self.new.digest, "false"),
                         OfflineEvidence(self.old.digest, True)):
            with self.subTest(evidence=evidence), self.assertRaises(Rejected):
                self.controller.stage(self.new, evidence, expected=0)
            self.assertEqual(self.controller.generation, 0)
            self.assertIsNone(self.controller.candidate)

    def test_reusing_release_id_with_different_content_is_rejected(self):
        self.stage()
        self.controller.decide(self.rows(unauthorized=True), expected=1)
        changed = replace(self.new, prompt="silent-overwrite")
        with self.assertRaisesRegex(Rejected, "immutable"):
            self.controller.stage(changed, OfflineEvidence(changed.digest, True), expected=2)

    def test_stale_generation_cannot_overwrite_newer_pointer(self):
        self.stage()
        self.controller.decide(self.rows(), expected=1)
        with self.assertRaisesRegex(Rejected, "stale generation"):
            self.controller.rollback(self.old.digest, expected=1)
        self.assertEqual(self.controller.stable, self.new.digest)
        self.assertEqual(self.controller.generation, 2)

    def test_incompatible_candidate_cannot_start(self):
        incompatible = replace(self.new, index="missing-index")
        with self.assertRaisesRegex(Rejected, "recovery plan"):
            self.controller.stage(incompatible, OfflineEvidence(incompatible.digest, True), expected=0)
        self.assertIsNone(self.controller.candidate)

    def test_bucket_is_stable_and_tuple_unambiguous(self):
        self.assertEqual(bucket("tenant-a", "task-7"), bucket("tenant-a", "task-7"))
        self.assertNotEqual(canonical(["ab", "c"]), canonical(["a", "bc"]))
        self.assertTrue(0 <= bucket("tenant-a", "task-7") < 10_000)

    def test_task_keeps_release_through_promotion_and_rollback(self):
        self.stage()
        task_id = next(f"task-{n}" for n in range(1_000) if bucket("tenant-a", f"task-{n}") < 1_000)
        original = self.controller.admit("tenant-a", task_id)
        self.assertEqual(original.release_digest, self.new.digest)
        with self.assertRaises(FrozenInstanceError):
            original.release_digest = self.old.digest
        self.controller.decide(self.rows(), expected=1)
        self.controller.rollback(self.old.digest, expected=2)
        self.assertIs(original, self.controller.admit("tenant-a", task_id))
        self.assertEqual(self.controller.resume("tenant-a", task_id).release_digest, self.new.digest)
        self.assertEqual(self.controller.admit("tenant-a", "new-task").release_digest, self.old.digest)

    def test_same_task_name_in_two_tenants_is_distinct(self):
        first = self.controller.admit("tenant-a", "vpn-1")
        second = self.controller.admit("tenant-b", "vpn-1")
        self.assertIsNot(first, second)
        self.assertEqual(len(self.controller.tasks), 2)

    def test_insufficient_observations_hold_without_pointer_mutation(self):
        self.stage()
        gate = self.controller.decide(self.rows()[:5], expected=1)
        self.assertEqual(gate.decision, "hold")
        self.assertEqual(self.controller.generation, 1)
        self.assertEqual(self.controller.stable, self.old.digest)

    def test_unsafe_observation_stops_even_below_sample_minimum(self):
        for change in ({"unauthorized": True}, {"unknown_write": True}):
            with self.subTest(change=change):
                self.assertEqual(evaluate(self.new, self.rows(**change)[:1]).decision, "stop")

    def test_failed_quality_latency_and_cost_stop(self):
        for change in ({"success": False}, {"latency_ms": 2_501}, {"cost_microusd": 40_001}):
            with self.subTest(change=change):
                self.assertEqual(evaluate(self.new, self.rows(**change)).decision, "stop")
        self.assertEqual(evaluate(self.new, self.rows(latency_ms=2_500, cost_microusd=40_000)).decision,
                         "promote")

    def test_duplicate_or_wrong_release_observations_are_rejected(self):
        with self.assertRaisesRegex(Rejected, "duplicate"):
            evaluate(self.new, [self.rows()[0]] * 40)
        with self.assertRaisesRegex(Rejected, "different release"):
            evaluate(self.old, self.rows())

    def test_stop_removes_candidate_without_changing_stable(self):
        self.stage()
        # Even an incompatible runtime must be able to stop new candidate admissions.
        self.controller.resources = Resources(indexes=("index-v1",))
        gate = self.controller.decide(self.rows(unauthorized=True)[:1], expected=1)
        self.assertEqual(gate.decision, "stop")
        self.assertEqual(self.controller.stable, self.old.digest)
        self.assertIsNone(self.controller.candidate)
        self.assertEqual(self.controller.weight_bps, 0)

    def test_rollback_checks_state_index_and_tool_compatibility(self):
        environments = (Resources(state_format="state-v2"), Resources(indexes=("index-v2",)),
                        Resources(tool_schemas=("ticket-v2",)))
        for resources in environments:
            with self.subTest(resources=resources), self.assertRaisesRegex(Rejected, "recovery plan"):
                check_compatibility(self.old, resources)
        self.stage()
        self.controller.decide(self.rows(), expected=1)
        self.controller.resources = environments[0]
        with self.assertRaises(Rejected):
            self.controller.rollback(self.old.digest, expected=2)
        self.assertEqual(self.controller.stable, self.new.digest)
        self.assertEqual(self.controller.generation, 2)

    def test_unknown_write_cannot_be_replayed_after_rollback(self):
        self.stage()
        self.controller.decide(self.rows(), expected=1)
        task = self.controller.admit("tenant-a", "write-task")
        self.controller.dispatch_write("tenant-a", "write-task", "operation-1", lose_receipt=True)
        self.controller.rollback(self.old.digest, expected=2)
        with self.assertRaisesRegex(Rejected, "reconcile"):
            self.controller.dispatch_write("tenant-a", "write-task", "operation-1", lose_receipt=False)
        self.assertEqual(task.operations["operation-1"], "unknown")
        self.assertEqual(task.write_dispatches, 1)

    def test_resume_fails_if_pinned_task_state_is_unreadable(self):
        task = self.controller.admit("tenant-a", "task-1")
        self.controller.tasks[("tenant-a", "task-1")] = replace(task, state_format="state-v99")
        with self.assertRaisesRegex(Rejected, "task state"):
            self.controller.resume("tenant-a", "task-1")

    def test_metrics_reject_nonfinite_negative_and_boolean_numbers(self):
        for change in ({"latency_ms": -1}, {"latency_ms": float("nan")},
                       {"cost_microusd": True}, {"success": "yes"}):
            with self.subTest(change=change), self.assertRaises(Rejected):
                Observation(self.new.digest, "run-invalid", **change)

    def test_demo_has_expected_decisions_and_invariants(self):
        report = demo()
        self.assertEqual([gate["decision"] for gate in report["gates"]], ["hold", "promote", "stop"])
        self.assertTrue(all(report["assertions"].values()))
        self.assertEqual([event["generation"] for event in report["events"]], [1, 2, 3])


if __name__ == "__main__":
    unittest.main()
