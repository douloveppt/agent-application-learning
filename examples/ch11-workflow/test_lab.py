from copy import deepcopy
from dataclasses import replace
import unittest

from lab import (
    CandidateError,
    Executor,
    InputRef,
    Node,
    Plan,
    PlanError,
    ReadCache,
    Scope,
    SyntheticBackend,
    TaskRequest,
    admit_route,
    build_plan,
    route_candidate,
    run_workflow,
    step_ref,
    validate_plan,
)


class OverrideResultBackend(SyntheticBackend):
    def __init__(self, capability, result):
        super().__init__()
        self.overridden_capability = capability
        self.overridden_result = result

    def execute(self, scope, capability, inputs):
        if capability == self.overridden_capability:
            self.dispatches.append({"capability": capability, "user_id": scope.user_id,
                                    "inputs": deepcopy(inputs)})
            return deepcopy(self.overridden_result)
        return super().execute(scope, capability, inputs)


class RoutingTests(unittest.TestCase):
    def test_combined_route_is_admitted_with_complete_confirmed_input(self):
        decision = admit_route(TaskRequest(), route_candidate("combined"))
        self.assertTrue(decision.runnable)
        self.assertEqual((decision.route, decision.reason), ("combined", "route_admitted"))

    def test_low_confidence_cannot_force_dispatch(self):
        decision = admit_route(TaskRequest(), route_candidate("combined", confidence=0.4))
        self.assertEqual((decision.route, decision.reason),
                         ("clarify", "confidence_below_threshold"))
        self.assertFalse(decision.runnable)

    def test_missing_route_input_becomes_specific_clarification(self):
        decision = admit_route(TaskRequest(ticket_id=None), route_candidate("combined"))
        self.assertEqual(decision.route, "clarify")
        self.assertEqual(decision.missing_fields, ("ticket_id",))

    def test_invalid_or_malformed_candidates_are_rejected(self):
        invalid = [
            route_candidate("delete_ticket"),
            {"route": "combined", "confidence": 0.9, "rationale": "ok",
             "tenant_id": "other-tenant"},
            {"route": "combined", "confidence": True, "rationale": "ok"},
            {"route": "combined", "confidence": 1.1, "rationale": "ok"},
        ]
        for candidate in invalid:
            with self.subTest(candidate=candidate):
                with self.assertRaises(CandidateError):
                    admit_route(TaskRequest(), candidate)

    def test_invalid_route_never_reaches_backend(self):
        backend = SyntheticBackend()
        with self.assertRaises(CandidateError):
            run_workflow(Scope(), TaskRequest(), route_candidate("close_ticket"), backend=backend)
        self.assertEqual((backend.admissions, backend.dispatches), ([], []))


class PlanValidationTests(unittest.TestCase):
    def setUp(self):
        self.request = TaskRequest()
        self.plan = build_plan("combined")

    def test_reference_combined_plan_is_valid(self):
        self.assertIsNone(validate_plan(self.plan, self.request))

    def test_duplicate_id_and_unknown_dependency_are_rejected(self):
        duplicate = replace(
            self.plan,
            nodes=(self.plan.nodes[0], replace(self.plan.nodes[1], id="knowledge"),
                   self.plan.nodes[2]),
        )
        unknown_dependency = replace(
            self.plan,
            nodes=(replace(self.plan.nodes[0], depends_on=("missing",)),
                   *self.plan.nodes[1:]),
        )
        for plan, reason in ((duplicate, "duplicate_node_id"),
                             (unknown_dependency, "unknown_dependency")):
            with self.subTest(reason=reason):
                with self.assertRaisesRegex(PlanError, reason):
                    validate_plan(plan, self.request)

    def test_cycle_is_rejected_before_execution(self):
        cyclic = replace(
            self.plan,
            nodes=(replace(self.plan.nodes[0], depends_on=("deliver",)),
                   self.plan.nodes[1], self.plan.nodes[2]),
        )
        with self.assertRaisesRegex(PlanError, "plan_contains_cycle"):
            validate_plan(cyclic, self.request)

    def test_unknown_or_route_mismatched_capability_is_rejected(self):
        unknown = replace(
            self.plan,
            nodes=(replace(self.plan.nodes[0], capability="delete_ticket"),
                   *self.plan.nodes[1:]),
        )
        mismatched = replace(build_plan("knowledge"), route="ticket_status")
        for plan, reason in ((unknown, "unknown_capability"),
                             (mismatched, "capability_does_not_match_route")):
            with self.subTest(reason=reason):
                with self.assertRaisesRegex(PlanError, reason):
                    validate_plan(plan, self.request)

    def test_node_and_attempt_budgets_are_enforced(self):
        too_many_nodes = replace(
            self.plan,
            nodes=self.plan.nodes + tuple(
                replace(self.plan.nodes[0], id=f"extra_{index}") for index in range(6)),
        )
        searches = tuple(
            replace(self.plan.nodes[0], id=f"knowledge_{index}", max_attempts=3)
            for index in range(4)
        )
        deliver = replace(
            self.plan.nodes[2],
            depends_on=tuple(node.id for node in searches) + ("ticket",),
            inputs={"knowledge": step_ref("knowledge_0"), "ticket": step_ref("ticket")},
        )
        too_many_attempts = replace(self.plan, nodes=searches + (self.plan.nodes[1], deliver))
        for plan, reason in ((too_many_nodes, "node_budget_exceeded"),
                             (too_many_attempts, "attempt_budget_exceeded")):
            with self.subTest(reason=reason):
                with self.assertRaisesRegex(PlanError, reason):
                    validate_plan(plan, self.request)

    def test_non_read_node_cannot_request_retry(self):
        plan = replace(
            self.plan,
            nodes=(*self.plan.nodes[:2], replace(self.plan.nodes[2], max_attempts=2)),
        )
        with self.assertRaisesRegex(PlanError, "only_read_capabilities_may_retry"):
            validate_plan(plan, self.request)

    def test_step_input_requires_declared_direct_dependency(self):
        delivery = replace(
            self.plan.nodes[2],
            depends_on=("ticket",),
            inputs={"knowledge": step_ref("knowledge"), "ticket": step_ref("ticket")},
        )
        plan = replace(self.plan, nodes=(*self.plan.nodes[:2], delivery))
        with self.assertRaisesRegex(PlanError, "step_reference_requires_direct_dependency"):
            validate_plan(plan, self.request)

    def test_combined_delivery_must_consume_both_branch_results(self):
        delivery = replace(
            self.plan.nodes[2], inputs={"ticket": step_ref("ticket")})
        plan = replace(self.plan, nodes=(*self.plan.nodes[:2], delivery))
        with self.assertRaisesRegex(PlanError, "delivery_input_contract_mismatch"):
            validate_plan(plan, self.request)

    def test_delivery_input_names_must_match_upstream_capabilities(self):
        delivery = replace(
            self.plan.nodes[2],
            inputs={"knowledge": step_ref("ticket"),
                    "ticket": step_ref("knowledge")},
        )
        plan = replace(self.plan, nodes=(*self.plan.nodes[:2], delivery))
        with self.assertRaisesRegex(PlanError, "delivery_input_source_mismatch"):
            validate_plan(plan, self.request)

    def test_orphan_node_that_cannot_reach_delivery_is_rejected(self):
        orphan = replace(self.plan.nodes[0], id="orphan")
        plan = replace(self.plan, nodes=(*self.plan.nodes, orphan))
        with self.assertRaisesRegex(PlanError, "node_cannot_reach_delivery"):
            validate_plan(plan, self.request)

    def test_plan_cannot_read_a_missing_request_field(self):
        with self.assertRaisesRegex(PlanError, "plan_references_missing_request_field"):
            validate_plan(self.plan, TaskRequest(ticket_id=None))

    def test_invalid_read_reference_source_is_rejected(self):
        knowledge = replace(
            self.plan.nodes[0],
            inputs={**self.plan.nodes[0].inputs,
                    "os": InputRef("model_memory", "confirmed_os")},
        )
        plan = replace(self.plan, nodes=(knowledge, *self.plan.nodes[1:]))
        with self.assertRaisesRegex(PlanError, "capability_input_source_mismatch"):
            validate_plan(plan, self.request)

    def test_read_input_slots_cannot_be_rebound(self):
        step_bound = replace(
            self.plan.nodes[0],
            depends_on=("ticket",),
            inputs={**self.plan.nodes[0].inputs,
                    "issue": step_ref("ticket")},
        )
        wrong_request_field = replace(
            self.plan.nodes[0],
            inputs={**self.plan.nodes[0].inputs,
                    "issue": InputRef("request", "confirmed_os")},
        )
        for knowledge in (step_bound, wrong_request_field):
            with self.subTest(issue_ref=knowledge.inputs["issue"]):
                plan = replace(self.plan, nodes=(knowledge, *self.plan.nodes[1:]))
                backend = SyntheticBackend()
                with self.assertRaisesRegex(PlanError, "capability_input_source_mismatch"):
                    Executor(Scope(), backend=backend).run(plan, self.request)
                self.assertEqual((backend.admissions, backend.dispatches), ([], []))


class ExecutorTests(unittest.TestCase):
    def setUp(self):
        self.scope = Scope()
        self.request = TaskRequest()
        self.candidate = route_candidate("combined")

    def test_normal_run_has_two_ready_waves_and_verified_delivery(self):
        backend = SyntheticBackend()
        result = run_workflow(self.scope, self.request, self.candidate, backend=backend)
        self.assertEqual((result["status"], result["reason"]),
                         ("completed", "delivery_succeeded"))
        self.assertEqual(result["waves"], [["knowledge", "ticket"], ["deliver"]])
        self.assertEqual(result["output"]["evidence_ids"], ["KB-E104-g2"])
        self.assertEqual(result["output"]["ticket"]["ticket_status"], "in_progress")
        self.assertEqual([item["capability"] for item in backend.dispatches],
                         ["search_knowledge", "get_ticket", "compose_answer"])

    def test_failed_branch_does_not_stop_independent_branch_but_blocks_join(self):
        backend = SyntheticBackend(fail_first={"search_knowledge": 2})
        result = run_workflow(self.scope, self.request, self.candidate, backend=backend)
        self.assertEqual((result["status"], result["output"]), ("partial", None))
        self.assertEqual(result["steps"]["knowledge"]["status"], "failed")
        self.assertEqual(result["steps"]["knowledge"]["attempts"], 2)
        self.assertEqual(result["steps"]["knowledge"]["error"],
                         {"status": "error", "code": "read_timeout"})
        self.assertEqual(result["steps"]["ticket"]["status"], "succeeded")
        self.assertEqual(result["steps"]["deliver"]["status"], "blocked")
        self.assertIn("ticket", result["partial_results"])
        self.assertNotIn("compose_answer", [item["capability"] for item in backend.dispatches])

    def test_transient_read_retries_only_within_node_limit(self):
        backend = SyntheticBackend(fail_first={"search_knowledge": 1})
        result = run_workflow(self.scope, self.request, self.candidate, backend=backend)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["steps"]["knowledge"]["attempts"], 2)
        self.assertEqual(sum(item["capability"] == "search_knowledge"
                             for item in backend.dispatches), 2)

    def test_read_timeout_is_not_converted_to_not_found(self):
        backend = SyntheticBackend(fail_first={"get_ticket": 2})
        result = run_workflow(self.scope, self.request, self.candidate, backend=backend)
        error = result["steps"]["ticket"]["error"]
        self.assertEqual(error["code"], "read_timeout")
        self.assertNotEqual(error["code"], "not_found_or_forbidden")

    def test_non_object_backend_result_becomes_an_explicit_step_failure(self):
        result = run_workflow(
            self.scope, self.request, self.candidate,
            backend=OverrideResultBackend("search_knowledge", None))
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["steps"]["knowledge"]["status"], "failed")
        self.assertEqual(result["steps"]["knowledge"]["error"],
                         {"status": "error", "code": "invalid_backend_result"})
        failed = [event for event in result["events"] if event["event"] == "node_failed"]
        self.assertEqual(failed[0]["code"], "invalid_backend_result")

    def test_empty_knowledge_is_a_successful_query_but_not_a_completed_delivery(self):
        result = run_workflow(
            self.scope, TaskRequest(confirmed_client_version="5.3"),
            route_candidate("knowledge"))
        self.assertEqual((result["status"], result["reason"]),
                         ("partial", "knowledge_evidence_missing"))
        self.assertEqual(result["steps"]["knowledge"]["status"], "succeeded")
        self.assertEqual(result["steps"]["knowledge"]["result"]["documents"], [])
        self.assertEqual(result["steps"]["deliver"]["error"],
                         {"status": "error", "code": "knowledge_evidence_missing"})
        self.assertIsNone(result["output"])

    def test_malformed_knowledge_results_fail_without_reaching_delivery(self):
        invalid_results = [
            {"status": "ok"},
            {"status": "ok", "documents": {}},
            {"status": "ok", "documents": [
                {"evidence_id": "KB-1", "topic": "VPN"}]},
            {"status": "ok", "documents": [
                {"evidence_id": "", "topic": "VPN", "text": "内容"}]},
        ]
        for backend_result in invalid_results:
            with self.subTest(backend_result=backend_result):
                result = run_workflow(
                    self.scope, self.request, route_candidate("knowledge"),
                    backend=OverrideResultBackend("search_knowledge", backend_result))
                self.assertEqual(result["status"], "failed")
                self.assertEqual(result["steps"]["knowledge"]["error"]["code"],
                                 "invalid_backend_result")
                self.assertEqual(result["steps"]["deliver"]["status"], "blocked")

    def test_malformed_ticket_results_fail_without_key_errors(self):
        invalid_results = [
            {"status": "ok"},
            {"status": "ok", "ticket": []},
            {"status": "ok", "ticket": {
                "ticket_id": "IT-999", "ticket_status": "in_progress"}},
            {"status": "ok", "ticket": {
                "ticket_id": "IT-100", "ticket_status": "invented"}},
            {"status": "ok", "ticket": {
                "ticket_id": "IT-100", "ticket_status": []}},
        ]
        for backend_result in invalid_results:
            with self.subTest(backend_result=backend_result):
                result = run_workflow(
                    self.scope, self.request, route_candidate("ticket_status"),
                    backend=OverrideResultBackend("get_ticket", backend_result))
                self.assertEqual(result["status"], "failed")
                self.assertEqual(result["steps"]["ticket"]["error"]["code"],
                                 "invalid_backend_result")

    def test_malformed_compose_results_cannot_become_completed(self):
        invalid_results = [
            {"status": "ok", "evidence_ids": ["KB-E104-g2"],
             "ticket": {"ticket_id": "IT-100", "ticket_status": "in_progress"}},
            {"status": "ok", "answer": "答复", "evidence_ids": ["invented"],
             "ticket": {"ticket_id": "IT-100", "ticket_status": "in_progress"}},
        ]
        for backend_result in invalid_results:
            with self.subTest(backend_result=backend_result):
                result = run_workflow(
                    self.scope, self.request, self.candidate,
                    backend=OverrideResultBackend("compose_answer", backend_result))
                self.assertEqual((result["status"], result["reason"]),
                                 ("partial", "invalid_backend_result"))
                self.assertIsNone(result["output"])

    def test_cache_reuses_only_successful_reads_after_fresh_admission(self):
        backend, cache = SyntheticBackend(), ReadCache()
        first = run_workflow(self.scope, self.request, self.candidate,
                             backend=backend, cache=cache)
        second = run_workflow(self.scope, self.request, self.candidate,
                              backend=backend, cache=cache)
        self.assertEqual((first["status"], second["status"]), ("completed", "completed"))
        self.assertEqual((second["steps"]["knowledge"]["status"],
                          second["steps"]["ticket"]["status"]), ("reused", "reused"))
        self.assertEqual(cache.hits, 2)
        self.assertEqual(sum(call["capability"] in {"search_knowledge", "get_ticket"}
                             for call in backend.dispatches), 2)
        read_admissions = [capability for capability, _ in backend.admissions
                           if capability in {"search_knowledge", "get_ticket"}]
        self.assertEqual(len(read_admissions), 4)

    def test_cache_rejects_invalid_writes_and_discards_corrupt_legacy_entries(self):
        inputs = {"ticket_id": "IT-100"}
        invalid = {"status": "ok"}
        cache = ReadCache()
        self.assertFalse(cache.put(self.scope, "get_ticket", inputs, invalid))

        # Simulate an old-format entry that predates the current result contract.
        cache._values[cache.signature(self.scope, "get_ticket", inputs)] = invalid
        backend = SyntheticBackend()
        result = run_workflow(
            self.scope, self.request, route_candidate("ticket_status"),
            backend=backend, cache=cache)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["steps"]["ticket"]["status"], "succeeded")
        self.assertEqual(cache.hits, 0)
        self.assertEqual(sum(call["capability"] == "get_ticket"
                             for call in backend.dispatches), 1)

    def test_changed_input_produces_a_new_read_signature(self):
        backend, cache = SyntheticBackend(), ReadCache()
        knowledge = route_candidate("knowledge")
        run_workflow(self.scope, self.request, knowledge, backend=backend, cache=cache)
        changed = TaskRequest(confirmed_client_version="5.3")
        result = run_workflow(self.scope, changed, knowledge, backend=backend, cache=cache)
        self.assertEqual((result["status"], result["reason"]),
                         ("partial", "knowledge_evidence_missing"))
        self.assertEqual(result["steps"]["knowledge"]["status"], "succeeded")
        self.assertEqual(cache.hits, 0)
        self.assertEqual(sum(call["capability"] == "search_knowledge"
                             for call in backend.dispatches), 2)

    def test_data_and_policy_revisions_are_part_of_cache_signature(self):
        inputs = {"ticket_id": "IT-100"}
        base = ReadCache(data_revision="data-1", policy_revision="policy-1")
        data_changed = ReadCache(data_revision="data-2", policy_revision="policy-1")
        policy_changed = ReadCache(data_revision="data-1", policy_revision="policy-2")
        signatures = {
            cache.signature(self.scope, "get_ticket", inputs)
            for cache in (base, data_changed, policy_changed)
        }
        signatures.add(base.signature(Scope(user_id="employee-b"), "get_ticket", inputs))
        self.assertEqual(len(signatures), 4)

    def test_admission_is_rechecked_before_cache_after_permission_revocation(self):
        cache, backend = ReadCache(), SyntheticBackend()
        candidate = route_candidate("ticket_status")
        self.assertEqual(run_workflow(
            self.scope, self.request, candidate, backend=backend, cache=cache)["status"],
            "completed")
        backend.revoked_ticket_users.add(self.scope.user_id)
        result = run_workflow(self.scope, self.request, candidate,
                              backend=backend, cache=cache)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["steps"]["ticket"]["error"]["code"],
                         "permission_revoked")
        self.assertEqual(cache.hits, 0)
        self.assertEqual(sum(call["capability"] == "get_ticket"
                             for call in backend.dispatches), 1)

    def test_failed_read_is_not_cached(self):
        backend = SyntheticBackend(fail_first={"search_knowledge": 2})
        cache = ReadCache()
        candidate = route_candidate("knowledge")
        first = run_workflow(self.scope, self.request, candidate, backend=backend, cache=cache)
        second = run_workflow(self.scope, self.request, candidate, backend=backend, cache=cache)
        self.assertEqual((first["status"], second["status"]), ("failed", "completed"))
        self.assertEqual(cache.hits, 0)
        self.assertEqual(sum(call["capability"] == "search_knowledge"
                             for call in backend.dispatches), 3)

    def test_fresh_runs_are_deterministic(self):
        first = run_workflow(self.scope, self.request, self.candidate)
        second = run_workflow(self.scope, self.request, self.candidate)
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
