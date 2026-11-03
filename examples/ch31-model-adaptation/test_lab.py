from dataclasses import FrozenInstanceError, replace
import unittest

from lab import (
    AdaptationManifest,
    DataRow,
    DataSnapshot,
    EvalCase,
    EvalContract,
    EvalResult,
    Rejected,
    admit_dataset,
    demo,
    evaluate_candidate,
    fixtures,
    normalize_text,
)


class ModelAdaptationLabTests(unittest.TestCase):
    def setUp(self):
        self.snapshot, self.contract, self.manifest, self.results = fixtures()

    def test_clean_dataset_and_complete_eval_can_promote(self):
        gate = evaluate_candidate(self.manifest, self.snapshot, self.contract, self.results)
        self.assertEqual(gate.decision, "promote")
        self.assertGreaterEqual(dict(gate.overall)["uplift"], 5)
        self.assertEqual(dict(self.snapshot.split_counts), {"train": 2, "validation": 1, "test": 2})

    def test_dataset_requires_explicit_rights_privacy_and_owner_purpose(self):
        changes = (
            {"usage_right": "unknown"},
            {"usage_right": "denied"},
            {"privacy": "raw"},
            {"owner_approved": False},
            {"purpose": "analytics"},
        )
        for change in changes:
            rows = (replace(self.snapshot.rows[0], **change),) + self.snapshot.rows[1:]
            with self.subTest(change=change), self.assertRaises(Rejected):
                admit_dataset(rows)

    def test_all_splits_must_exist(self):
        with self.assertRaisesRegex(Rejected, "split test is empty"):
            admit_dataset(tuple(row for row in self.snapshot.rows if row.split != "test"))

    def test_normalized_content_detects_cross_split_copy_with_new_row_id(self):
        copied = replace(
            self.snapshot.rows[0],
            row_id="new-row-id",
            split="test",
            conversation_id="new-conversation",
            group_id="new-group",
            user_text=" ＶＰＮ  连接后无法访问内网 ",
        )
        self.assertEqual(copied.content_hash, self.snapshot.rows[0].content_hash)
        with self.assertRaisesRegex(Rejected, "normalized content"):
            admit_dataset(self.snapshot.rows + (copied,))

    def test_same_input_with_different_answer_cannot_cross_splits(self):
        copied = replace(
            self.snapshot.rows[0],
            row_id="new-row-id-and-answer",
            split="test",
            conversation_id="new-conversation",
            group_id="new-group",
            assistant_text="这是一个不同的目标答案。",
        )
        self.assertNotEqual(copied.content_hash, self.snapshot.rows[0].content_hash)
        self.assertEqual(copied.input_hash, self.snapshot.rows[0].input_hash)
        with self.assertRaisesRegex(Rejected, "cross-split input_hash"):
            admit_dataset(self.snapshot.rows + (copied,))

    def test_conversation_or_group_cannot_cross_splits(self):
        for field in ("conversation_id", "group_id"):
            copied = replace(
                self.snapshot.rows[0],
                row_id=f"related-{field}",
                split="test",
                user_text=f"different request for {field}",
                assistant_text=f"different answer for {field}",
                conversation_id=(
                    self.snapshot.rows[0].conversation_id if field == "conversation_id" else "new-conv"
                ),
                group_id=self.snapshot.rows[0].group_id if field == "group_id" else "new-group",
            )
            with self.subTest(field=field), self.assertRaisesRegex(Rejected, f"cross-split {field}"):
                admit_dataset(self.snapshot.rows + (copied,))

    def test_duplicate_row_or_content_is_rejected(self):
        duplicate_id = replace(
            self.snapshot.rows[0], user_text="another input", assistant_text="another output"
        )
        with self.assertRaisesRegex(Rejected, "duplicate row_id"):
            admit_dataset(self.snapshot.rows + (duplicate_id,))
        same_content = replace(
            self.snapshot.rows[0], row_id="new-id", conversation_id="new-conv", group_id="new-group"
        )
        with self.assertRaisesRegex(Rejected, "duplicate normalized content"):
            admit_dataset(self.snapshot.rows + (same_content,))

    def test_manifest_is_immutable_and_each_version_changes_digest(self):
        with self.assertRaises(FrozenInstanceError):
            self.manifest.template_revision = "overwrite"
        fields = (
            "candidate_id",
            "base_model_revision",
            "dataset_digest",
            "training_recipe_revision",
            "template_revision",
            "tokenizer_revision",
            "eval_contract_digest",
        )
        for field in fields:
            changed = replace(self.manifest, **{field: f"changed-{field}"})
            with self.subTest(field=field):
                self.assertNotEqual(changed.digest, self.manifest.digest)

    def test_manifest_must_bind_the_exact_eval_contract(self):
        wrong = replace(self.manifest, eval_contract_digest="wrong-contract")
        with self.assertRaisesRegex(Rejected, "different eval contract"):
            evaluate_candidate(wrong, self.snapshot, self.contract, self.results)

    def test_manifest_must_bind_a_self_consistent_admitted_dataset_snapshot(self):
        wrong_manifest = replace(self.manifest, dataset_digest="arbitrary-dataset")
        with self.assertRaisesRegex(Rejected, "different dataset snapshot"):
            evaluate_candidate(wrong_manifest, self.snapshot, self.contract, self.results)

        forged_digest = replace(self.snapshot, digest="forged-digest")
        with self.assertRaisesRegex(Rejected, "not self-consistent"):
            evaluate_candidate(self.manifest, forged_digest, self.contract, self.results)

        changed_rows = self.snapshot.rows[:-1] + (
            replace(self.snapshot.rows[-1], assistant_text="tampered after admission"),
        )
        forged_rows = DataSnapshot(changed_rows, self.snapshot.digest, self.snapshot.split_counts)
        with self.assertRaisesRegex(Rejected, "not self-consistent"):
            evaluate_candidate(self.manifest, forged_rows, self.contract, self.results)

    def test_results_must_bind_the_exact_manifest(self):
        changed = replace(self.manifest, tokenizer_revision="tokenizer-other-revision")
        with self.assertRaisesRegex(Rejected, "different manifest"):
            evaluate_candidate(changed, self.snapshot, self.contract, self.results)

    def test_missing_unexpected_or_duplicate_blind_case_is_rejected(self):
        with self.assertRaisesRegex(Rejected, "incomplete blind set"):
            evaluate_candidate(self.manifest, self.snapshot, self.contract, self.results[:-1])
        unexpected = replace(self.results[-1], case_id="not-in-contract")
        with self.assertRaisesRegex(Rejected, "unexpected"):
            evaluate_candidate(
                self.manifest, self.snapshot, self.contract, self.results[:-1] + (unexpected,)
            )
        duplicated = self.results[:-1] + (self.results[0],)
        with self.assertRaisesRegex(Rejected, "duplicate eval case"):
            evaluate_candidate(self.manifest, self.snapshot, self.contract, duplicated)

    def test_positive_average_cannot_override_hard_safety_failure(self):
        results = tuple(
            replace(row, candidate_score=100, safe=False)
            if row.case_id == "blind-auth-1"
            else replace(row, candidate_score=100)
            for row in self.results
        )
        gate = evaluate_candidate(self.manifest, self.snapshot, self.contract, results)
        self.assertEqual(gate.decision, "reject")
        self.assertIn("hard safety", gate.reason)
        self.assertGreater(dict(gate.overall)["uplift"], 0)

    def test_critical_slice_and_overall_uplift_are_separate_gates(self):
        low_slice = tuple(
            replace(row, candidate_score=79) if row.case_id == "blind-auth-1" else row
            for row in self.results
        )
        self.assertIn(
            "critical slice",
            evaluate_candidate(self.manifest, self.snapshot, self.contract, low_slice).reason,
        )
        no_uplift_scores = {
            "blind-help-1": 60,
            "blind-help-2": 60,
            "blind-auth-1": 80,
            "blind-write-1": 80,
        }
        no_uplift = tuple(
            replace(row, candidate_score=no_uplift_scores[row.case_id])
            for row in self.results
        )
        self.assertIn(
            "overall uplift",
            evaluate_candidate(self.manifest, self.snapshot, self.contract, no_uplift).reason,
        )

    def test_invalid_types_and_contract_duplicates_are_rejected(self):
        with self.assertRaises(Rejected):
            DataRow("id", "train", "conv", "group", "user", "answer", owner_approved="yes")
        with self.assertRaises(Rejected):
            EvalResult("case", "manifest", True, True)
        duplicate = EvalCase("same", "quality", 50)
        with self.assertRaisesRegex(Rejected, "duplicate case_id"):
            EvalContract("bad", (duplicate, duplicate), (("quality", 50),))
        with self.assertRaises(Rejected):
            AdaptationManifest("candidate", "", "data", "recipe", "template", "tokenizer", "eval")

    def test_eval_contract_requires_tuples_typed_members_and_finite_uplift(self):
        case = EvalCase("case", "quality", 50)
        invalid_values = (
            {"cases": [case]},
            {"cases": ("not-an-eval-case",)},
            {"critical_slice_minima": [["quality", 50]]},
            {"critical_slice_minima": (("quality", "50"),)},
            {"minimum_overall_uplift": float("nan")},
            {"minimum_overall_uplift": float("inf")},
        )
        for change in invalid_values:
            arguments = {
                "contract_id": "strict-contract",
                "cases": (case,),
                "critical_slice_minima": (("quality", 50),),
                "minimum_overall_uplift": 1.0,
                **change,
            }
            with self.subTest(change=change), self.assertRaises(Rejected):
                EvalContract(**arguments)

    def test_normalization_is_nfkc_casefold_and_whitespace_only(self):
        self.assertEqual(normalize_text("  ＶＰＮ\n连接  "), "vpn 连接")
        self.assertNotEqual(normalize_text("VPN 连接"), normalize_text("VPN 无法连接"))

    def test_demo_exposes_pass_safety_rejection_and_invalid_evidence(self):
        report = demo()
        self.assertTrue(all(report["assertions"].values()))
        self.assertEqual(report["gates"]["clean"]["decision"], "promote")
        self.assertEqual(report["gates"]["high_average_but_unsafe"]["decision"], "reject")
        self.assertEqual(
            set(report["invalid_evidence"]),
            {
                "cross_split_content",
                "cross_split_input",
                "privacy",
                "missing_eval",
                "duplicate_eval",
                "version_mismatch",
                "dataset_mismatch",
            },
        )


if __name__ == "__main__":
    unittest.main()
