"""Offline teaching model: admission, settlement, routing and scoped result reuse.

All amounts are integer teaching microcredits, not vendor prices. No network I/O.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace
import json
from threading import Lock


class BudgetDenied(RuntimeError):
    pass


@dataclass
class Reservation:
    held: int
    state: str = "reserved"
    actual: int | None = None
    overrun: int = 0


class Ledger:
    """One process, one shared ledger; the lock is not a distributed transaction."""

    def __init__(self, limit: int):
        self._amount(limit)
        self.limit = limit
        self.spent = 0
        self.breached = False
        self._entries: dict[str, Reservation] = {}
        self._lock = Lock()

    @staticmethod
    def _amount(value: int) -> None:
        if type(value) is not int or value < 0:
            raise ValueError("amount must be a non-negative integer")

    def _held(self) -> int:
        return sum(e.held for e in self._entries.values() if e.state != "settled")

    def reserve(self, key: str, amount: int) -> None:
        self._amount(amount)
        with self._lock:
            if key in self._entries:
                entry = self._entries[key]
                if entry.held != amount:
                    raise ValueError("reservation key reused with a different amount")
                if entry.state != "reserved":
                    raise ValueError("unknown or settled reservation cannot authorize dispatch")
                return
            if self.breached or self.spent + self._held() + amount > self.limit:
                raise BudgetDenied("shared budget cannot admit this attempt")
            self._entries[key] = Reservation(amount)

    def mark_unknown(self, key: str) -> None:
        with self._lock:
            entry = self._entries[key]
            if entry.state == "settled":
                raise ValueError("settled usage cannot become unknown")
            entry.state = "unknown"

    def _finish(self, key: str, actual: int, *, reconcile: bool) -> None:
        self._amount(actual)
        with self._lock:
            entry = self._entries[key]
            if entry.state == "settled":
                if entry.actual != actual:
                    raise ValueError("conflicting settlement; investigate instead of overwriting")
                return
            required = "unknown" if reconcile else "reserved"
            if entry.state != required:
                raise ValueError(f"expected {required}; explicit reconciliation is required")
            entry.actual = actual
            entry.overrun = max(0, actual - entry.held)
            entry.state = "settled"
            self.spent += actual  # Never clamp a vendor's actual usage to the estimate.
            self.breached = self.breached or entry.overrun > 0

    def settle(self, key: str, actual: int) -> None:
        self._finish(key, actual, reconcile=False)

    def reconcile(self, key: str, actual: int) -> None:
        self._finish(key, actual, reconcile=True)

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "limit": self.limit,
                "spent": self.spent,
                "reserved": self._held(),
                "admission_blocked": self.breached,
                "entries": {k: asdict(v) for k, v in self._entries.items()},
            }


@dataclass(frozen=True)
class Scope:
    tenant: str
    principal: str
    authz_epoch: str


@dataclass(frozen=True)
class Task:
    run_id: str
    scope: Scope
    request: str
    region: str = "eu"
    capabilities: frozenset[str] = frozenset({"structured_output"})
    prompt_version: str = "prompt-v1"
    evidence_version: str = "manual-v1"
    policy_version: str = "route-v1"
    output_schema_version: str = "schema-v1"
    result_contract_version: str = "result-contract-v1"
    model_config_version: str = "model-config-v1"
    read_only: bool = True
    cancelled: bool = False
    effect_certainty: str = "none"

    def __post_init__(self):
        if self.effect_certainty not in {"none", "confirmed", "unknown"}:
            raise ValueError("invalid effect certainty")


class ResultCache:
    """Exact, private, read-only result reuse; caller authenticates before lookup."""

    def __init__(self):
        self._values: dict[tuple, str] = {}

    @staticmethod
    def key(task: Task) -> tuple:
        if not task.read_only:
            raise ValueError("business write results are not reusable answers")
        return (
            task.scope, task.request, task.region, task.capabilities,
            task.prompt_version, task.evidence_version, task.policy_version,
            task.output_schema_version, task.result_contract_version, task.model_config_version,
        )

    def get(self, task: Task) -> str | None:
        return self._values.get(self.key(task))

    def put(self, task: Task, answer: str) -> None:
        self._values[self.key(task)] = answer


@dataclass(frozen=True)
class Model:
    name: str
    region: str
    capabilities: frozenset[str]
    reservation: int


@dataclass(frozen=True)
class SimulatedOutcome:
    quality_pass: bool
    actual: int | None  # None means usage is unknown, not free.
    cancelled_after: bool = False
    effect_after: str = "none"

    def __post_init__(self):
        if self.effect_after not in {"none", "confirmed", "unknown"}:
            raise ValueError("invalid effect certainty")
        if self.actual is not None:
            Ledger._amount(self.actual)


def continuation_stop(cancelled: bool, effect_certainty: str, *, read_only: bool) -> str | None:
    if effect_certainty == "unknown":
        return "reconciliation_required"
    if effect_certainty == "confirmed":
        return "effect_contract_breached" if read_only else "write_effect_confirmed"
    if cancelled:
        return "cancelled"
    return None


class Router:
    """Fixed two-attempt exercise, not a trained production quality router."""

    def __init__(self, models: list[Model], ledger: Ledger, cache: ResultCache):
        self.models, self.ledger, self.cache = models, ledger, cache
        self._started_runs: set[str] = set()

    def run(self, task: Task, outcomes: dict[str, SimulatedOutcome]) -> dict:
        if task.run_id in self._started_runs:
            raise ValueError("demo cannot resume or replay a run; use a fresh run_id")
        self._started_runs.add(task.run_id)
        result = {
            "run_id": task.run_id, "attempts": [], "status": "",
            "usage_state": "not_dispatched", "effect_certainty": task.effect_certainty,
            "cancel_requested": task.cancelled, "quality_state": "not_checked",
        }
        stop = continuation_stop(task.cancelled, task.effect_certainty, read_only=task.read_only)
        if stop:
            return {**result, "status": stop}
        if task.read_only:
            cached = self.cache.get(task)
            if cached is not None:
                return {**result, "status": "cache_hit", "answer": cached}

        # Capabilities and region are hard filters, before cost ordering.
        candidates = sorted(
            (m for m in self.models if m.region == task.region
             and task.capabilities <= m.capabilities),
            key=lambda m: (m.reservation, m.name),
        )
        if not candidates:
            return {**result, "status": "no_eligible_model"}

        # Business writes never use quality failure as a reason to dispatch again.
        attempt_limit = 2 if task.read_only else 1
        for attempt, model in enumerate(candidates[:attempt_limit], start=1):
            key = f"{task.run_id}:attempt-{attempt}"
            try:
                self.ledger.reserve(key, model.reservation)
            except BudgetDenied:
                return {**result, "status": "budget_exhausted"}
            outcome = outcomes[model.name]
            result["attempts"].append({"model": model.name, "reservation_key": key})
            result["usage_state"] = "unknown" if outcome.actual is None else "confirmed"
            result["effect_certainty"] = outcome.effect_after
            result["cancel_requested"] = outcome.cancelled_after
            result["quality_state"] = "passed" if outcome.quality_pass else "failed"
            if outcome.actual is None:
                self.ledger.mark_unknown(key)
            else:
                self.ledger.settle(key, outcome.actual)
            result["budget_contract_breached"] = self.ledger.snapshot()["admission_blocked"]
            stop = continuation_stop(
                outcome.cancelled_after, outcome.effect_after, read_only=task.read_only,
            )
            if stop:
                return {**result, "status": stop}
            if outcome.actual is None:
                return {**result, "status": "usage_pending"}
            if result["budget_contract_breached"]:
                return {**result, "status": "budget_contract_breached"}
            if not task.read_only:
                return {**result, "status": "write_effect_not_confirmed"}
            if outcome.quality_pass:
                answer = "教学结果：VPN 手册的适用条件与建议已通过固定检查。"
                if task.read_only:
                    self.cache.put(task, answer)
                return {**result, "status": "succeeded", "answer": answer}
        return {**result, "status": "quality_not_met"}


def demo_models() -> list[Model]:
    caps = frozenset({"structured_output"})
    return [
        Model("cheap-other-region", "us", caps, 5),
        Model("cheap-missing-capability", "eu", frozenset(), 10),
        Model("small-eu", "eu", caps, 40),
        Model("strong-eu", "eu", caps, 100),
    ]


def demo() -> dict:
    scope = Scope("tenant-a", "employee-7", "authz-3")
    task = Task("run-upgrade", scope, "VPN E104 如何排查？")
    outcomes = {
        "small-eu": SimulatedOutcome(False, 30),
        "strong-eu": SimulatedOutcome(True, 80),
    }
    ledger, cache = Ledger(150), ResultCache()
    router = Router(demo_models(), ledger, cache)
    upgrade = router.run(task, outcomes)
    hit = router.run(replace(task, run_id="run-cached"), outcomes)
    isolated = cache.get(replace(task, scope=replace(scope, tenant="tenant-b")))

    limited = Ledger(120)
    budget_stop = Router(demo_models(), limited, ResultCache()).run(
        replace(task, run_id="run-limited"), outcomes,
    )
    pending = Ledger(100)
    timeout = Router(demo_models(), pending, ResultCache()).run(
        replace(task, run_id="run-timeout"),
        {"small-eu": SimulatedOutcome(False, None)},
    )
    before = pending.snapshot()
    pending.reconcile("run-timeout:attempt-1", 25)

    unknown = Router(demo_models(), Ledger(150), ResultCache()).run(
        replace(task, run_id="run-unknown-effect", read_only=False),
        {"small-eu": SimulatedOutcome(False, 30, effect_after="unknown")},
    )
    confirmed = Router(demo_models(), Ledger(150), ResultCache()).run(
        replace(task, run_id="run-confirmed-effect", read_only=False),
        {"small-eu": SimulatedOutcome(False, 30, effect_after="confirmed")},
    )
    overrun = Ledger(40)
    overrun.reserve("adapter-violation", 40)
    overrun.settle("adapter-violation", 55)
    return {
        "units": "integer teaching microcredits; no real prices or model calls",
        "upgrade": upgrade, "upgrade_ledger": ledger.snapshot(),
        "same_scope_cache": hit, "other_tenant_cache": isolated,
        "upgrade_budget_denied": budget_stop, "limited_ledger": limited.snapshot(),
        "timeout": timeout, "timeout_before_reconciliation": before,
        "timeout_after_reconciliation": pending.snapshot(),
        "unknown_effect_blocks_fallback": unknown,
        "confirmed_write_is_not_replayed": confirmed,
        "actual_usage_overrun_is_not_clamped": overrun.snapshot(),
    }


if __name__ == "__main__":
    print(json.dumps(demo(), ensure_ascii=False, indent=2))
