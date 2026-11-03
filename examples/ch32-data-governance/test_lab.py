"""Negative paths exercise policy, provenance, revocation and feedback gates."""
from dataclasses import replace
import unittest

from lab import Asset, Catalog, Feedback, PURPOSES, Rejected, Review, TrustedRegistry, demo, fixture


class GovernanceTests(unittest.TestCase):
    def test_service_does_not_imply_training_or_evaluation(self):
        catalog, _ = fixture(held=False)
        for kind, purpose in (("train_manifest", "training"), ("eval", "evaluation")):
            with self.subTest(purpose=purpose), self.assertRaises(Rejected):
                catalog.admit(Asset(f"b-{kind}", "tenant-b", kind, purpose,
                                    "Synthetic", ("b-source",)))

    def test_eval_cannot_be_read_as_service(self):
        catalog, _ = fixture(held=False)
        self.assertIn("Synthetic", catalog.read("tenant-a", "eval:tenant-a:fb-1", "evaluation"))
        with self.assertRaisesRegex(Rejected, "purpose"):
            catalog.read("tenant-a", "eval:tenant-a:fb-1", "service")

    def test_eval_cannot_be_rewrapped_as_service_cache(self):
        catalog, _ = fixture(held=False)
        with self.assertRaisesRegex(Rejected, "purpose"):
            catalog.admit(Asset("eval-service-cache", "tenant-a", "cache", "service",
                                "Synthetic", ("eval:tenant-a:fb-1",)))

    def test_training_manifest_cannot_be_read_as_service(self):
        catalog, _ = fixture(held=False)
        self.assertEqual("Synthetic row membership", catalog.read("tenant-a", "a-train", "training"))
        with self.assertRaisesRegex(Rejected, "purpose"):
            catalog.read("tenant-a", "a-train", "service")

    def test_service_derivative_cannot_be_promoted_to_training(self):
        catalog, _ = fixture(held=False)
        self.assertEqual("Same synthetic text", catalog.read("tenant-a", "a-source", "training"))
        with self.assertRaisesRegex(Rejected, "purpose"):
            catalog.admit(Asset("service-cache-training", "tenant-a", "train_manifest", "training",
                                "Synthetic", ("a-cache",)))

    def test_unknown_source_and_purpose_fail_closed(self):
        catalog, _ = fixture(held=False)
        with self.assertRaises(Rejected):
            catalog.admit(Asset("unknown", "tenant-a", "source", "service",
                                "Synthetic", source_locator="unknown"))
        with self.assertRaises(Rejected):
            catalog.read("tenant-a", "a-source", "unspecified")

    def test_complete_lineage_required(self):
        catalog, _ = fixture(held=False)
        bad = (
            Asset("no-parent", "tenant-a", "cache", "service", "Synthetic"),
            Asset("unknown-parent", "tenant-a", "cache", "service", "Synthetic", ("missing",)),
            Asset("fake-source", "tenant-a", "cache", "service", "Synthetic",
                  ("a-source",), "synthetic:ticket-1"),
            Asset("source-parent", "tenant-a", "source", "service", "Synthetic",
                  ("a-source",), "synthetic:ticket-1"),
        )
        for asset in bad:
            with self.subTest(asset=asset.asset_id), self.assertRaises(Rejected):
                catalog.admit(asset)

    def test_cross_tenant_lineage_and_read_rejected(self):
        catalog, _ = fixture(held=False)
        with self.assertRaises(Rejected):
            catalog.admit(Asset("cross", "tenant-b", "cache", "service",
                                "Synthetic", ("a-source",)))
        with self.assertRaises(Rejected):
            catalog.read("tenant-b", "a-cache", "service")

    def test_ids_are_immutable_and_self_cycle_rejected(self):
        catalog, _ = fixture(held=False)
        with self.assertRaises(Rejected):
            catalog.admit(replace(catalog.asset("a-cache"), payload="Replacement"))
        with self.assertRaises(Rejected):
            catalog.admit(Asset("self", "tenant-a", "cache", "service", "Synthetic", ("self",)))

    def test_corrupted_cycle_is_detected(self):
        catalog, _ = fixture(held=False)
        # Simulate corrupted storage; normal admit() cannot create this cycle.
        catalog._assets["a-cache"] = replace(catalog.asset("a-cache"), parents=("a-index",))
        catalog._assets["a-index"] = replace(catalog.asset("a-index"), parents=("a-cache",))
        with self.assertRaisesRegex(Rejected, "cycle"):
            catalog.read("tenant-a", "a-cache", "service")

    def test_dedicated_purpose_cannot_be_relabelled(self):
        catalog, _ = fixture(held=False)
        for kind in ("train_manifest", "eval", "model"):
            with self.subTest(kind=kind), self.assertRaises(Rejected):
                catalog.admit(Asset(f"mislabel-{kind}", "tenant-a", kind, "service",
                                    "Synthetic", ("a-source",)))

    def test_graph_delete_covers_registered_copies_and_is_tenant_scoped(self):
        catalog, _ = fixture(with_model=False, held=False)
        receipt = catalog.delete("request-1", "tenant-a", "synthetic:ticket-1")
        self.assertEqual("complete_in_simulated_catalog", receipt["status"])
        self.assertEqual(6, len(receipt["assets"]))
        self.assertEqual("blocked", catalog.asset("a-train").state)
        for asset_id in receipt["assets"]:
            self.assertIsNone(catalog.asset(asset_id).payload)
        self.assertEqual("Same synthetic text", catalog.read("tenant-b", "b-source", "service"))
        self.assertEqual("Synthetic derived text", catalog.read("tenant-b", "b-cache", "service"))

    def test_tombstone_blocks_new_revision_and_rederived_copy(self):
        catalog, _ = fixture(with_model=False, held=False)
        catalog.delete("request-1", "tenant-a", "synthetic:ticket-1")
        with self.assertRaises(Rejected):
            catalog.admit(Asset("new-revision", "tenant-a", "source", "service", "Replay",
                                source_locator="synthetic:ticket-1"))
        with self.assertRaises(Rejected):
            catalog.admit(Asset("new-cache", "tenant-a", "cache", "service", "Replay", ("a-source",)))

    def test_tombstone_precedes_first_ingestion(self):
        catalog = Catalog(TrustedRegistry({("tenant-a", "late-source"): PURPOSES}))
        receipt = catalog.delete("request-late", "tenant-a", "late-source")
        self.assertEqual({}, receipt["assets"])
        self.assertTrue(receipt["source_tombstoned"])
        with self.assertRaises(Rejected):
            catalog.admit(Asset("late", "tenant-a", "source", "service", "Late",
                                source_locator="late-source"))

    def test_hold_has_partial_receipt_and_ordinary_read_is_blocked(self):
        catalog, _ = fixture(with_model=False)
        receipt = catalog.delete("request-1", "tenant-a", "synthetic:ticket-1")
        self.assertEqual("partial", receipt["status"])
        self.assertEqual(["a-source"], receipt["unresolved"])
        self.assertEqual("held", catalog.asset("a-source").state)
        self.assertIsNotNone(catalog.asset("a-source").payload)
        with self.assertRaises(Rejected):
            catalog.read("tenant-a", "a-source", "service")
        catalog.registry.release_hold("a-source")
        resumed = catalog.delete("request-1", "tenant-a", "synthetic:ticket-1")
        self.assertEqual("complete_in_simulated_catalog", resumed["status"])
        self.assertIsNone(catalog.asset("a-source").payload)

    def test_existing_model_remains_unresolved_after_hold_release(self):
        catalog, _ = fixture()
        catalog.delete("request-1", "tenant-a", "synthetic:ticket-1")
        catalog.registry.release_hold("a-source")
        receipt = catalog.delete("request-1", "tenant-a", "synthetic:ticket-1")
        self.assertEqual("partial", receipt["status"])
        self.assertEqual(["a-model"], receipt["unresolved"])
        self.assertEqual("review_required", catalog.asset("a-model").state)
        self.assertIsNotNone(catalog.asset("a-model").payload)
        with self.assertRaises(Rejected):
            catalog.read("tenant-a", "a-model", "service")

    def test_request_replay_idempotent_but_scope_change_rejected(self):
        catalog, _ = fixture()
        receipt = catalog.delete("request-1", "tenant-a", "synthetic:ticket-1")
        self.assertEqual(receipt, catalog.delete("request-1", "tenant-a", "synthetic:ticket-1"))
        with self.assertRaises(Rejected):
            catalog.delete("request-1", "tenant-b", "synthetic:ticket-1")

    def test_mixed_derivative_is_removed_conservatively(self):
        registry = TrustedRegistry({("tenant-a", "one"): PURPOSES, ("tenant-a", "two"): PURPOSES})
        catalog = Catalog(registry)
        for key in ("one", "two"):
            catalog.admit(Asset(key, "tenant-a", "source", "service", "Synthetic", source_locator=key))
        catalog.admit(Asset("mixed", "tenant-a", "cache", "service", "Merged", ("one", "two")))
        catalog.delete("delete-one", "tenant-a", "one")
        self.assertEqual("deleted", catalog.asset("mixed").state)
        self.assertEqual("Synthetic", catalog.read("tenant-a", "two", "service"))

    def test_untrusted_review_flag_and_model_judgment_rejected(self):
        feedback = Feedback("fb", "tenant-a", "source", "Synthetic input", "Synthetic answer",
                            "evidence", self_reported_reviewed=True)
        for method in (None, "model_self_judgment"):
            reviews = {} if method is None else {
                ("tenant-a", "fb"): Review(feedback.content_digest, "verified", method,
                                            "reviewer", "evidence")}
            catalog = Catalog(TrustedRegistry({("tenant-a", "source"): PURPOSES}, reviews))
            catalog.admit(Asset("source", "tenant-a", "source", "service", "Synthetic",
                                source_locator="source"))
            with self.subTest(method=method), self.assertRaises(Rejected):
                catalog.publish_eval(feedback)

    def test_direct_eval_import_cannot_bypass_review_gate(self):
        catalog, _ = fixture(held=False)
        with self.assertRaisesRegex(Rejected, "trusted review"):
            catalog.admit(Asset("unreviewed-eval", "tenant-a", "eval", "evaluation",
                                "Synthetic answer", ("a-source",)))

    def test_changed_answer_or_evidence_invalidates_review(self):
        catalog, feedback = fixture(held=False)
        for changed in (replace(feedback, expected_text="Changed answer"),
                        replace(feedback, evidence_ref="other-evidence")):
            with self.subTest(change=changed), self.assertRaisesRegex(Rejected, "review"):
                catalog.publish_eval(changed)

    def test_verified_feedback_still_requires_evaluation_rights(self):
        feedback = Feedback("fb", "tenant-b", "b-source", "Synthetic input", "Synthetic answer", "proof")
        review = Review(feedback.content_digest, "verified", "human_with_evidence", "reviewer", "proof")
        catalog = Catalog(TrustedRegistry({("tenant-b", "source"): frozenset({"service"})},
                                          {("tenant-b", "fb"): review}))
        catalog.admit(Asset("b-source", "tenant-b", "source", "service", "Synthetic", source_locator="source"))
        with self.assertRaisesRegex(Rejected, "purpose"):
            catalog.publish_eval(feedback)

    def test_verified_feedback_is_bound_to_tenant_and_cannot_be_published_twice(self):
        catalog, feedback = fixture(held=False)
        with self.assertRaises(Rejected):
            catalog.publish_eval(replace(feedback, tenant="tenant-b"))
        with self.assertRaisesRegex(Rejected, "identity"):
            catalog.publish_eval(feedback)

    def test_held_parent_blocks_new_derivative_before_delete(self):
        catalog, _ = fixture()
        with self.assertRaises(Rejected):
            catalog.admit(Asset("held-child", "tenant-a", "cache", "service", "Synthetic", ("a-index",)))

    def test_demo_expected_failures_and_partial_model_receipt(self):
        result = demo()
        self.assertEqual(5, len(result["expected_rejections"]))
        self.assertEqual("partial", result["first_deletion"]["status"])
        self.assertEqual(["a-model"], result["after_hold_release"]["unresolved"])


if __name__ == "__main__":
    unittest.main()
