"""Offline, deterministic multi-agent contract lab; Python 3.11+ standard library."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import asdict, dataclass
import hashlib
import json
from typing import Any


READ_CAPABILITIES = frozenset({"search_knowledge", "read_diagnostics", "read_policy"})
SINGLE_VALUE_CLAIMS = frozenset({"primary_error", "ticket_allowed"})
ROLES = {
    "knowledge": (False, "search_knowledge", "recommended_step"),
    "diagnosis": (True, "read_diagnostics", "primary_error"),
    "policy": (True, "read_policy", "ticket_allowed"),
}
IDENTITY_FIELDS = (
    "tenant_id", "run_id", "task_id", "attempt_id", "agent_id",
    "plan_version", "input_digest",
)
RESULT_FIELDS = frozenset(IDENTITY_FIELDS) | {
    "status", "findings", "usage_tokens",
}


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(",", ":")).encode()).hexdigest()


@dataclass(frozen=True)
class TaskContract:
    tenant_id: str
    run_id: str
    task_id: str
    attempt_id: str
    agent_id: str
    plan_version: int
    input_digest: str
    required: bool
    capabilities: tuple[str, ...]
    allowed_claims: tuple[str, ...]
    token_limit: int


@dataclass(frozen=True)
class Evidence:
    tenant_id: str
    run_id: str
    claim: str
    value: str


class Budget:
    """Single-thread teaching ledger. Settlement comes from trusted harness metering."""

    def __init__(self, limit: int):
        self.limit = limit
        self.spent = 0
        self.reserved: dict[str, int] = {}
        self.settled: dict[str, int] = {}

    @property
    def available(self) -> int:
        return self.limit - self.spent - sum(self.reserved.values())

    def reserve(self, attempt_id: str, amount: int) -> None:
        if type(amount) is not int or amount <= 0:
            raise ValueError("invalid_reservation")
        if attempt_id in self.reserved or attempt_id in self.settled:
            raise ValueError("attempt_already_budgeted")
        if amount > self.available:
            raise ValueError("parent_budget_exhausted")
        self.reserved[attempt_id] = amount

    def settle(self, attempt_id: str, trusted_usage: int) -> str:
        if type(trusted_usage) is not int or trusted_usage < 0:
            raise ValueError("invalid_metering")
        if attempt_id in self.settled:
            if self.settled[attempt_id] != trusted_usage:
                raise ValueError("conflicting_metering_receipt")
            return "DUPLICATE_METERING"
        if attempt_id not in self.reserved:
            raise ValueError("unknown_attempt")
        reserved = self.reserved.pop(attempt_id)
        self.spent += trusted_usage  # Even a cap violation is real consumption.
        self.settled[attempt_id] = trusted_usage
        return "CAP_EXCEEDED" if trusted_usage > reserved else "SETTLED"


class Coordinator:
    """Trusted parent owns contracts, acceptance, join policy and the final draft gate."""

    def __init__(self, *, tenant_id: str = "demo-company", run_id: str = "vpn-001",
                 token_budget: int = 900):
        self.tenant_id = tenant_id
        self.run_id = run_id
        self.plan_version = 1
        self.input_digest = digest({"issue": "VPN E104", "os": "Windows",
                                    "client_version": "5.2"})
        self.cancelled = False
        self.budget = Budget(token_budget)
        self.contracts: dict[str, TaskContract] = {}
        self.results: dict[str, dict[str, Any]] = {}
        self.audit: list[dict[str, str]] = []
        # Trusted fixture evidence stands in for scoped, versioned source reads.
        self.evidence = {
            "kb-3": Evidence(tenant_id, run_id, "recommended_step", "check_certificate_validity"),
            "diag-17": Evidence(tenant_id, run_id, "primary_error", "tls_handshake_timeout"),
            "policy-4": Evidence(tenant_id, run_id, "ticket_allowed", "true"),
            "diag-18": Evidence(tenant_id, run_id, "primary_error", "connection_refused"),
        }

    def dispatch(self, role: str, *, requested_capabilities: tuple[str, ...] | None = None,
                 token_limit: int = 200) -> TaskContract:
        if self.cancelled:
            raise ValueError("run_cancelled")
        if role not in ROLES:
            raise ValueError("unknown_role")
        if role in self.contracts:
            raise ValueError("task_already_dispatched")
        required, capability, allowed_claim = ROLES[role]
        capabilities = requested_capabilities or (capability,)
        # An agent's requested role or text cannot mint execution permissions.
        if not set(capabilities) <= READ_CAPABILITIES or set(capabilities) != {capability}:
            raise ValueError("capability_denied")
        attempt_id = f"{role}-a1"
        contract = TaskContract(
            self.tenant_id, self.run_id, role, attempt_id, f"{role}-agent",
            self.plan_version, self.input_digest, required, tuple(capabilities),
            (allowed_claim,), token_limit,
        )
        self.budget.reserve(attempt_id, token_limit)
        self.contracts[role] = contract
        return contract

    def _record(self, code: str, task_id: str) -> str:
        self.audit.append({"code": code, "task_id": task_id})
        return code

    def accept(self, candidate: Any) -> str:
        """Candidate data never modifies scope, permissions, plan or metered spending."""
        if type(candidate) is not dict or set(candidate) != RESULT_FIELDS:
            return self._record("INVALID_SCHEMA", "unknown")
        task_id = candidate["task_id"]
        if type(task_id) is not str or task_id not in self.contracts:
            return self._record("UNKNOWN_TASK", "unknown")
        contract = self.contracts[task_id]
        expected = asdict(contract)
        for field in IDENTITY_FIELDS:
            if type(candidate[field]) is not type(expected[field]) or candidate[field] != expected[field]:
                return self._record("CONTRACT_MISMATCH", task_id)
        if self.cancelled:
            return self._record("LATE_CANCELLED", task_id)
        if contract.plan_version != self.plan_version or contract.input_digest != self.input_digest:
            return self._record("STALE_RESULT", task_id)
        if (candidate["status"] not in ("succeeded", "failed")
                or type(candidate["status"]) is not str
                or type(candidate["usage_tokens"]) is not int
                or candidate["usage_tokens"] < 0
                or type(candidate["findings"]) is not list
                or len(candidate["findings"]) > 8):
            return self._record("INVALID_SCHEMA", task_id)
        if candidate["status"] == "failed" and candidate["findings"]:
            return self._record("INVALID_SCHEMA", task_id)
        for finding in candidate["findings"]:
            if (type(finding) is not dict
                    or set(finding) != {"claim", "value", "evidence_ref"}
                    or any(type(v) is not str or not v or len(v) > 200 for v in finding.values())):
                return self._record("INVALID_SCHEMA", task_id)
            if finding["claim"] not in contract.allowed_claims:
                return self._record("RESULT_SCOPE_REJECTED", task_id)
            source = self.evidence.get(finding["evidence_ref"])
            if (source is None or source.tenant_id != contract.tenant_id
                    or source.run_id != contract.run_id
                    or source.claim != finding["claim"] or source.value != finding["value"]):
                return self._record("EVIDENCE_REJECTED", task_id)
        if task_id in self.results:
            code = "DUPLICATE" if self.results[task_id] == candidate else "CONFLICTING_DUPLICATE"
            return self._record(code, task_id)
        self.results[task_id] = deepcopy(candidate)
        return self._record("ACCEPTED", task_id)

    def outcome(self) -> str:
        if self.cancelled:
            return "CANCELLED"
        # A replan must issue new contracts; this lab intentionally stops here.
        if any(c.plan_version != self.plan_version or c.input_digest != self.input_digest
               for c in self.contracts.values()):
            return "WAITING"
        if set(self.contracts) != set(ROLES):
            return "WAITING"
        if any(c.required and self.results.get(role, {}).get("status") == "failed"
               for role, c in self.contracts.items()):
            return "BLOCKED"
        if any(role not in self.results for role in self.contracts):
            return "WAITING"
        claims: dict[str, set[str]] = {}
        for result in self.results.values():
            for finding in result["findings"]:
                claims.setdefault(finding["claim"], set()).add(finding["value"])
        if any(len(values) > 1 for claim, values in claims.items()
               if claim in SINGLE_VALUE_CLAIMS):
            return "NEEDS_REVIEW"
        # Success of a child alone is insufficient: the required business facts must exist.
        if (not claims.get("primary_error") or claims.get("ticket_allowed") != {"true"}):
            return "BLOCKED"
        degraded = any(not c.required and self.results[role]["status"] == "failed"
                       for role, c in self.contracts.items())
        return "READY_DRAFT_DEGRADED" if degraded else "READY_DRAFT"


def result_for(contract: TaskContract, *, failed: bool = False) -> dict[str, Any]:
    references = {"knowledge": ("recommended_step", "check_certificate_validity", "kb-3"),
                  "diagnosis": ("primary_error", "tls_handshake_timeout", "diag-17"),
                  "policy": ("ticket_allowed", "true", "policy-4")}
    claim, value, reference = references[contract.task_id]
    return {
        **{field: getattr(contract, field) for field in IDENTITY_FIELDS},
        "status": "failed" if failed else "succeeded",
        "findings": [] if failed else [{"claim": claim, "value": value,
                                         "evidence_ref": reference}],
        "usage_tokens": 40 if failed else 100,
    }


async def fake_expert(contract: TaskContract, *, failed: bool = False) -> dict[str, Any]:
    await asyncio.sleep(0)  # Cooperative concurrency, no network and no claimed speed benchmark.
    return result_for(contract, failed=failed)


async def run_demo(*, failed_role: str | None = None, conflict: bool = False,
                   cancel_before_delivery: bool = False, replan_before_delivery: bool = False) -> dict[str, Any]:
    parent = Coordinator()
    contracts = [parent.dispatch(role) for role in ROLES]
    results = await asyncio.gather(*(fake_expert(c, failed=c.task_id == failed_role)
                                     for c in contracts))
    if cancel_before_delivery:
        parent.cancelled = True
    if replan_before_delivery:
        parent.plan_version += 1
    if conflict:
        results[1]["findings"].append(
            {"claim": "primary_error", "value": "connection_refused", "evidence_ref": "diag-18"})
    for contract, result in zip(contracts, results):
        # This value represents harness metering, not trust in result["usage_tokens"].
        parent.budget.settle(contract.attempt_id, 40 if contract.task_id == failed_role else 100)
        parent.accept(result)
    duplicate = parent.accept(results[0])
    try:
        Coordinator().dispatch("policy", requested_capabilities=("create_ticket",))
    except ValueError as exc:
        write_request = str(exc)
    return {"outcome": parent.outcome(), "accepted": len(parent.results),
            "duplicate_delivery": duplicate, "metered_tokens": parent.budget.spent,
            "reserved_tokens": sum(parent.budget.reserved.values()),
            "write_request": write_request, "audit": parent.audit}


async def main() -> None:
    scenarios = {
        "normal": {},
        "optional_failure": {"failed_role": "knowledge"},
        "required_failure": {"failed_role": "policy"},
        "conflicting_evidence": {"conflict": True},
        "cancelled": {"cancel_before_delivery": True},
        "stale_plan": {"replan_before_delivery": True},
    }
    for name, options in scenarios.items():
        print(json.dumps({"scenario": name, **await run_demo(**options)}, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
