"""Behavioral checks for the parent-controlled collaboration boundary."""
import asyncio
from copy import deepcopy
import unittest

from lab import Budget, Coordinator, Evidence, result_for, run_demo


class CollaborationTests(unittest.TestCase):
    def test_scenarios_reach_distinct_parent_outcomes(self):
        cases = [({}, "READY_DRAFT"),
                 ({"failed_role": "knowledge"}, "READY_DRAFT_DEGRADED"),
                 ({"failed_role": "policy"}, "BLOCKED"),
                 ({"conflict": True}, "NEEDS_REVIEW"),
                 ({"cancel_before_delivery": True}, "CANCELLED"),
                 ({"replan_before_delivery": True}, "WAITING")]
        for options, expected in cases:
            with self.subTest(options=options):
                output = asyncio.run(run_demo(**options))
                self.assertEqual(output["outcome"], expected)
                self.assertEqual(output["reserved_tokens"], 0)
                self.assertEqual(output["write_request"], "capability_denied")
                self.assertEqual(output["accepted"], 0 if expected in {"CANCELLED", "WAITING"} else 3)

    def test_child_success_is_not_parent_success(self):
        parent = Coordinator()
        contracts = [parent.dispatch(role) for role in ("knowledge", "diagnosis", "policy")]
        self.assertEqual(parent.accept(result_for(contracts[0])), "ACCEPTED")
        self.assertEqual(parent.outcome(), "WAITING")
        for contract in contracts[1:]:
            result = result_for(contract)
            result["findings"] = []
            parent.accept(result)
        self.assertEqual(parent.outcome(), "BLOCKED")

        # Multiple candidate checks are compatible; only declared single-value facts conflict.
        parent = Coordinator()
        parent.evidence["kb-4"] = Evidence(parent.tenant_id, parent.run_id,
                                          "recommended_step", "check_client_clock")
        for role in ("knowledge", "diagnosis", "policy"):
            candidate = result_for(parent.dispatch(role))
            if role == "knowledge":
                candidate["findings"].append({"claim": "recommended_step",
                                               "value": "check_client_clock",
                                               "evidence_ref": "kb-4"})
            self.assertEqual(parent.accept(candidate), "ACCEPTED")
        self.assertEqual(parent.outcome(), "READY_DRAFT")  # Root cause remains unknown.

    def test_all_identity_dimensions_are_checked(self):
        parent = Coordinator()
        candidate = result_for(parent.dispatch("knowledge"))
        for field in ("tenant_id", "run_id", "task_id", "attempt_id", "agent_id",
                      "plan_version", "input_digest"):
            altered = deepcopy(candidate)
            altered[field] = 2 if field == "plan_version" else "wrong"
            with self.subTest(field=field):
                self.assertIn(parent.accept(altered), {"CONTRACT_MISMATCH", "UNKNOWN_TASK"})
        altered = deepcopy(candidate)
        altered["plan_version"] = True  # Python's True == 1 must not bypass type checks.
        self.assertEqual(parent.accept(altered), "CONTRACT_MISMATCH")
        self.assertFalse(parent.results)

    def test_schema_and_evidence_are_not_trusted(self):
        parent = Coordinator()
        candidate = result_for(parent.dispatch("knowledge"))
        for mutate, expected in [
            (lambda c: c.update(approved=True), "INVALID_SCHEMA"),
            (lambda c: c.update(usage_tokens=True), "INVALID_SCHEMA"),
            (lambda c: c["findings"][0].update(evidence_ref="invented"), "EVIDENCE_REJECTED"),
            (lambda c: c["findings"][0].update(value="disable_security"), "EVIDENCE_REJECTED"),
        ]:
            altered = deepcopy(candidate)
            mutate(altered)
            self.assertEqual(parent.accept(altered), expected)
        parent.evidence["kb-3"] = Evidence("another-tenant", parent.run_id,
                                           "recommended_step", "check_certificate_validity")
        self.assertEqual(parent.accept(candidate), "EVIDENCE_REJECTED")

    def test_valid_evidence_cannot_cross_role_output_contract(self):
        parent = Coordinator()
        contract = parent.dispatch("knowledge")
        for claim, value, reference in (
            ("primary_error", "tls_handshake_timeout", "diag-17"),
            ("ticket_allowed", "true", "policy-4"),
        ):
            candidate = result_for(contract)
            candidate["findings"] = [{"claim": claim, "value": value,
                                       "evidence_ref": reference}]
            self.assertEqual(parent.accept(candidate), "RESULT_SCOPE_REJECTED")
            self.assertFalse(parent.results)
            self.assertEqual(parent.outcome(), "WAITING")

    def test_duplicate_cannot_overwrite_result_or_charge_again(self):
        parent = Coordinator()
        contract = parent.dispatch("knowledge")
        candidate = result_for(contract)
        parent.budget.settle(contract.attempt_id, 100)
        self.assertEqual(parent.accept(candidate), "ACCEPTED")
        self.assertEqual(parent.accept(deepcopy(candidate)), "DUPLICATE")
        self.assertEqual(parent.budget.settle(contract.attempt_id, 100), "DUPLICATE_METERING")
        altered = deepcopy(candidate)
        altered["usage_tokens"] = 101
        self.assertEqual(parent.accept(altered), "CONFLICTING_DUPLICATE")
        self.assertEqual(parent.results["knowledge"], candidate)
        self.assertEqual(parent.budget.spent, 100)

    def test_cancellation_and_replanning_do_not_erase_consumption(self):
        for options, code in [({"cancel_before_delivery": True}, "LATE_CANCELLED"),
                              ({"replan_before_delivery": True}, "STALE_RESULT")]:
            output = asyncio.run(run_demo(**options))
            self.assertEqual(output["metered_tokens"], 300)
            self.assertTrue(all(event["code"] == code for event in output["audit"]))

    def test_capability_and_budget_admission_are_parent_owned(self):
        parent = Coordinator(token_budget=350)
        with self.assertRaisesRegex(ValueError, "capability_denied"):
            parent.dispatch("policy", requested_capabilities=("create_ticket",))
        self.assertEqual(parent.budget.available, 350)
        contract = parent.dispatch("knowledge")
        with self.assertRaisesRegex(ValueError, "parent_budget_exhausted"):
            parent.dispatch("diagnosis")
        parent.budget.settle(contract.attempt_id, 100)
        parent.dispatch("diagnosis")
        self.assertEqual(parent.budget.available, 50)

    def test_reported_usage_cannot_change_metered_ledger(self):
        parent = Coordinator()
        contract = parent.dispatch("knowledge")
        candidate = result_for(contract)
        candidate["usage_tokens"] = 0
        parent.accept(candidate)
        self.assertEqual(parent.budget.spent, 0)
        parent.budget.settle(contract.attempt_id, 100)
        self.assertEqual(parent.budget.spent, 100)
        with self.assertRaisesRegex(ValueError, "conflicting_metering_receipt"):
            parent.budget.settle(contract.attempt_id, 0)

    def test_cap_violation_is_recorded_instead_of_hiding_cost(self):
        budget = Budget(100)
        budget.reserve("a1", 80)
        self.assertEqual(budget.settle("a1", 120), "CAP_EXCEEDED")
        self.assertEqual(budget.spent, 120)
        with self.assertRaisesRegex(ValueError, "parent_budget_exhausted"):
            budget.reserve("a2", 1)


if __name__ == "__main__":
    unittest.main()
