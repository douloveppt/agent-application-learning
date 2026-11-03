#!/usr/bin/env python3
"""Deterministic, in-memory release-control lesson; Python 3.11+ only."""
from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass, field
import hashlib
import json
import math
from typing import Literal


class Rejected(ValueError):
    """A release operation failed its admission rule."""


def canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


@dataclass(frozen=True)
class Release:
    release_id: str
    code: str
    model: str
    prompt: str
    tool_schema: str
    state_writer: str
    state_readers: tuple[str, ...]
    index: str
    policy: str

    def __post_init__(self) -> None:
        if not all(isinstance(value, str) and value for key, value in asdict(self).items()
                   if key != "state_readers"):
            raise Rejected("manifest fields must be nonempty strings")
        if (not isinstance(self.state_readers, tuple) or not self.state_readers
                or any(not isinstance(item, str) or not item for item in self.state_readers)
                or self.state_writer not in self.state_readers):
            raise Rejected("state_readers must be an immutable tuple including state_writer")

    @property
    def digest(self) -> str:
        return hashlib.sha256(canonical(asdict(self))).hexdigest()


@dataclass(frozen=True)
class Resources:
    """Versions currently available in the simulated environment."""
    state_format: str = "state-v1"
    indexes: tuple[str, ...] = ("index-v1", "index-v2")
    tool_schemas: tuple[str, ...] = ("ticket-v1", "ticket-v2")


def check_compatibility(release: Release, resources: Resources) -> None:
    failures = []
    if resources.state_format not in release.state_readers:
        failures.append("state reader incompatible")
    if release.index not in resources.indexes:
        failures.append("required index unavailable")
    if release.tool_schema not in resources.tool_schemas:
        failures.append("tool schema unavailable")
    if failures:
        raise Rejected("; ".join(failures) + "; recovery plan required")


@dataclass(frozen=True)
class OfflineEvidence:
    release_digest: str
    passed: bool
    suite: str = "vpn-release-suite-v1"


@dataclass(frozen=True)
class Observation:
    release_digest: str
    run_id: str
    success: bool = True
    latency_ms: int = 1_000
    cost_microusd: int = 20_000
    unauthorized: bool = False
    unknown_write: bool = False

    def __post_init__(self) -> None:
        if (not isinstance(self.run_id, str) or not self.run_id
                or type(self.success) is not bool or type(self.unauthorized) is not bool
                or type(self.unknown_write) is not bool
                or type(self.latency_ms) is not int or self.latency_ms < 0
                or type(self.cost_microusd) is not int or self.cost_microusd < 0):
            raise Rejected("observations require booleans and nonnegative integer metrics")


@dataclass(frozen=True)
class Gate:
    decision: Literal["hold", "stop", "promote"]
    reason: str
    samples: int


def evaluate(release: Release, observations: list[Observation]) -> Gate:
    """Teaching thresholds, not confidence intervals or production SLOs."""
    if any(row.release_digest != release.digest for row in observations):
        raise Rejected("observations belong to a different release")
    if len({row.run_id for row in observations}) != len(observations):
        raise Rejected("duplicate run_id cannot increase the observation denominator")
    count = len(observations)
    # Severe evidence stops admission even before the sample minimum is met.
    if any(row.unauthorized or row.unknown_write for row in observations):
        return Gate("stop", "unauthorized access or unknown write observed", count)
    if count < 40:
        return Gate("hold", "need at least 40 unique teaching run observations", count)
    success_rate = sum(row.success for row in observations) / count
    p95 = sorted(row.latency_ms for row in observations)[math.ceil(count * .95) - 1]
    mean_cost = sum(row.cost_microusd for row in observations) / count
    if success_rate < .95 or p95 > 2_500 or mean_cost > 40_000:
        return Gate("stop", "success, p95 latency, or mean cost threshold failed", count)
    return Gate("promote", "all teaching thresholds passed", count)


def bucket(tenant: str, task_id: str) -> int:
    if not isinstance(tenant, str) or not tenant or not isinstance(task_id, str) or not task_id:
        raise Rejected("tenant and task_id are required")
    # A canonical array prevents ambiguous concatenations such as ab+c / a+bc.
    digest = hashlib.sha256(canonical([tenant, task_id])).digest()
    return int.from_bytes(digest[:8], "big") % 10_000


@dataclass(frozen=True)
class Task:
    tenant: str
    task_id: str
    release_digest: str
    state_format: str
    operations: dict[str, Literal["unknown", "confirmed"]] = field(default_factory=dict)

    @property
    def write_dispatches(self) -> int:
        return len(self.operations)


class Controller:
    """Single-process simulation: generation checks model, but do not implement, DB CAS."""

    def __init__(self, initial: Release, resources: Resources = Resources()):
        check_compatibility(initial, resources)
        self.releases: dict[str, Release] = {initial.digest: initial}
        self.release_ids = {initial.release_id: initial.digest}
        self.stable = initial.digest
        self.candidate: str | None = None
        self.weight_bps = 0
        self.generation = 0
        self.resources = resources
        self.tasks: dict[tuple[str, str], Task] = {}
        self.events: list[dict[str, object]] = []

    def _expect(self, expected: int) -> None:
        if expected != self.generation:
            raise Rejected(f"stale generation: expected {expected}, current {self.generation}")

    def _record(self, action: str, **details: object) -> None:
        self.generation += 1
        self.events.append({"generation": self.generation, "action": action, **details})

    def stage(self, release: Release, evidence: OfflineEvidence, *, expected: int,
              weight_bps: int = 1_000) -> None:
        self._expect(expected)
        if (evidence.release_digest != release.digest or evidence.passed is not True
                or evidence.suite != "vpn-release-suite-v1"):
            raise Rejected("offline quality evidence rejected")
        if type(weight_bps) is not int or not 1 <= weight_bps <= 5_000:
            raise Rejected("teaching canary weight must be 1..5000 basis points")
        known = self.release_ids.get(release.release_id)
        if known is not None and known != release.digest:
            raise Rejected("release_id already refers to different immutable content")
        if self.candidate is not None or release.digest == self.stable:
            raise Rejected("finish current canary or choose a different candidate")
        check_compatibility(release, self.resources)
        self.releases[release.digest] = release
        self.release_ids[release.release_id] = release.digest
        self.candidate, self.weight_bps = release.digest, weight_bps
        self._record("canary_started", release_digest=release.digest, weight_bps=weight_bps)

    def admit(self, tenant: str, task_id: str) -> Task:
        key = (tenant, task_id)
        if key in self.tasks:
            return self.tasks[key]
        chosen = self.candidate if self.candidate and bucket(tenant, task_id) < self.weight_bps else self.stable
        release = self.releases[chosen]
        check_compatibility(release, self.resources)
        task = Task(tenant, task_id, chosen, release.state_writer)
        self.tasks[key] = task
        return task

    def resume(self, tenant: str, task_id: str) -> Task:
        task = self.tasks[(tenant, task_id)]
        release = self.releases[task.release_digest]
        check_compatibility(release, self.resources)
        if task.state_format not in release.state_readers:
            raise Rejected("pinned worker cannot read task state")
        return task

    def decide(self, observations: list[Observation], *, expected: int) -> Gate:
        self._expect(expected)
        if self.candidate is None:
            raise Rejected("no canary to evaluate")
        gate = evaluate(self.releases[self.candidate], observations)
        if gate.decision == "hold":
            return gate
        candidate = self.candidate
        if gate.decision == "promote":
            check_compatibility(self.releases[candidate], self.resources)
            self.stable = candidate
        self.candidate, self.weight_bps = None, 0
        self._record(gate.decision, release_digest=candidate, reason=gate.reason)
        return gate

    def rollback(self, target_digest: str, *, expected: int) -> None:
        self._expect(expected)
        if target_digest not in self.releases:
            raise Rejected("rollback target was not registered")
        check_compatibility(self.releases[target_digest], self.resources)
        self.stable, self.candidate, self.weight_bps = target_digest, None, 0
        self._record("rollback_new_admissions", release_digest=target_digest)

    def dispatch_write(self, tenant: str, task_id: str, operation_id: str, *, lose_receipt: bool) -> None:
        task = self.resume(tenant, task_id)
        if operation_id in task.operations:
            raise Rejected("operation already dispatched; reconcile authoritative receipt first")
        task.operations[operation_id] = "unknown" if lose_receipt else "confirmed"


def fixtures() -> tuple[Release, Release]:
    old = Release("vpn-r1", "image-sha256-r1", "teaching-model-snapshot-a", "prompt-v1",
                  "ticket-v1", "state-v1", ("state-v1",), "index-v1", "policy-v1")
    new = Release("vpn-r2", "image-sha256-r2", "teaching-model-snapshot-a", "prompt-v2",
                  "ticket-v2", "state-v1", ("state-v1",), "index-v2", "policy-v1")
    return old, new


def demo() -> dict[str, object]:
    old, new = fixtures()
    controller = Controller(old)
    rejected: dict[str, str] = {}
    try:
        controller.stage(new, OfflineEvidence(new.digest, False), expected=0)
    except Rejected as exc:
        rejected["offline_failure"] = str(exc)
    controller.stage(new, OfflineEvidence(new.digest, True), expected=0)
    canary_id = next(f"vpn-{number}" for number in range(1_000)
                     if bucket("tenant-a", f"vpn-{number}") < 1_000)
    pinned = controller.admit("tenant-a", canary_id)
    observations = [Observation(new.digest, f"observed-run-{number}") for number in range(40)]
    hold = controller.decide(observations[:5], expected=1)
    promote = controller.decide(observations, expected=1)
    controller.dispatch_write("tenant-a", canary_id, "ticket-op-1", lose_receipt=True)
    try:
        controller.rollback(old.digest, expected=1)
    except Rejected as exc:
        rejected["stale_controller"] = str(exc)
    controller.rollback(old.digest, expected=2)
    after = controller.admit("tenant-a", "after-rollback")
    resumed = controller.resume("tenant-a", canary_id)
    try:
        controller.dispatch_write("tenant-a", canary_id, "ticket-op-1", lose_receipt=True)
    except Rejected as exc:
        rejected["unknown_write_replay"] = str(exc)
    controller.resources = Resources(state_format="state-v2")
    try:
        controller.rollback(old.digest, expected=3)
    except Rejected as exc:
        rejected["incompatible_rollback"] = str(exc)
    stop = evaluate(new, [Observation(new.digest, "unsafe-run", unauthorized=True)])
    checks = {
        "offline_failure_did_not_activate": "offline_failure" in rejected,
        "new_tasks_use_old_release": after.release_digest == old.digest,
        "old_task_keeps_candidate_release": resumed.release_digest == new.digest,
        "unknown_write_dispatched_once": resumed.write_dispatches == 1,
        "incompatible_rollback_did_not_advance_generation": controller.generation == 3,
    }
    if not all(checks.values()):
        raise Rejected("demo invariant failed")
    return {
        "lab": "ch30-llmops-v1", "mode": "offline_in_memory_simulation",
        "manifests": [{"manifest": asdict(item), "sha256": item.digest} for item in (old, new)],
        "gates": [asdict(hold), asdict(promote), asdict(stop)],
        "rejected": rejected, "events": controller.events,
        "pinned_task": {"task_id": pinned.task_id, "bucket": bucket("tenant-a", pinned.task_id),
                        "release_digest": pinned.release_digest, "operations": pinned.operations,
                        "write_dispatches": pinned.write_dispatches},
        "assertions": checks,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["demo"])
    parser.parse_args()
    print(json.dumps(demo(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
