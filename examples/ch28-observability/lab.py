#!/usr/bin/env python3
"""Deterministic observability lab; Python 3.11+, standard library only.

This is a teaching event model, not an OpenTelemetry SDK implementation.
"""
from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import re
import sqlite3
import sys
from typing import Any, Iterable


LAB_VERSION = "ch28-observability-lab-v1"
SCHEMA_VERSION = "diagnostic-report-v1"
RUNTIME_VERSION = "teaching-runtime-v1"
DIAGNOSTIC_VERSION = "evidence-rules-v1"
SCENARIOS = ("success", "slow", "lost-receipt", "missing-trace", "telemetry-drop")
SLOW_RUN_MS = 2_000
TENANT_REF = "tenant-teaching-a"
REQUESTER_REF = "requester-teaching-a"
APPROVED_REVISION = "exact-command-v1"
RECEIPT_AUTHORITY = "teaching-ticket-system"
JOURNAL_VERSION = "controlled-tool-journal-fixture-v1"
HEX_32 = re.compile(r"^[0-9a-f]{32}$")
HEX_16 = re.compile(r"^[0-9a-f]{16}$")


class LabError(Exception):
    """Invalid fixture or report evidence."""


class LostReceipt(Exception):
    """The write may have completed, but its direct reply was not received."""


def manifest() -> dict[str, str]:
    return {
        "lab_version": LAB_VERSION,
        "schema_version": SCHEMA_VERSION,
        "runtime_version": RUNTIME_VERSION,
        "diagnostic_version": DIAGNOSTIC_VERSION,
        "timing": "deterministic_simulation",
    }


def synthetic_ids(index: int) -> dict[str, str]:
    """Create stable synthetic IDs without deriving them from user content."""
    return {
        "run_id": f"run-{index:02d}",
        "attempt_id": f"attempt-{index:02d}-01",
        "trace_id": f"{index:032x}",
        "root_span_id": f"{index * 1_000:016x}",
        "operation_id": f"vpn-ticket-op-{index:02d}",
    }


def capture_attributes(request: dict[str, Any]) -> dict[str, Any]:
    """Allowlist low-risk diagnostic fields; never recursively copy the request."""
    captured: dict[str, Any] = {}
    if request.get("category") in {"vpn", "network"}:
        captured["request.category"] = request["category"]
    if request.get("locale") in {"zh-CN", "en-US"}:
        captured["request.locale"] = request["locale"]
    if type(request.get("approved")) is bool:
        captured["request.approved"] = request["approved"]
    tool = request.get("tool")
    if isinstance(tool, dict) and tool.get("name") in {"knowledge_search", "ticket_create"}:
        captured["tool.name"] = tool["name"]
    return captured


class ReceiptStore:
    """Authoritative business receipts, deliberately separate from telemetry."""

    def __init__(self) -> None:
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.db.execute(
            """CREATE TABLE receipts (
                 operation_id TEXT PRIMARY KEY,
                 ticket_id TEXT NOT NULL,
                 outcome TEXT NOT NULL,
                 authority TEXT NOT NULL,
                 tenant_ref TEXT NOT NULL,
                 requester_ref TEXT NOT NULL,
                 approved_revision TEXT NOT NULL
               )"""
        )
        self.db.commit()

    def record_created(self, operation_id: str, ticket_id: str) -> None:
        self.db.execute(
            "INSERT INTO receipts VALUES (?, ?, 'created', ?, ?, ?, ?)",
            (operation_id, ticket_id, RECEIPT_AUTHORITY, TENANT_REF,
             REQUESTER_REF, APPROVED_REVISION),
        )
        self.db.commit()

    def lookup(self, operation_id: str) -> dict[str, str] | None:
        row = self.db.execute(
            """SELECT operation_id,ticket_id,outcome,authority,tenant_ref,
                      requester_ref,approved_revision
                 FROM receipts WHERE operation_id=?""",
            (operation_id,),
        ).fetchone()
        return dict(row) if row else None

    def close(self) -> None:
        self.db.close()


class ControlledTicketTools:
    """Teaching tool boundary that records calls outside the simulated request."""

    def __init__(self, store: ReceiptStore, ids: dict[str, str], ticket_id: str):
        self.store = store
        self.ids = ids
        self.ticket_id = ticket_id
        self.events: list[dict[str, Any]] = []

    def event(self, kind: str, action: str, method: str, **values: Any) -> None:
        self.events.append({
            "seq": len(self.events),
            "kind": kind,
            "action": action,
            "method": method,
            "operation_id": self.ids["operation_id"],
            "tenant_ref": TENANT_REF,
            "requester_ref": REQUESTER_REF,
            "approved_revision": APPROVED_REVISION,
            **values,
        })

    def create_ticket(self, *, effect_committed: bool, lose_receipt: bool) -> dict[str, str]:
        self.event("tool_call", "ticket.create", "write")
        if effect_committed:
            self.store.record_created(self.ids["operation_id"], self.ticket_id)
        if lose_receipt:
            self.event("tool_result", "ticket.create", "write", result="outcome_unknown")
            raise LostReceipt()
        receipt = self.store.lookup(self.ids["operation_id"])
        if receipt is None:
            raise LabError("acknowledged create has no authoritative receipt")
        self.event("tool_result", "ticket.create", "write", result="acknowledged",
                   ticket_id=receipt["ticket_id"])
        return receipt

    def lookup_by_operation_id(self) -> tuple[dict[str, str] | None, dict[str, str]]:
        self.event("tool_call", "ticket.lookup_by_operation", "read")
        receipt = self.store.lookup(self.ids["operation_id"])
        result = "found" if receipt else "not_found_inconclusive"
        self.event("tool_result", "ticket.lookup_by_operation", "read", result=result,
                   **({"ticket_id": receipt["ticket_id"]} if receipt else {}))
        return receipt, {"method": "read_by_operation_id", "result": result}

    def snapshot(self) -> dict[str, Any]:
        return {"version": JOURNAL_VERSION, "events": list(self.events)}


def operation_summary(events: list[dict[str, Any]], operation_id: str) -> dict[str, int]:
    calls = [event for event in events
             if event.get("kind") == "tool_call" and event.get("operation_id") == operation_id]
    writes = sum(event.get("action") == "ticket.create" and event.get("method") == "write"
                 for event in calls)
    reads = sum(event.get("action") == "ticket.lookup_by_operation"
                and event.get("method") == "read" for event in calls)
    return {
        "write_attempts": writes,
        "write_retries": max(0, writes - 1),
        "read_reconciliations": reads,
    }


def span(
    ids: dict[str, str], suffix: int, name: str, start_ms: int, end_ms: int,
    parent_span_id: str | None,
) -> dict[str, Any]:
    return {
        "trace_id": ids["trace_id"],
        "span_id": f"{suffix:016x}",
        "parent_span_id": parent_span_id,
        "name": name,
        "start_ms": start_ms,
        "end_ms": end_ms,
        "duration_ms": end_ms - start_ms,
    }


def trace_for(name: str, ids: dict[str, str]) -> dict[str, Any]:
    root = ids["root_span_id"]
    child = int(root, 16)
    if name == "missing-trace":
        return {"sampled": False, "export_status": "not_sampled", "complete": False, "spans": []}
    if name == "telemetry-drop":
        return {"sampled": True, "export_status": "dropped", "complete": False, "spans": []}
    if name == "slow":
        spans = [
            span(ids, child, "agent.run", 500, 2_300, None),
            span(ids, child + 1, "knowledge.retrieval", 650, 1_650, root),
            span(ids, child + 2, "model.generate", 1_650, 1_950, root),
            span(ids, child + 3, "ticket.create", 1_950, 2_150, root),
        ]
    elif name == "lost-receipt":
        spans = [
            span(ids, child, "agent.run", 0, 1_400, None),
            span(ids, child + 1, "ticket.create", 300, 900, root),
            span(ids, child + 2, "ticket.lookup", 1_000, 1_250, root),
        ]
    else:
        spans = [
            span(ids, child, "agent.run", 0, 720, None),
            span(ids, child + 1, "knowledge.retrieval", 80, 230, root),
            span(ids, child + 2, "model.generate", 230, 480, root),
            span(ids, child + 3, "ticket.create", 480, 650, root),
        ]
    return {"sampled": True, "export_status": "exported", "complete": True, "spans": spans}


def fixture_request(extra: dict[str, Any] | None = None) -> dict[str, Any]:
    request: dict[str, Any] = {
        "category": "vpn",
        "locale": "zh-CN",
        "approved": True,
        "tool": {"name": "ticket_create", "arguments": {"raw_title": "VPN 无法连接"}},
        "raw_prompt": "请提交 VPN 报修；DO_NOT_EXPORT_DEMO_SECRET_7f3b",
        "auth": {"token": "DO_NOT_EXPORT_DEMO_SECRET_7f3b"},
    }
    if extra:
        request.update(extra)
    return request


def simulate(name: str, index: int, store: ReceiptStore,
             request: dict[str, Any] | None = None) -> dict[str, Any]:
    if name not in SCENARIOS:
        raise LabError(f"unknown scenario: {name}")
    ids = synthetic_ids(index)
    ticket_id = f"TICKET-{index:04d}"
    duration = {"success": 720, "slow": 2_300, "lost-receipt": 1_400,
                "missing-trace": 810, "telemetry-drop": 950}[name]
    queue_wait = 500 if name == "slow" else 0
    dispatch_state = "ACKNOWLEDGED"
    final_state = "SUCCEEDED"
    tools = ControlledTicketTools(store, ids, ticket_id)
    reconciliation = {"method": "not_requested", "result": "not_requested"}
    receipt: dict[str, str] | None
    if name in {"lost-receipt", "telemetry-drop"}:
        try:
            tools.create_ticket(effect_committed=name == "lost-receipt", lose_receipt=True)
            raise LabError("lost-receipt fixture did not lose its receipt")
        except LostReceipt:
            dispatch_state = "UNKNOWN"
            receipt, reconciliation = tools.lookup_by_operation_id()
            if receipt is None:
                final_state = "UNKNOWN"
    else:
        receipt = tools.create_ticket(effect_committed=True, lose_receipt=False)

    journal = tools.snapshot()
    summary = operation_summary(journal["events"], ids["operation_id"])
    run_record = {
        **ids,
        "tenant_ref": TENANT_REF,
        "requester_ref": REQUESTER_REF,
        "approved_revision": APPROVED_REVISION,
        "final_state": final_state,
        "dispatch_state": dispatch_state,
        "verified_ticket_id": receipt["ticket_id"] if receipt and final_state == "SUCCEEDED" else None,
        "duration_ms": duration,
        "queue_wait_ms": queue_wait,
        **summary,
    }
    business = {
        "authoritative_receipt": receipt,
        "reconciliation": reconciliation,
    }
    sample = {
        "scenario": name,
        "manifest": manifest(),
        "captured_attributes": capture_attributes(request or fixture_request()),
        "evidence": {
            "run_record": run_record,
            "operation_journal": journal,
            "telemetry": trace_for(name, ids),
            "business": business,
        },
    }
    sample["diagnosis"] = diagnose(sample)
    return sample


def validate_identity(run: dict[str, Any], telemetry: dict[str, Any]) -> None:
    for field in ("run_id", "attempt_id", "operation_id"):
        if not isinstance(run.get(field), str) or not run[field]:
            raise LabError(f"{field} must be a nonempty string")
    if not HEX_32.fullmatch(str(run.get("trace_id", ""))):
        raise LabError("trace_id must be 32 lowercase hexadecimal characters")
    if not HEX_16.fullmatch(str(run.get("root_span_id", ""))):
        raise LabError("root_span_id must be 16 lowercase hexadecimal characters")
    for item in telemetry.get("spans", []):
        if item.get("trace_id") != run["trace_id"] or not HEX_16.fullmatch(str(item.get("span_id", ""))):
            raise LabError("span identity does not match the run")


def latency_analysis(run: dict[str, Any], telemetry: dict[str, Any]) -> dict[str, Any]:
    spans = telemetry.get("spans", [])
    root = next((s for s in spans if s.get("span_id") == run["root_span_id"]), None)
    children = [s for s in spans if s.get("parent_span_id") == run["root_span_id"]]
    dominant = max(children, key=lambda s: s["duration_ms"], default=None)
    result: dict[str, Any] = {
        "run_duration_ms": run["duration_ms"],
        "queue_wait_ms": run["queue_wait_ms"],
        "root_span_elapsed_ms": root["duration_ms"] if root else None,
        "method": "root_wall_clock_elapsed_plus_explicit_queue; this_is_not_a_general_critical_path_algorithm",
        "classification": "slow" if run["duration_ms"] >= SLOW_RUN_MS else "within_teaching_threshold",
    }
    if dominant:
        result["dominant_child_span"] = dominant["name"]
        result["dominant_child_ms"] = dominant["duration_ms"]
    return result


def diagnose(sample: dict[str, Any]) -> dict[str, Any]:
    """Infer only from evidence. The scenario label is intentionally ignored."""
    evidence = sample.get("evidence")
    if not isinstance(evidence, dict):
        raise LabError("sample has no evidence")
    run = evidence.get("run_record", {})
    telemetry = evidence.get("telemetry", {})
    business = evidence.get("business", {})
    journal = evidence.get("operation_journal", {})
    validate_identity(run, telemetry)
    events = journal.get("events") if isinstance(journal, dict) else None
    if not isinstance(events, list):
        events = []
    sequence_valid = journal.get("version") == JOURNAL_VERSION and all(
        isinstance(event, dict) and event.get("seq") == index
        for index, event in enumerate(events)
    )
    computed_summary = operation_summary(events, run.get("operation_id"))
    summary_consistent = all(run.get(field) == value for field, value in computed_summary.items())
    calls = [event for event in events if event.get("kind") == "tool_call"
             and event.get("operation_id") == run.get("operation_id")]
    create_calls = [event for event in calls
                    if event.get("action") == "ticket.create" and event.get("method") == "write"]
    read_calls = [event for event in calls
                  if event.get("action") == "ticket.lookup_by_operation"
                  and event.get("method") == "read"]
    binding = {"tenant_ref": TENANT_REF, "requester_ref": REQUESTER_REF,
               "approved_revision": APPROVED_REVISION}
    run_binding_matches = all(run.get(field) == value for field, value in binding.items())
    journal_binding_matches = bool(events) and all(
        all(event.get(field) == value for field, value in binding.items()) for event in events
    )
    receipt = business.get("authoritative_receipt")
    authority_matches = isinstance(receipt, dict) and receipt.get("authority") == RECEIPT_AUTHORITY
    receipt_binding_matches = receipt is None or (
        isinstance(receipt, dict)
        and all(receipt.get(field) == value for field, value in binding.items())
    )
    receipt_matches = bool(
        isinstance(receipt, dict)
        and receipt.get("operation_id") == run.get("operation_id")
        and receipt.get("outcome") == "created"
        and receipt.get("ticket_id") == run.get("verified_ticket_id")
        and authority_matches
        and receipt_binding_matches
    )
    base_operation_safe = bool(
        sequence_valid and summary_consistent and run_binding_matches and journal_binding_matches
        and len(create_calls) == 1 and computed_summary["write_retries"] == 0
    )
    reconciliation = business.get("reconciliation", {})
    lookup_results = [event for event in events
                      if event.get("kind") == "tool_result"
                      and event.get("action") == "ticket.lookup_by_operation"
                      and event.get("method") == "read" and event.get("result") == "found"]
    safe_reconciliation = bool(
        base_operation_safe and len(read_calls) == 1 and len(lookup_results) == 1
        and reconciliation.get("method") == "read_by_operation_id"
        and reconciliation.get("result") == "found"
    )
    trace_complete = bool(telemetry.get("complete") and telemetry.get("export_status") == "exported")
    gaps: list[str] = []
    if not trace_complete:
        gaps.append("complete_trace_unavailable")

    if run.get("final_state") == "SUCCEEDED" and receipt_matches and base_operation_safe:
        if run.get("dispatch_state") == "UNKNOWN" and safe_reconciliation:
            verdict = "completed_after_read_only_reconciliation"
            explanation = "派发回执丢失后，按原 operation_id 只读核实到权威工单；没有重新写入。"
        elif run.get("dispatch_state") == "UNKNOWN":
            verdict = "evidence_conflict"
            explanation = "运行声称已恢复，但操作记录不能证明只有一次写入且恢复调用为只读。"
            gaps.append("safe_reconciliation_not_proven")
        else:
            verdict = "completed_verified"
            explanation = "运行终态与权威业务回执一致。"
    elif run.get("final_state") == "UNKNOWN":
        verdict = "unknown"
        explanation = "没有可证明成功或失败的权威回执；阴性查询不足以证明写入从未发生。"
        gaps.append("positive_business_receipt_unavailable")
    else:
        verdict = "evidence_conflict"
        explanation = "运行终态与权威业务证据不一致，需要人工复核。"
        gaps.append("run_and_business_state_conflict")

    if not sequence_valid:
        gaps.append("operation_journal_invalid")
    if not summary_consistent:
        gaps.append("run_operation_summary_mismatch")
    if not run_binding_matches or not journal_binding_matches or not receipt_binding_matches:
        gaps.append("operation_binding_mismatch")
    if receipt is not None and not authority_matches:
        gaps.append("receipt_authority_mismatch")

    return {
        "verdict": verdict,
        "explanation": explanation,
        "evidence_gaps": sorted(set(gaps)),
        "safe_next_action": (
            "continue_read_only_reconciliation_or_human_review"
            if verdict in {"unknown", "evidence_conflict"}
            else "none"
        ),
        "redispatch_allowed": False if run.get("dispatch_state") == "UNKNOWN" else None,
        "operation_checks": {
            "journal_sequence_valid": sequence_valid,
            "summary_consistent": summary_consistent,
            "create_dispatches": len(create_calls),
            "read_reconciliations": len(read_calls),
            "binding_matches": run_binding_matches and journal_binding_matches and receipt_binding_matches,
            "receipt_authority_matches": authority_matches,
        },
        "latency": latency_analysis(run, telemetry),
    }


def percentile_nearest_rank(values: list[int], percentile: float) -> int:
    ordered = sorted(values)
    rank = max(1, int(len(ordered) * percentile + 0.999999))
    return ordered[min(rank, len(ordered)) - 1]


def aggregate_metrics(samples: Iterable[dict[str, Any]]) -> dict[str, Any]:
    rows = list(samples)
    if not rows:
        raise LabError("metrics require at least one run")
    runs = [row["evidence"]["run_record"] for row in rows]
    states = Counter(run["final_state"] for run in runs)
    durations = [int(run["duration_ms"]) for run in runs]
    exported = sum(
        row["evidence"]["telemetry"].get("export_status") == "exported" for row in rows
    )
    total = len(runs)
    return {
        "source": "trusted_run_records",
        "timing": "deterministic_simulation",
        "runs_total": total,
        "status_counts": dict(sorted(states.items())),
        "unknown_runs": states.get("UNKNOWN", 0),
        "unknown_rate": states.get("UNKNOWN", 0) / total,
        "latency_ms": {
            "denominator_runs": total,
            "p50": percentile_nearest_rank(durations, 0.50),
            "p95": percentile_nearest_rank(durations, 0.95),
        },
        "trace_exports": {"count": exported, "denominator_runs": total},
        "note": "业务指标分母来自全量运行记录；Trace 采样或丢失不会移除运行。",
    }


def build_report(names: Iterable[str], request: dict[str, Any] | None = None) -> dict[str, Any]:
    store = ReceiptStore()
    try:
        samples = [simulate(name, index, store, request) for index, name in enumerate(names, 1)]
    finally:
        store.close()
    return {"manifest": manifest(), "samples": samples, "metrics": aggregate_metrics(samples)}


def write_report(report: dict[str, Any], output_dir: Path, filename: str) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / filename
    payload = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    path.write_text(payload, encoding="utf-8")
    return path


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the chapter 28 offline observability lab")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--all", action="store_true", help="run all five teaching scenarios")
    mode.add_argument("--scenario", choices=SCENARIOS, help="run one teaching scenario")
    parser.add_argument("--output-dir", type=Path, default=Path(__file__).with_name("var"))
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    names = SCENARIOS if args.all else (args.scenario,)
    try:
        report = build_report(names)
        filename = "observability-report.json" if args.all else f"{args.scenario}.json"
        path = write_report(report, args.output_dir, filename)
    except (LabError, OSError, sqlite3.Error, TypeError, ValueError) as exc:
        print(f"lab error: {exc}", file=sys.stderr)
        return 2
    for sample in report["samples"]:
        run = sample["evidence"]["run_record"]
        diagnosis = sample["diagnosis"]
        print(
            f"{run['run_id']} {sample['scenario']}: {diagnosis['verdict']}; "
            f"duration={run['duration_ms']}ms; trace={sample['evidence']['telemetry']['export_status']}"
        )
    print(
        f"metrics: runs={report['metrics']['runs_total']}; "
        f"unknown={report['metrics']['unknown_runs']}; "
        f"trace_exports={report['metrics']['trace_exports']['count']}; report={path}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
