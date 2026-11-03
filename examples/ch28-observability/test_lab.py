import copy
import json
from pathlib import Path
import re
import tempfile
import unittest

import lab


class ObservabilityLabTests(unittest.TestCase):
    def report(self):
        return lab.build_report(lab.SCENARIOS)

    def by_name(self, report, name):
        return next(row for row in report["samples"] if row["scenario"] == name)

    def test_all_scenarios_have_independent_valid_correlation_ids(self):
        samples = self.report()["samples"]
        traces = set()
        span_ids = set()
        for sample in samples:
            run = sample["evidence"]["run_record"]
            self.assertRegex(run["trace_id"], r"^[0-9a-f]{32}$")
            self.assertRegex(run["root_span_id"], r"^[0-9a-f]{16}$")
            traces.add(run["trace_id"])
            for item in sample["evidence"]["telemetry"]["spans"]:
                self.assertNotIn(item["span_id"], span_ids)
                span_ids.add(item["span_id"])
            self.assertTrue(run["run_id"] and run["attempt_id"] and run["operation_id"])
            self.assertEqual(lab.LAB_VERSION, sample["manifest"]["lab_version"])
        self.assertEqual(len(samples), len(traces))
        self.assertEqual(len(samples), len({
            row["evidence"]["run_record"]["root_span_id"] for row in samples
        }))

    def test_diagnosis_ignores_scenario_label(self):
        sample = copy.deepcopy(self.by_name(self.report(), "lost-receipt"))
        sample["scenario"] = "renamed-by-test"
        self.assertEqual(
            "completed_after_read_only_reconciliation", lab.diagnose(sample)["verdict"]
        )

    def test_lost_receipt_reconciles_without_redispatch(self):
        sample = self.by_name(self.report(), "lost-receipt")
        run = sample["evidence"]["run_record"]
        events = sample["evidence"]["operation_journal"]["events"]
        self.assertEqual("UNKNOWN", run["dispatch_state"])
        self.assertEqual(1, run["write_attempts"])
        self.assertEqual(0, run["write_retries"])
        self.assertEqual(1, run["read_reconciliations"])
        self.assertEqual([
            ("tool_call", "ticket.create", "write", None),
            ("tool_result", "ticket.create", "write", "outcome_unknown"),
            ("tool_call", "ticket.lookup_by_operation", "read", None),
            ("tool_result", "ticket.lookup_by_operation", "read", "found"),
        ], [(e["kind"], e["action"], e["method"], e.get("result")) for e in events])
        self.assertEqual("found", sample["evidence"]["business"]["reconciliation"]["result"])
        self.assertFalse(sample["diagnosis"]["redispatch_allowed"])

    def test_second_create_dispatch_cannot_be_reported_as_safe_reconciliation(self):
        sample = copy.deepcopy(self.by_name(self.report(), "lost-receipt"))
        events = sample["evidence"]["operation_journal"]["events"]
        second_write = copy.deepcopy(next(
            event for event in events
            if event["kind"] == "tool_call" and event["action"] == "ticket.create"
        ))
        events.insert(2, second_write)
        for index, event in enumerate(events):
            event["seq"] = index
        sample["evidence"]["run_record"].update(write_attempts=2, write_retries=1)
        diagnosis = lab.diagnose(sample)
        self.assertEqual("evidence_conflict", diagnosis["verdict"])
        self.assertEqual(2, diagnosis["operation_checks"]["create_dispatches"])

    def test_tampered_run_call_summary_cannot_prove_safe_reconciliation(self):
        sample = copy.deepcopy(self.by_name(self.report(), "lost-receipt"))
        sample["evidence"]["run_record"]["write_attempts"] = 2
        diagnosis = lab.diagnose(sample)
        self.assertEqual("evidence_conflict", diagnosis["verdict"])
        self.assertFalse(diagnosis["operation_checks"]["summary_consistent"])

    def test_write_reconciliation_method_cannot_prove_safe_recovery(self):
        sample = copy.deepcopy(self.by_name(self.report(), "lost-receipt"))
        events = sample["evidence"]["operation_journal"]["events"]
        for event in events:
            if event["action"] == "ticket.lookup_by_operation":
                event["method"] = "write"
        sample["evidence"]["run_record"]["read_reconciliations"] = 0
        sample["evidence"]["business"]["reconciliation"]["method"] = "write_by_operation_id"
        diagnosis = lab.diagnose(sample)
        self.assertEqual("evidence_conflict", diagnosis["verdict"])
        self.assertEqual(0, diagnosis["operation_checks"]["read_reconciliations"])

    def test_receipt_authority_and_scope_revision_must_match_controlled_operation(self):
        original = self.by_name(self.report(), "success")
        mutations = (
            ("authority", "untrusted-system"),
            ("tenant_ref", "tenant-teaching-b"),
            ("requester_ref", "requester-teaching-b"),
            ("approved_revision", "different-revision"),
        )
        for field, value in mutations:
            with self.subTest(field=field):
                sample = copy.deepcopy(original)
                receipt = sample["evidence"]["business"]["authoritative_receipt"]
                receipt[field] = value
                if field != "authority":
                    sample["evidence"]["run_record"][field] = value
                self.assertEqual("evidence_conflict", lab.diagnose(sample)["verdict"])

    def test_inconclusive_negative_lookup_remains_unknown(self):
        sample = self.by_name(self.report(), "telemetry-drop")
        self.assertEqual("UNKNOWN", sample["evidence"]["run_record"]["final_state"])
        self.assertEqual("unknown", sample["diagnosis"]["verdict"])
        self.assertEqual("continue_read_only_reconciliation_or_human_review",
                         sample["diagnosis"]["safe_next_action"])
        self.assertFalse(sample["diagnosis"]["redispatch_allowed"])

    def test_missing_trace_does_not_erase_verified_business_result(self):
        sample = self.by_name(self.report(), "missing-trace")
        self.assertEqual([], sample["evidence"]["telemetry"]["spans"])
        self.assertEqual("completed_verified", sample["diagnosis"]["verdict"])
        self.assertIn("complete_trace_unavailable", sample["diagnosis"]["evidence_gaps"])

    def test_metrics_use_all_run_records_not_only_exported_traces(self):
        metrics = self.report()["metrics"]
        self.assertEqual(5, metrics["runs_total"])
        self.assertEqual(5, metrics["latency_ms"]["denominator_runs"])
        self.assertEqual(3, metrics["trace_exports"]["count"])
        self.assertEqual(5, metrics["trace_exports"]["denominator_runs"])
        self.assertEqual(1, metrics["unknown_runs"])
        self.assertEqual(0.2, metrics["unknown_rate"])

    def test_latency_does_not_sum_parent_and_child_spans(self):
        sample = self.by_name(self.report(), "slow")
        latency = sample["diagnosis"]["latency"]
        spans = sample["evidence"]["telemetry"]["spans"]
        naive_sum = sum(item["duration_ms"] for item in spans)
        self.assertEqual(1_800, latency["root_span_elapsed_ms"])
        self.assertNotEqual(naive_sum, latency["root_span_elapsed_ms"])
        self.assertNotIn("critical_path_ms", latency)
        self.assertEqual("knowledge.retrieval", latency["dominant_child_span"])
        self.assertEqual(500, latency["queue_wait_ms"])

    def test_recursive_sensitive_fields_never_enter_report(self):
        sentinel = "NESTED_SECRET_SENTINEL_91c2"
        request = {
            "category": "vpn", "locale": "zh-CN", "approved": True,
            "tool": {"name": "ticket_create", "arguments": {"password": sentinel}},
            "messages": [{"content": {"raw_prompt": sentinel}}],
            "auth": {"refresh": {"token": sentinel}},
        }
        payload = json.dumps(lab.build_report(("success",), request), ensure_ascii=False)
        self.assertNotIn(sentinel, payload)
        captured = lab.build_report(("success",), request)["samples"][0]["captured_attributes"]
        self.assertEqual({
            "request.category": "vpn", "request.locale": "zh-CN",
            "request.approved": True, "tool.name": "ticket_create",
        }, captured)

    def test_built_in_fault_sentinel_is_not_serialized(self):
        payload = json.dumps(self.report(), ensure_ascii=False)
        self.assertNotIn("DO_NOT_EXPORT_DEMO_SECRET_7f3b", payload)
        self.assertNotRegex(payload, re.compile(r"raw_prompt|token|password", re.I))

    def test_missing_positive_receipt_prevents_success_claim(self):
        sample = copy.deepcopy(self.by_name(self.report(), "success"))
        sample["evidence"]["business"]["authoritative_receipt"] = None
        self.assertEqual("evidence_conflict", lab.diagnose(sample)["verdict"])

    def test_cli_writes_json_and_returns_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(0, lab.main(["--all", "--output-dir", tmp]))
            path = Path(tmp) / "observability-report.json"
            report = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(5, report["metrics"]["runs_total"])
            self.assertNotIn("DO_NOT_EXPORT_DEMO_SECRET_7f3b", path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
