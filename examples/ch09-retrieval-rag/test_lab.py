import json
import math
import unittest
from dataclasses import replace

from lab import (Chunk, Document, IndexRow, RetrievalLab, Scope, bm25, checked_rewrite, demo,
                 recall_at_k, reciprocal_rank, rrf, validate_citations)


class RetrievalTests(unittest.TestCase):
    def setUp(self):
        self.lab = RetrievalLab()
        self.scope = Scope("demo-company", frozenset({"employee"}))
        self.query = "Windows 5.2 VPN E104 连接失败"

    def ids(self, result):
        return [item["chunk_id"] for item in result["evidence"]]

    def test_bm25_matches_a_hand_calculated_equal_length_example(self):
        scores = dict(bm25("e104", {"a": "e104 vpn", "b": "e105 vpn"}))
        self.assertAlmostEqual(scores["a"], math.log(2))
        self.assertNotIn("b", scores)

    def test_rrf_hand_calculation_and_duplicate_route_id(self):
        scores = dict(rrf([["a", "b", "b"], ["b", "c"]]))
        self.assertAlmostEqual(scores["b"], 1 / 62 + 1 / 61)
        self.assertAlmostEqual(scores["a"], 1 / 61)
        self.assertEqual([key for key, _ in rrf([["a", "b"], ["b", "c"]])],
                         ["b", "a", "c"])

    def test_rewrite_preserves_error_code_set(self):
        self.assertEqual(checked_rewrite("E104 连接失败", "VPN E104 无法连接"),
                         "VPN E104 无法连接")
        for rewritten in ("E105 连接失败", "连接失败", "E104 E105"):
            with self.assertRaisesRegex(ValueError, "rewrite_changed_error_code"):
                checked_rewrite("E104 连接失败", rewritten)

    def test_query_missing_or_conflicting_context_requests_clarification(self):
        result = self.lab.retrieve(self.scope, "VPN E104 连接失败")
        self.assertEqual(result["status"], "clarify")
        self.assertEqual(result["missing"], ["os", "client_version"])
        self.assertEqual(self.lab.retrieve(self.scope, "Windows macOS 5.2 E104")["status"],
                         "clarify")

    def test_correct_evidence_and_conditions_are_kept_together(self):
        result = self.lab.retrieve(self.scope, self.query)
        self.assertEqual(result["status"], "evidence_ready")
        self.assertEqual(self.ids(result), ["vpn-current", "vpn-condition"])
        self.assertIn("先检查系统时间", result["evidence"][0]["text"])
        self.assertTrue(all(item["generation"] == "g2" for item in result["evidence"]))

    def test_applicability_prevents_windows_steps_for_macos(self):
        result = self.lab.retrieve(self.scope, "macOS 5.2 VPN E104 连接失败")
        self.assertEqual(self.ids(result), ["vpn-mac"])

    def test_dirty_tenant_and_acl_rows_never_enter_returned_evidence(self):
        dirty = tuple(IndexRow(key, "g1", "demo-company", frozenset({"employee"}))
                      for key in ("hr-private", "other-private"))
        result = self.lab.retrieve(self.scope, self.query, dirty_rows=dirty)
        serialized = json.dumps(result)
        self.assertNotIn("HR_PRIVATE_FIXTURE", serialized)
        self.assertNotIn("OTHER_TENANT_FIXTURE", serialized)
        self.assertNotIn("hr-private", serialized)
        self.assertNotIn("other-private", serialized)
        self.assertNotIn("rejected_candidate_count", serialized)

    def test_stale_deleted_and_wrong_generation_candidates_are_rejected(self):
        wrong_generation = IndexRow("vpn-current", "g1", "demo-company",
                                    frozenset({"employee"}))
        self.assertIsNone(self.lab.eligible(self.scope, wrong_generation))
        result = self.lab.retrieve(self.scope, self.query,
                                   dirty_rows=(wrong_generation,))
        self.assertNotIn("vpn-old", self.ids(result))
        self.assertNotIn("deleted-old", self.ids(result))
        self.assertNotIn("rejected_candidate_count", json.dumps(result))

    def test_expanded_private_neighbor_does_not_bypass_authorization(self):
        # Remove it from the index entirely: it is reachable only through expansion.
        self.lab.index = [row for row in self.lab.index if row.chunk_id != "hr-private"]
        result = self.lab.retrieve(self.scope, self.query)
        self.assertEqual(self.ids(result), ["vpn-current", "vpn-condition"])
        self.assertNotIn("HR_PRIVATE_FIXTURE", json.dumps(result))

    def test_revocation_and_delete_in_authority_override_unchanged_index(self):
        self.lab.documents["vpn"] = replace(self.lab.documents["vpn"],
                                            groups=frozenset({"hr"}))
        self.assertEqual(self.lab.retrieve(self.scope, self.query)["status"], "abstain")
        self.lab.documents["vpn"] = replace(self.lab.documents["vpn"],
                                            groups=frozenset({"employee"}), deleted=True)
        self.assertEqual(self.lab.retrieve(self.scope, self.query)["status"], "abstain")

    def test_budget_does_not_keep_an_orphan_condition_as_answer(self):
        result = self.lab.retrieve(self.scope, self.query, max_chars=60)
        self.assertEqual(result["status"], "abstain")
        self.assertEqual(result["reason"], "evidence_exceeds_budget")
        self.assertEqual(result["evidence"], [])

    def test_any_unusable_required_condition_rejects_the_entire_seed_group(self):
        for failure in ("missing", "forbidden", "stale", "deleted", "inapplicable"):
            with self.subTest(failure=failure):
                lab = RetrievalLab()
                condition = lab.chunks["vpn-condition"]
                if failure == "missing":
                    del lab.chunks["vpn-condition"]
                elif failure == "forbidden":
                    lab.chunks["vpn-condition"] = replace(condition, document_id="hr", generation="g1")
                elif failure == "stale":
                    lab.chunks["vpn-condition"] = replace(condition, generation="g1")
                elif failure == "deleted":
                    lab.documents["condition-owner"] = Document(
                        "demo-company", frozenset({"employee"}), "g2", deleted=True)
                    lab.chunks["vpn-condition"] = replace(condition, document_id="condition-owner")
                else:
                    lab.chunks["vpn-condition"] = replace(condition, os="macOS")
                result = lab.retrieve(self.scope, self.query)
                self.assertEqual(result, {"status": "abstain", "reason": "no_complete_evidence",
                                          "evidence": []})

    def test_transitive_required_dependency_cannot_be_ignored(self):
        condition = self.lab.chunks["vpn-condition"]
        self.lab.chunks["vpn-condition"] = replace(condition, required=("missing-context",))
        result = self.lab.retrieve(self.scope, self.query)
        self.assertEqual(result["reason"], "no_complete_evidence")
        self.assertEqual(result["evidence"], [])

    def test_optional_budget_never_evicts_the_required_group(self):
        self.lab.chunks["extra"] = Chunk(
            "extra", "vpn", "g2", "额外的可选背景。" * 10,
            "Windows", "5.2", "E104", "kb://it/vpn?revision=2#extra", role="condition")
        self.lab.chunks["vpn-current"] = replace(
            self.lab.chunks["vpn-current"], optional=("extra", "hr-private"))
        result = self.lab.retrieve(self.scope, self.query, max_chars=77)
        self.assertEqual(result["status"], "evidence_ready")
        self.assertEqual(self.ids(result), ["vpn-current", "vpn-condition"])

    def two_ranked_seeds(self, *, first_required=(), first_extra_text=""):
        text = "Windows 5.2 VPN E104 连接失败：联系 IT。"
        self.lab.chunks = {
            "a-first": Chunk("a-first", "vpn", "g2", text + first_extra_text,
                             "Windows", "5.2", "E104", "kb://it/vpn#first",
                             required=first_required),
            "b-second": Chunk("b-second", "vpn", "g2", text,
                              "Windows", "5.2", "E104", "kb://it/vpn#second"),
        }
        self.lab.index = [IndexRow(key, "g2", "demo-company", frozenset({"employee"}))
                          for key in self.lab.chunks]
        self.lab.fixture_ranking = ["a-first", "b-second"]

    def test_invalid_first_group_does_not_consume_final_top_k_slot(self):
        self.two_ranked_seeds(first_required=("missing-context",))
        result = self.lab.retrieve(self.scope, self.query, top_k=1)
        self.assertEqual(result["status"], "evidence_ready")
        self.assertEqual(self.ids(result), ["b-second"])

    def test_overbudget_first_group_does_not_consume_final_top_k_slot(self):
        self.two_ranked_seeds(first_extra_text="额外正文" * 100)
        result = self.lab.retrieve(self.scope, self.query, top_k=1, max_chars=60)
        self.assertEqual(result["status"], "evidence_ready")
        self.assertEqual(self.ids(result), ["b-second"])

    def test_selection_stops_after_requested_number_of_complete_seed_groups(self):
        self.two_ranked_seeds()
        result = self.lab.retrieve(self.scope, self.query, top_k=1)
        self.assertEqual(self.ids(result), ["a-first"])

    def test_first_optional_cannot_take_budget_needed_by_second_required_group(self):
        self.two_ranked_seeds()
        required_chars = sum(len(chunk.text) for chunk in self.lab.chunks.values())
        self.lab.chunks["a-first"] = replace(self.lab.chunks["a-first"], optional=("extra",))
        self.lab.chunks["extra"] = Chunk(
            "extra", "vpn", "g2", "可选背景。", "Windows", "5.2", "E104",
            "kb://it/vpn#extra", role="condition")
        result = self.lab.retrieve(self.scope, self.query, top_k=2, max_chars=required_chars)
        self.assertEqual(self.ids(result), ["a-first", "b-second"])
        self.assertEqual(result["used_chars"], required_chars)

    def test_user_responses_and_demo_do_not_expose_hidden_candidate_counts(self):
        responses = [demo(), self.lab.retrieve(self.scope, self.query),
                     self.lab.retrieve(self.scope, "Windows 9.0 VPN E999"),
                     self.lab.retrieve(self.scope, "VPN E104")]
        self.lab.backend_available = False
        responses.append(self.lab.retrieve(self.scope, self.query))
        self.assertNotIn("rejected_candidate_count", json.dumps(responses))

    def test_future_or_unknown_code_does_not_reuse_nearby_vpn_steps(self):
        result = self.lab.retrieve(self.scope, "Windows 9.0 VPN E999 连接失败")
        self.assertEqual(result["status"], "abstain")
        self.assertEqual(result["reason"], "no_applicable_evidence")

    def test_search_failure_is_distinct_from_empty_results(self):
        self.lab.index = []
        self.assertEqual(self.lab.retrieve(self.scope, self.query)["status"], "abstain")
        self.lab.backend_available = False
        result = self.lab.retrieve(self.scope, self.query)
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["code"], "search_unavailable")

    def test_citation_validation_checks_ids_but_cannot_prove_support(self):
        evidence = self.lab.retrieve(self.scope, self.query)["evidence"]
        self.assertFalse(validate_citations([{"text": "检查时间", "citations": ["S999"]}],
                                            evidence))
        self.assertFalse(validate_citations([{"text": "检查时间", "citations": []}], evidence))
        # A structurally valid but false claim passes: entailment needs another check.
        self.assertTrue(validate_citations([{"text": "月球是奶酪", "citations": ["S1"]}],
                                           evidence))

    def test_metrics_with_multiple_relevant_ids_and_unanswerable_question(self):
        self.assertEqual(recall_at_k(["b", "a", "c"], {"b", "c"}, 2), 0.5)
        self.assertEqual(reciprocal_rank(["a", "c", "b"], {"b", "c"}), 0.5)
        self.assertEqual(reciprocal_rank(["a"], {"b"}), 0.0)
        self.assertIsNone(recall_at_k(["a"], set(), 2))

    def test_demo_keeps_fixture_route_explicit_and_output_private(self):
        result = demo()
        self.assertEqual(result["second_route"], "fixed_manual_ranking_not_embedding")
        self.assertEqual(result["backend_failure"], "error")
        self.assertNotIn("PRIVATE_FIXTURE", json.dumps(result))


if __name__ == "__main__":
    unittest.main()
