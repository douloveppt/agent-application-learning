"""Tests for scope, independent behavior checks and evidence freshness."""
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest

from lab import (AcceptanceHarness, BASE_REVISION, Environment, FIXED_SOURCE,
                 Policy, Rejected, Workspace, demo, digest, snapshot,
                 run_external_suite, source_variant, tree_digest, write_workspace)


class HarnessTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="ch33-test-")
        self.root = Path(self.temporary.name)
        self.harness = AcceptanceHarness()

    def tearDown(self):
        self.temporary.cleanup()

    def assess(self, scenario: str = "fixed", environment: Environment = Environment(),
               revision: str = BASE_REVISION):
        workspace = write_workspace(self.root / scenario, scenario, revision)
        return workspace, self.harness.assess(workspace, environment)

    def test_known_defect_is_reproduced_as_baseline_not_new_regression(self):
        _, report = self.assess("faulty")
        self.assertEqual("known_baseline_failure_reproduced", report["status"])
        self.assertEqual(1, report["report_artifact"]["exit_code"])
        acceptance = next(check for check in report["checks"]
                          if check["name"] == "external_acceptance")
        self.assertFalse(acceptance["detail"]["same_tenant_other_hidden"])

    def test_fixed_candidate_is_ready_with_independent_four_case_suite(self):
        workspace, report = self.assess()
        self.assertEqual("ready_for_review", report["status"])
        acceptance = next(check for check in report["checks"]
                          if check["name"] == "external_acceptance")
        self.assertEqual(4, len(acceptance["detail"]))
        self.assertTrue(all(acceptance["detail"].values()))
        self.assertTrue(self.harness.verify_receipt(report, workspace, Environment())["valid"])

    def test_same_tenant_other_case_uniquely_catches_missing_owner_check(self):
        observations = run_external_suite("faulty")["observations"]
        self.assertEqual({"same_tenant_other_hidden"},
                         {name for name, passed in observations.items() if not passed})

    def test_cross_tenant_case_uniquely_catches_missing_tenant_check(self):
        observations = run_external_suite("_owner_only")["observations"]
        self.assertEqual({"cross_tenant_hidden"},
                         {name for name, passed in observations.items() if not passed})

    def test_own_and_missing_cases_each_reject_a_distinct_bad_model(self):
        controls = {
            "_deny_all": "own_ticket_visible",
            "_fabricate_missing": "missing_hidden",
        }
        for variant, expected_failure in controls.items():
            with self.subTest(variant=variant):
                observations = run_external_suite(variant)["observations"]
                self.assertEqual({expected_failure},
                                 {name for name, passed in observations.items() if not passed})

    def test_test_tampering_fails_before_functional_acceptance(self):
        _, report = self.assess("test_tamper")
        self.assertEqual("policy_rejected", report["status"])
        self.assertNotIn("external_acceptance", {check["name"] for check in report["checks"]})
        self.assertEqual("failed", next(check for check in report["checks"]
                                        if check["name"] == "protected_tests")["status"])

    def test_out_of_scope_dependency_change_is_rejected(self):
        _, report = self.assess("out_of_scope")
        self.assertEqual("policy_rejected", report["status"])
        self.assertIn("project.toml", report["changed_paths"])
        self.assertEqual("failed", next(check for check in report["checks"]
                                        if check["name"] == "change_scope")["status"])

    def test_environment_failure_is_blocked_not_product_failure(self):
        environment = Environment(ready=False, failure="synthetic image unavailable")
        _, report = self.assess(environment=environment)
        self.assertEqual("blocked_environment", report["status"])
        self.assertEqual(2, report["report_artifact"]["exit_code"])
        self.assertEqual(["environment"], [check["name"] for check in report["checks"]])

    def test_unexpected_base_revision_is_rejected(self):
        _, report = self.assess(revision="different-base")
        self.assertEqual("policy_rejected", report["status"])
        self.assertEqual("failed", next(check for check in report["checks"]
                                        if check["name"] == "base_revision")["status"])

    def test_unknown_source_is_not_executed_and_is_rejected(self):
        workspace = write_workspace(self.root / "unknown", "fixed")
        (workspace.root / "src/service.py").write_text(
            "raise RuntimeError('must never execute candidate source')\n", encoding="utf-8")
        report = self.harness.assess(workspace, Environment())
        self.assertEqual("policy_rejected", report["status"])
        with self.assertRaisesRegex(Rejected, "reviewed teaching template"):
            source_variant(workspace)

    def test_missing_required_source_returns_structured_policy_rejection(self):
        workspace = write_workspace(self.root / "missing-source", "fixed")
        (workspace.root / "src/service.py").unlink()
        report = self.harness.assess(workspace, Environment())
        self.assertEqual("policy_rejected", report["status"])
        template = next(check for check in report["checks"]
                        if check["name"] == "reviewed_template")
        self.assertEqual("failed", template["status"])
        self.assertIn("src/service.py", report["changed_paths"])

    def test_non_utf8_required_source_returns_structured_policy_rejection(self):
        workspace = write_workspace(self.root / "non-utf8-source", "fixed")
        (workspace.root / "src/service.py").write_bytes(b"\xff\xfe\x00")
        report = self.harness.assess(workspace, Environment())
        self.assertEqual("policy_rejected", report["status"])
        template = next(check for check in report["checks"]
                        if check["name"] == "reviewed_template")
        self.assertEqual("failed", template["status"])
        self.assertEqual(1, report["report_artifact"]["exit_code"])

    def test_unlisted_file_changes_full_tree_and_scope(self):
        workspace = write_workspace(self.root / "addition", "fixed")
        addition = workspace.root / "notes.txt"
        addition.write_text("unexpected generated file\n", encoding="utf-8")
        report = self.harness.assess(workspace, Environment())
        self.assertIn("notes.txt", report["changed_paths"])
        self.assertEqual("policy_rejected", report["status"])

    def test_source_change_after_assessment_invalidates_receipt(self):
        workspace, report = self.assess()
        (workspace.root / "src/service.py").write_text(FIXED_SOURCE + "\n", encoding="utf-8")
        verification = self.harness.verify_receipt(report, workspace, Environment())
        self.assertFalse(verification["valid"])
        self.assertIn("tree_digest changed", verification["reasons"])

    def test_environment_change_invalidates_receipt(self):
        workspace, report = self.assess()
        changed = replace(Environment(), image="python:3.11-other@sha256:synthetic")
        verification = self.harness.verify_receipt(report, workspace, changed)
        self.assertIn("environment_digest changed", verification["reasons"])

    def test_policy_change_invalidates_receipt(self):
        workspace, report = self.assess()
        verifier = AcceptanceHarness(Policy(max_changed_files=2))
        verification = verifier.verify_receipt(report, workspace, Environment())
        self.assertIn("policy_digest changed", verification["reasons"])

    def test_missing_evidence_fails_closed(self):
        workspace, report = self.assess()
        broken = deepcopy(report)
        del broken["evidence"]["check_suite_digest"]
        broken["report_artifact"]["sha256"] = digest(
            {key: value for key, value in broken.items() if key != "report_artifact"})
        verification = self.harness.verify_receipt(broken, workspace, Environment())
        self.assertIn("missing evidence fields", verification["reasons"])

    def test_report_modification_breaks_report_digest(self):
        workspace, report = self.assess()
        altered = deepcopy(report)
        altered["completion_boundary"] = "merge approved"
        verification = self.harness.verify_receipt(altered, workspace, Environment())
        self.assertIn("report digest mismatch", verification["reasons"])

    def test_non_ready_report_cannot_be_reused_as_ready_evidence(self):
        workspace, report = self.assess("faulty")
        verification = self.harness.verify_receipt(report, workspace, Environment())
        self.assertFalse(verification["valid"])
        self.assertIn("report is not review-ready", verification["reasons"])

    def test_source_artifacts_have_relative_path_hash_and_exit_code(self):
        _, report = self.assess()
        self.assertEqual({"project.toml", "src/service.py", "tests/test_access.py"},
                         {item["path"] for item in report["source_artifacts"]})
        self.assertTrue(all(len(item["sha256"]) == 64 for item in report["source_artifacts"]))
        self.assertEqual(0, report["report_artifact"]["exit_code"])

    def test_tree_digest_changes_when_protected_test_changes(self):
        workspace = write_workspace(self.root / "tree", "fixed")
        before = tree_digest(snapshot(workspace.root))
        (workspace.root / "tests/test_access.py").write_text("CASES = ()\n", encoding="utf-8")
        self.assertNotEqual(before, tree_digest(snapshot(workspace.root)))

    def test_demo_exposes_all_expected_outcomes_and_stale_evidence(self):
        result = demo()
        self.assertEqual("known_baseline_failure_reproduced", result["faulty"]["status"])
        self.assertEqual("ready_for_review", result["fixed"]["status"])
        self.assertEqual("policy_rejected", result["test_tamper"]["status"])
        self.assertEqual("policy_rejected", result["out_of_scope"]["status"])
        self.assertEqual("blocked_environment", result["environment_failure"]["status"])
        self.assertFalse(result["stale_evidence"]["valid"])


if __name__ == "__main__":
    unittest.main()
