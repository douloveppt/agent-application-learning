from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier
import unittest

from lab import (
    BudgetDenied, Ledger, ResultCache, Router, Scope, SimulatedOutcome, Task,
    demo_models,
)


class LabTests(unittest.TestCase):
    def setUp(self):
        self.task = Task("run-1", Scope("a", "user-1", "authz-1"), "VPN E104")
        self.outcomes = {
            "small-eu": SimulatedOutcome(False, 30),
            "strong-eu": SimulatedOutcome(True, 80),
        }

    def test_concurrent_reservations_do_not_oversell(self):
        ledger, barrier = Ledger(100), Barrier(12)

        def contender(i):
            barrier.wait()
            try:
                ledger.reserve(str(i), 30)
                return True
            except BudgetDenied:
                return False

        with ThreadPoolExecutor(max_workers=12) as pool:
            admitted = list(pool.map(contender, range(12)))
        self.assertEqual(sum(admitted), 3)
        self.assertEqual(ledger.snapshot()["reserved"], 90)

    def test_reservation_and_settlement_are_idempotent_but_conflicts_fail(self):
        ledger = Ledger(100)
        ledger.reserve("x", 60)
        ledger.reserve("x", 60)
        with self.assertRaises(ValueError):
            ledger.reserve("x", 50)
        ledger.settle("x", 40)
        ledger.settle("x", 40)
        with self.assertRaises(ValueError):
            ledger.reserve("x", 60)
        self.assertEqual(ledger.snapshot()["spent"], 40)
        self.assertEqual(ledger.snapshot()["reserved"], 0)
        with self.assertRaises(ValueError):
            ledger.settle("x", 41)

    def test_unknown_usage_stays_reserved_until_reconciliation(self):
        ledger = Ledger(100)
        ledger.reserve("x", 80)
        ledger.mark_unknown("x")
        with self.assertRaises(ValueError):
            ledger.reserve("x", 80)
        with self.assertRaises(BudgetDenied):
            ledger.reserve("y", 30)
        with self.assertRaises(ValueError):
            ledger.settle("x", 0)
        self.assertEqual(ledger.snapshot()["reserved"], 80)
        ledger.reconcile("x", 50)
        ledger.reconcile("x", 50)
        ledger.reserve("y", 50)
        self.assertEqual(ledger.snapshot()["spent"], 50)

    def test_overrun_records_full_actual_and_blocks_new_admission(self):
        ledger = Ledger(100)
        ledger.reserve("x", 40)
        ledger.reserve("in-flight", 50)
        ledger.settle("x", 70)
        state = ledger.snapshot()
        self.assertEqual((state["spent"], state["reserved"]), (70, 50))
        self.assertEqual(state["entries"]["x"]["overrun"], 30)
        self.assertTrue(state["admission_blocked"])
        with self.assertRaises(BudgetDenied):
            ledger.reserve("new", 0)

    def test_hard_filters_precede_cost_order_and_upgrade_rechecks_budget(self):
        ledger = Ledger(150)
        result = Router(demo_models(), ledger, ResultCache()).run(self.task, self.outcomes)
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual([a["model"] for a in result["attempts"]], ["small-eu", "strong-eu"])
        self.assertEqual(ledger.snapshot()["spent"], 110)
        self.assertEqual(ledger.snapshot()["reserved"], 0)

    def test_upgrade_is_denied_if_remaining_budget_cannot_cover_reservation(self):
        ledger = Ledger(120)
        result = Router(demo_models(), ledger, ResultCache()).run(self.task, self.outcomes)
        self.assertEqual(result["status"], "budget_exhausted")
        self.assertEqual(len(result["attempts"]), 1)
        self.assertEqual(ledger.snapshot()["spent"], 30)

    def test_cache_scope_and_version_boundaries(self):
        cache = ResultCache()
        cache.put(self.task, "approved read-only answer")
        self.assertIsNotNone(cache.get(replace(self.task, run_id="another-run")))
        variants = [
            replace(self.task, scope=replace(self.task.scope, tenant="b")),
            replace(self.task, scope=replace(self.task.scope, principal="user-2")),
            replace(self.task, scope=replace(self.task.scope, authz_epoch="authz-2")),
            replace(self.task, evidence_version="manual-v2"),
            replace(self.task, prompt_version="prompt-v2"),
            replace(self.task, policy_version="route-v2"),
            replace(self.task, output_schema_version="schema-v2"),
            replace(self.task, result_contract_version="result-contract-v2"),
            replace(self.task, model_config_version="model-config-v2"),
        ]
        for task in variants:
            self.assertIsNone(cache.get(task))
        with self.assertRaises(ValueError):
            cache.put(replace(self.task, read_only=False), "created")

    def test_cache_hit_spends_no_new_simulated_budget(self):
        cache, ledger = ResultCache(), Ledger(0)
        cache.put(self.task, "answer")
        result = Router(demo_models(), ledger, cache).run(self.task, {})
        self.assertEqual(result["status"], "cache_hit")
        self.assertEqual(result["attempts"], [])
        self.assertEqual(ledger.snapshot()["spent"], 0)

    def test_timeout_does_not_fall_back_or_assume_zero_usage(self):
        ledger = Ledger(150)
        result = Router(demo_models(), ledger, ResultCache()).run(
            self.task, {"small-eu": SimulatedOutcome(False, None)},
        )
        self.assertEqual(result["status"], "usage_pending")
        self.assertEqual(len(result["attempts"]), 1)
        self.assertEqual(ledger.snapshot()["reserved"], 40)
        combined_ledger = Ledger(150)
        combined = Router(demo_models(), combined_ledger, ResultCache()).run(
            self.task, {"small-eu": SimulatedOutcome(False, None, effect_after="unknown")},
        )
        self.assertEqual(combined["status"], "reconciliation_required")
        self.assertEqual(combined["usage_state"], "unknown")
        self.assertEqual(combined["effect_certainty"], "unknown")
        self.assertEqual(combined_ledger.snapshot()["reserved"], 40)

    def test_cancel_and_unknown_effect_block_fallback(self):
        for outcome, expected in [
            (SimulatedOutcome(False, 30, cancelled_after=True), "cancelled"),
            (SimulatedOutcome(False, 30, effect_after="unknown"), "reconciliation_required"),
        ]:
            result = Router(demo_models(), Ledger(150), ResultCache()).run(
                replace(self.task, read_only=False), {"small-eu": outcome},
            )
            self.assertEqual(result["status"], expected)
            self.assertEqual(len(result["attempts"]), 1)

    def test_confirmed_write_and_write_without_effect_never_replay(self):
        for actual in [30, None]:
            result = Router(demo_models(), Ledger(150), ResultCache()).run(
                replace(self.task, read_only=False),
                {"small-eu": SimulatedOutcome(False, actual, effect_after="confirmed")},
            )
            self.assertEqual(result["status"], "write_effect_confirmed")
            self.assertEqual(result["effect_certainty"], "confirmed")
            self.assertEqual(result["quality_state"], "failed")
            self.assertEqual(len(result["attempts"]), 1)
        no_effect = Router(demo_models(), Ledger(150), ResultCache()).run(
            replace(self.task, read_only=False), {"small-eu": SimulatedOutcome(False, 30)},
        )
        self.assertEqual(no_effect["status"], "write_effect_not_confirmed")
        self.assertEqual(len(no_effect["attempts"]), 1)

    def test_read_only_task_with_confirmed_effect_reports_contract_breach(self):
        cache = ResultCache()
        result = Router(demo_models(), Ledger(150), cache).run(
            self.task, {"small-eu": SimulatedOutcome(False, 30, effect_after="confirmed")},
        )
        self.assertEqual(result["status"], "effect_contract_breached")
        self.assertEqual(result["effect_certainty"], "confirmed")
        self.assertEqual(len(result["attempts"]), 1)
        self.assertIsNone(cache.get(self.task))
        cached = ResultCache()
        cached.put(self.task, "answer")
        preexisting = Router(demo_models(), Ledger(150), cached).run(
            replace(self.task, effect_certainty="confirmed"), {},
        )
        self.assertEqual(preexisting["status"], "effect_contract_breached")
        self.assertEqual(preexisting["attempts"], [])

    def test_preexisting_cancel_and_unknown_do_not_admit_or_use_cache(self):
        for changes, expected in [
            ({"cancelled": True}, "cancelled"),
            ({"effect_certainty": "unknown"}, "reconciliation_required"),
        ]:
            cache, ledger = ResultCache(), Ledger(150)
            cache.put(self.task, "answer")
            result = Router(demo_models(), ledger, cache).run(replace(self.task, **changes), {})
            self.assertEqual(result["status"], expected)
            self.assertEqual(result["attempts"], [])
            self.assertEqual(ledger.snapshot()["entries"], {})

    def test_no_model_or_exhausted_quality_stops_with_two_attempts_at_most(self):
        router = Router(demo_models(), Ledger(200), ResultCache())
        absent = router.run(replace(self.task, region="unavailable"), {})
        self.assertEqual(absent["status"], "no_eligible_model")
        failed = router.run(replace(self.task, run_id="run-2"), {
            "small-eu": SimulatedOutcome(False, 30),
            "strong-eu": SimulatedOutcome(False, 80),
        })
        self.assertEqual(failed["status"], "quality_not_met")
        self.assertEqual(len(failed["attempts"]), 2)


if __name__ == "__main__":
    unittest.main()
