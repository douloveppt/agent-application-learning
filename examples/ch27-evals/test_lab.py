import copy
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import lab


class EvalHarnessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.version, cls.digest, cls.cases = lab.load_dataset()
        cls.by_id = {c.id: c for c in cls.cases}

    def candidate(self, key):
        result = lab.evaluate_case(self.by_id[key], "candidate")
        self.assertEqual("PASS", result["status"])
        return result

    def regrade(self, key, result):
        return lab.grade(self.by_id[key], result["output"], result["evidence"])

    def test_policies_demonstrate_gain_and_hard_safety_gate(self):
        reports = {p: lab.run_suite(self.cases, p, self.version, self.digest)
                   for p in ("baseline", "candidate", "unsafe")}
        self.assertEqual([7, 10, 9], [reports[p]["summary"]["passed"] for p in reports])
        self.assertFalse(reports["baseline"]["gate"]["passed"])
        self.assertTrue(reports["candidate"]["gate"]["passed"])
        self.assertIn("critical_violation", reports["unsafe"]["gate"]["reasons"])
        paired = lab.paired_report(reports["baseline"], reports["candidate"])
        self.assertEqual({"unchanged": 7, "improved": 3}, paired["counts"])

    def test_success_claim_without_business_row_is_rejected(self):
        r = self.candidate("create")
        r["evidence"]["tickets"] = [t for t in r["evidence"]["tickets"] if t["id"] == "foreign-1"]
        self.assertIn("success_without_verified_state", self.regrade("create", r)["critical"])

    def test_wrong_receipt_reference_is_rejected(self):
        r = self.candidate("create")
        r["output"]["ticket_id"] = "foreign-1"
        self.assertIn("success_without_verified_state", self.regrade("create", r)["critical"])

    def test_missing_approval_is_blocked_even_when_tool_prevents_write(self):
        r = lab.evaluate_case(self.by_id["approval"], "unsafe")
        self.assertEqual("FAIL", r["status"])
        self.assertIn("unapproved_attempt", r["critical"])
        self.assertEqual(1, len(r["evidence"]["tickets"]))  # Only the seeded foreign row.

    def test_empty_or_truncated_trace_cannot_erase_unsafe_attempt(self):
        original = lab.evaluate_case(self.by_id["approval"], "unsafe")
        for trace in ([], original["evidence"]["trace"][:-1],
                      [e for e in original["evidence"]["trace"] if e["kind"] != "blocked"]):
            with self.subTest(trace_length=len(trace)):
                result = copy.deepcopy(original)
                result["evidence"]["trace"] = trace
                with self.assertRaises(lab.HarnessError):
                    self.regrade("approval", result)
                checked = lab.gate(self.cases, [result])
                self.assertIn("infrastructure_or_evidence_error", checked["reasons"])

    def test_missing_authorization_fails_even_with_resealed_trace(self):
        r = self.candidate("create")
        trace = [e for e in r["evidence"]["trace"] if e["kind"] != "authorized"]
        for index, event in enumerate(trace):
            event["seq"] = index
        r["evidence"]["trace"] = trace
        r["evidence"]["trace_meta"] = lab.trace_metadata(trace)
        self.assertIn("write_without_prior_authorization", self.regrade("create", r)["critical"])

    def test_authorization_after_write_does_not_satisfy_contract(self):
        r = self.candidate("create")
        trace = r["evidence"]["trace"]
        a = next(i for i, e in enumerate(trace) if e["kind"] == "authorized")
        w = next(i for i, e in enumerate(trace) if e["kind"] == "write_observed")
        trace[a], trace[w] = trace[w], trace[a]
        for index, event in enumerate(trace):
            event["seq"] = index
        r["evidence"]["trace_meta"] = lab.trace_metadata(trace)
        self.assertIn("write_without_prior_authorization", self.regrade("create", r)["critical"])

    def test_state_mutation_cannot_hide_unauthorized_write(self):
        r = self.candidate("denied")
        r["evidence"]["tickets"].append({"id":"injected", "tenant":"tenant-a",
            "requester":"employee-a", "operation_key":"op-1", "title":"VPN 无法连接"})
        self.assertIn("unauthorized_or_unapproved_state", self.regrade("denied", r)["critical"])

    def test_cross_tenant_state_mutation_is_rejected(self):
        r = self.candidate("tenant")
        r["evidence"]["tickets"][0]["title"] = "leaked-or-modified"
        self.assertIn("cross_tenant_state_changed", self.regrade("tenant", r)["critical"])

    def test_read_tool_does_not_expose_foreign_record(self):
        case = self.by_id["tenant"]
        with tempfile.TemporaryDirectory() as tmp:
            env = lab.Environment(Path(tmp) / "business.db", case.input)
            try:
                self.assertIsNone(env.read_ticket("tenant-b", "foreign-1"))
                graded = lab.grade(case, {"status":"denied"}, env.snapshot())
                self.assertIn("cross_tenant_read_attempt", graded["critical"])
            finally:
                env.close()

    def test_denied_output_cannot_smuggle_an_extra_record(self):
        r = self.candidate("tenant")
        r["output"]["record"] = {"title":"private"}
        self.assertIn("unexpected_output_fields", self.regrade("tenant", r)["critical"])

    def test_duplicate_effect_is_rejected_despite_success_claim(self):
        r = self.candidate("duplicate")
        extra = copy.deepcopy(next(t for t in r["evidence"]["tickets"] if t["id"] == "ticket-1"))
        extra["id"] = "ticket-2"
        r["evidence"]["tickets"].append(extra)
        self.assertIn("duplicate_business_effect", self.regrade("duplicate", r)["critical"])

    def test_negative_lookup_preserves_unknown_and_forbids_rewrite(self):
        r = self.candidate("unknown_negative")
        self.assertEqual("UNKNOWN", r["evidence"]["runtime"]["state"])
        self.assertFalse(any(e["kind"] == "create_attempt" for e in r["evidence"]["trace"]))
        r["evidence"]["runtime"]["state"] = "FAILED"
        self.assertIn("unknown_erased", self.regrade("unknown_negative", r)["critical"])

    def test_lost_receipt_is_reconciled_with_one_create_attempt(self):
        r = self.candidate("lost_receipt")
        kinds = [e["kind"] for e in r["evidence"]["trace"]]
        self.assertEqual(1, kinds.count("create_attempt"))
        self.assertIn("verified_receipt", kinds)

    def test_unsupported_answer_cannot_pass_on_status_alone(self):
        r = self.candidate("no_evidence")
        r["output"] = {"status":"answered", "answer":"VPN 已恢复", "citations":["invented"]}
        self.assertIn("unsupported_answer", self.regrade("no_evidence", r)["critical"])

    def test_missing_cases_and_evidence_block_the_gate(self):
        r = lab.run_suite(self.cases, "candidate", self.version, self.digest)
        self.assertFalse(lab.gate(self.cases, r["results"][:-1])["passed"])
        del r["results"][0]["evidence"]
        checked = lab.gate(self.cases, r["results"])
        self.assertIn("infrastructure_or_evidence_error", checked["reasons"])

    def test_forged_pass_is_recomputed(self):
        r = lab.run_suite(self.cases, "unsafe", self.version, self.digest)
        row = next(x for x in r["results"] if x["case_id"] == "approval")
        row.update(status="PASS", critical=[], failures=[])
        checked = lab.gate(self.cases, r["results"])
        self.assertIn("critical_violation", checked["reasons"])
        self.assertIn("infrastructure_or_evidence_error", checked["reasons"])

    def test_harness_error_is_not_scored_as_agent_failure(self):
        with patch.object(lab.Environment, "snapshot", side_effect=OSError("fixture unavailable")):
            row = lab.evaluate_case(self.by_id["create"], "candidate")
        self.assertEqual("INFRA_ERROR", row["status"])
        self.assertEqual([], row["critical"])
        self.assertIn("infrastructure_or_evidence_error", lab.gate(self.cases, [row])["reasons"])

    def test_invalid_dataset_and_cli_infrastructure_exit(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.json"
            path.write_text(json.dumps({"dataset_version":"bad", "cases":[]}), encoding="utf-8")
            with self.assertRaises(lab.HarnessError):
                lab.load_dataset(path)
            self.assertEqual(2, lab.main(["--dataset", str(path), "--output-dir", tmp]))

    def test_compare_baseline_infrastructure_failure_returns_two(self):
        original = lab.run_policy

        def broken_baseline(policy, task, env):
            if policy == "baseline":
                raise OSError("baseline fixture unavailable")
            return original(policy, task, env)

        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(lab, "run_policy", side_effect=broken_baseline), redirect_stdout(io.StringIO()):
                self.assertEqual(2, lab.main(["--compare", "--output-dir", tmp]))
            candidate = json.loads((Path(tmp) / "candidate.json").read_text())
            self.assertTrue(candidate["gate"]["passed"])
            paired = json.loads((Path(tmp) / "paired.json").read_text())
            self.assertEqual(10, paired["counts"]["unscored"])


if __name__ == "__main__":
    unittest.main()
