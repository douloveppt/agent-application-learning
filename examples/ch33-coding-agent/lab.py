#!/usr/bin/env python3
"""Trusted acceptance-harness lesson. Python 3.11+, synthetic files only."""
from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import hashlib
import inspect
import json
from pathlib import Path
import tempfile
from types import MappingProxyType
from typing import Any


BASE_REVISION = "base-7d3c1e2"
SUITE_ID = "ticket-access-external-v1"

FAULTY_SOURCE = '''"""Synthetic service with the known same-tenant authorization defect."""

def query_ticket(actor, ticket_id, tickets):
    ticket = tickets.get(ticket_id)
    if ticket is None or ticket["tenant"] != actor["tenant"]:
        return None
    return ticket
'''

FIXED_SOURCE = '''"""Synthetic service that derives scope from the trusted actor."""

def query_ticket(actor, ticket_id, tickets):
    ticket = tickets.get(ticket_id)
    if ticket is None or ticket["tenant"] != actor["tenant"]:
        return None
    if ticket["owner_id"] != actor["user_id"]:
        return None
    return ticket
'''

TRUSTED_TEST_FILE = '''# Protected teaching fixture; the external harness owns the real assertions.
CASES = ("own", "same_tenant_other", "cross_tenant", "missing")
'''

BASE_FILES = MappingProxyType({
    "project.toml": "name = 'synthetic-ticket-service'\n",
    "src/service.py": FAULTY_SOURCE,
    "tests/test_access.py": TRUSTED_TEST_FILE,
})


def canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: object) -> str:
    payload = value if isinstance(value, str) else canonical(value)
    return hashlib.sha256(payload.encode()).hexdigest()


@dataclass(frozen=True)
class Policy:
    expected_base: str = BASE_REVISION
    allowed_paths: tuple[str, ...] = ("src/service.py",)
    protected_paths: tuple[str, ...] = ("tests/test_access.py",)
    max_changed_files: int = 1
    suite_id: str = SUITE_ID

    @property
    def content_digest(self) -> str:
        return digest(asdict(self))


@dataclass(frozen=True)
class Environment:
    image: str = "python:3.11-teaching@sha256:synthetic"
    network: str = "disabled"
    harness_version: str = "1.0"
    ready: bool = True
    failure: str | None = None

    @property
    def content_digest(self) -> str:
        return digest(asdict(self))


@dataclass(frozen=True)
class Workspace:
    root: Path
    base_revision: str


class Rejected(ValueError):
    """The synthetic workspace cannot cross a trusted gate."""


def write_workspace(root: Path, scenario: str, base_revision: str = BASE_REVISION) -> Workspace:
    """Materialize one of four fixed teaching candidates; never execute its code."""
    changes = {
        "faulty": {},
        "fixed": {"src/service.py": FIXED_SOURCE},
        "test_tamper": {
            "src/service.py": FIXED_SOURCE,
            "tests/test_access.py": "CASES = ('own',)  # weakened by candidate\n",
        },
        "out_of_scope": {
            "src/service.py": FIXED_SOURCE,
            "project.toml": "name = 'synthetic-ticket-service'\ndependencies = ['unreviewed']\n",
        },
    }
    if scenario not in changes:
        raise Rejected("unknown fixed teaching scenario")
    for relative, content in {**BASE_FILES, **changes[scenario]}.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return Workspace(root.resolve(), base_revision)


def snapshot(root: Path) -> dict[str, str]:
    """Hash every regular file so unlisted additions also invalidate evidence."""
    result: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise Rejected("symlink found in synthetic source tree")
        if path.is_dir():
            continue
        if not path.is_file():
            raise Rejected("non-regular source-tree entry")
        relative = path.relative_to(root).as_posix()
        result[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def tree_digest(files: dict[str, str]) -> str:
    return digest(sorted(files.items()))


def changed_paths(files: dict[str, str]) -> list[str]:
    expected = {name: digest(content) for name, content in BASE_FILES.items()}
    return sorted(name for name in set(files) | set(expected)
                  if files.get(name) != expected.get(name))


def source_variant(workspace: Workspace) -> str:
    try:
        source = (workspace.root / "src/service.py").read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise Rejected("required source is missing or not readable UTF-8") from exc
    if source == FAULTY_SOURCE:
        return "faulty"
    if source == FIXED_SOURCE:
        return "fixed"
    raise Rejected("source is not a reviewed teaching template")


def query_ticket(variant: str, actor: dict[str, str], ticket_id: str,
                 tickets: dict[str, dict[str, str]]) -> dict[str, str] | None:
    """Independent model; underscored variants are test-only negative controls."""
    ticket = tickets.get(ticket_id)
    if variant == "_deny_all":
        return None
    if variant == "_fabricate_missing" and ticket is None:
        return {"ticket_id": ticket_id, "tenant": actor["tenant"],
                "owner_id": actor["user_id"]}
    if variant == "_owner_only":
        return ticket if ticket is not None and ticket["owner_id"] == actor["user_id"] else None
    if variant == "faulty":
        return ticket if ticket is not None and ticket["tenant"] == actor["tenant"] else None
    if variant not in {"fixed", "_fabricate_missing"}:
        raise Rejected("unknown source behavior")
    if ticket is None:
        return None
    if ticket["tenant"] != actor["tenant"] or ticket["owner_id"] != actor["user_id"]:
        return None
    return ticket


def run_external_suite(variant: str) -> dict[str, Any]:
    tickets = {
        "T-A1": {"ticket_id": "T-A1", "tenant": "tenant-a", "owner_id": "alice"},
        "T-A2": {"ticket_id": "T-A2", "tenant": "tenant-a", "owner_id": "bob"},
        # Same user ID in another tenant makes this case isolate the tenant check.
        "T-B1": {"ticket_id": "T-B1", "tenant": "tenant-b", "owner_id": "alice"},
    }
    actor = {"tenant": "tenant-a", "user_id": "alice"}
    observations = {
        "own_ticket_visible": query_ticket(variant, actor, "T-A1", tickets) is not None,
        "same_tenant_other_hidden": query_ticket(variant, actor, "T-A2", tickets) is None,
        "cross_tenant_hidden": query_ticket(variant, actor, "T-B1", tickets) is None,
        "missing_hidden": query_ticket(variant, actor, "T-404", tickets) is None,
    }
    return {"passed": all(observations.values()), "observations": observations,
            "exit_code": 0 if all(observations.values()) else 1}


def suite_digest() -> str:
    contract = {
        "suite_id": SUITE_ID,
        "cases": ["own_ticket_visible", "same_tenant_other_hidden",
                  "cross_tenant_hidden", "missing_hidden"],
        "implementation": digest(inspect.getsource(query_ticket) +
                                 inspect.getsource(run_external_suite)),
    }
    return digest(contract)


class AcceptanceHarness:
    def __init__(self, policy: Policy = Policy()):
        self.policy = policy

    @staticmethod
    def _check(name: str, passed: bool, target: str, detail: str) -> dict[str, Any]:
        return {"name": name, "status": "passed" if passed else "failed",
                "target": target, "detail": detail, "exit_code": 0 if passed else 1}

    def assess(self, workspace: Workspace, environment: Environment) -> dict[str, Any]:
        files = snapshot(workspace.root)
        changed = changed_paths(files)
        evidence = {
            "tree_digest": tree_digest(files),
            "base_revision": workspace.base_revision,
            "policy_digest": self.policy.content_digest,
            "environment_digest": environment.content_digest,
            "check_suite_digest": suite_digest(),
        }
        checks: list[dict[str, Any]] = []
        if not environment.ready:
            checks.append({"name": "environment", "status": "blocked", "target": environment.image,
                           "detail": environment.failure or "unavailable", "exit_code": 2})
            return self._report("blocked_environment", evidence, files, changed, checks, 2)

        base_ok = workspace.base_revision == self.policy.expected_base
        checks.append(self._check("base_revision", base_ok, workspace.base_revision,
                                  "expected revision" if base_ok else "unexpected revision"))
        allowed = set(changed) <= set(self.policy.allowed_paths)
        bounded = len(changed) <= self.policy.max_changed_files
        checks.append(self._check("change_scope", allowed and bounded, ",".join(changed) or "(none)",
                                  "within declared scope" if allowed and bounded else "scope violation"))
        protected_ok = all(files.get(path) == digest(BASE_FILES[path])
                           for path in self.policy.protected_paths)
        checks.append(self._check("protected_tests", protected_ok,
                                  ",".join(self.policy.protected_paths),
                                  "trusted fixture intact" if protected_ok else "trusted fixture changed"))
        try:
            variant = source_variant(workspace)
            template_ok = True
        except Rejected:
            variant, template_ok = "unknown", False
        checks.append(self._check("reviewed_template", template_ok, "src/service.py",
                                  variant if template_ok else "unknown source template"))

        if not all(check["status"] == "passed" for check in checks):
            return self._report("policy_rejected", evidence, files, changed, checks, 1)

        functional = run_external_suite(variant)
        checks.append({"name": "external_acceptance", "status": "passed" if functional["passed"] else "failed",
                       "target": self.policy.suite_id, "detail": functional["observations"],
                       "exit_code": functional["exit_code"]})
        baseline = not changed and evidence["tree_digest"] == tree_digest(
            {name: digest(content) for name, content in BASE_FILES.items()})
        if functional["passed"]:
            status, exit_code = "ready_for_review", 0
        elif baseline:
            status, exit_code = "known_baseline_failure_reproduced", 1
        else:
            status, exit_code = "candidate_failed", 1
        return self._report(status, evidence, files, changed, checks, exit_code)

    def _report(self, status: str, evidence: dict[str, str], files: dict[str, str],
                changed: list[str], checks: list[dict[str, Any]], exit_code: int) -> dict[str, Any]:
        body = {
            "status": status,
            "evidence": evidence,
            "changed_paths": changed,
            "source_artifacts": [{"path": path, "sha256": value} for path, value in files.items()],
            "checks": checks,
            "completion_boundary": "review_only; merge, deployment and user acceptance require separate authority",
        }
        return {**body, "report_artifact": {"path": "stdout", "sha256": digest(body),
                                             "exit_code": exit_code}}

    def verify_receipt(self, report: dict[str, Any], workspace: Workspace,
                       environment: Environment) -> dict[str, Any]:
        reasons: list[str] = []
        evidence = report.get("evidence")
        required = {"tree_digest", "base_revision", "policy_digest",
                    "environment_digest", "check_suite_digest"}
        if not isinstance(evidence, dict) or not required <= set(evidence):
            reasons.append("missing evidence fields")
        else:
            current = {
                "tree_digest": tree_digest(snapshot(workspace.root)),
                "base_revision": workspace.base_revision,
                "policy_digest": self.policy.content_digest,
                "environment_digest": environment.content_digest,
                "check_suite_digest": suite_digest(),
            }
            reasons.extend(f"{key} changed" for key in sorted(required) if evidence.get(key) != current[key])
        artifact = report.get("report_artifact", {})
        body = {key: value for key, value in report.items() if key != "report_artifact"}
        if artifact.get("sha256") != digest(body):
            reasons.append("report digest mismatch")
        if report.get("status") != "ready_for_review":
            reasons.append("report is not review-ready")
        return {"valid": not reasons, "reasons": reasons}


def demo() -> dict[str, Any]:
    harness = AcceptanceHarness()
    results: dict[str, Any] = {}
    with tempfile.TemporaryDirectory(prefix="ch33-coding-agent-") as directory:
        root = Path(directory)
        for scenario in ("faulty", "fixed", "test_tamper", "out_of_scope"):
            workspace = write_workspace(root / scenario, scenario)
            results[scenario] = harness.assess(workspace, Environment())
        blocked = write_workspace(root / "blocked", "fixed")
        results["environment_failure"] = harness.assess(
            blocked, Environment(ready=False, failure="synthetic toolchain unavailable"))
        fixed_workspace = Workspace(root / "fixed", BASE_REVISION)
        fixed_report = results["fixed"]
        (fixed_workspace.root / "src/service.py").write_text(FIXED_SOURCE + "\n", encoding="utf-8")
        results["stale_evidence"] = harness.verify_receipt(
            fixed_report, fixed_workspace, Environment())
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("demo",))
    args = parser.parse_args()
    if args.command == "demo":
        print(json.dumps(demo(), ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
