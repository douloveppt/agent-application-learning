#!/usr/bin/env python3
"""Offline host simulator for Chapter 15: reusable Agent Skills.

The lab deliberately separates a skill's instructional content from the trusted
host.  The host owns identity, tool availability, authorization, package locks,
and fixed handlers.  Skill files can guide a task, but cannot grant authority.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path, PurePosixPath
import shutil
import tempfile
from typing import Any


BASE_DIR = Path(__file__).resolve().parent
REGISTRY_PATH = BASE_DIR / "registry.json"
MAX_RESOURCE_BYTES = 256 * 1024
HANDLER_ID = "host-builtin:enterprise-it-triage:v1"
HANDLER_TRUST = {
    "kind": "host_builtin",
    "source": "lab.py:SkillHost._run_enterprise_it_triage",
    "meaning": "Installed by the host; it is not loaded from the skill package.",
}


HOST_TICKETS = (
    {
        "tenant_id": "acme",
        "user_id": "employee-17",
        "ticket_id": "IT-2048",
        "category": "vpn",
        "status": "waiting_for_employee",
        "summary": "VPN client times out after MFA",
    },
    {
        "tenant_id": "acme",
        "user_id": "employee-99",
        "ticket_id": "IT-9999",
        "category": "vpn",
        "status": "in_progress",
        "summary": "Another employee's VPN incident",
    },
    {
        "tenant_id": "other-tenant",
        "user_id": "employee-17",
        "ticket_id": "IT-7000",
        "category": "email-access",
        "status": "open",
        "summary": "A similarly named user in another tenant",
    },
)


class RegistryError(RuntimeError):
    """The host registry is invalid."""


class SkillIntegrityError(RuntimeError):
    """The package does not match its trusted lock."""


class ToolPolicyError(RuntimeError):
    """A tool call crossed an availability or authorization boundary."""


def digest_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def package_digest(resource_digests: dict[str, str]) -> str:
    canonical = json.dumps(
        resource_digests,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return digest_bytes(canonical)


def load_registry(path: Path = REGISTRY_PATH) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RegistryError("registry.json is unavailable or invalid JSON") from exc
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        raise RegistryError("unsupported registry schema")
    skills = data.get("skills")
    if not isinstance(skills, dict) or not skills:
        raise RegistryError("registry must contain at least one skill")
    for skill_id, entry in skills.items():
        if not isinstance(skill_id, str) or not isinstance(entry, dict):
            raise RegistryError("registry skill entries must be objects")
        required = {
            "name",
            "description",
            "version",
            "root",
            "entrypoint",
            "handler",
            "required_tools",
            "modes",
            "resources",
            "package_digest",
        }
        if not required <= entry.keys():
            raise RegistryError(f"skill {skill_id} is missing required registry fields")
        if entry["name"] != skill_id:
            raise RegistryError(f"skill {skill_id} has a mismatched name")
        if not isinstance(entry["resources"], dict) or not entry["resources"]:
            raise RegistryError(f"skill {skill_id} has no locked resources")
    return data


@dataclass(frozen=True)
class Principal:
    tenant_id: str
    user_id: str
    allowed_tools: frozenset[str]


@dataclass(frozen=True)
class TaskRequest:
    skill_candidates: tuple[str, ...]
    mode_candidates: tuple[str, ...]
    symptom: str | None
    proposed_tools: tuple[str, ...] = ()


@dataclass(frozen=True)
class VerifiedPackage:
    contents: dict[str, bytes]
    digests: dict[str, str]
    digest: str
    verified_bytes: int


class ToolRuntime:
    """A small trusted tool runtime with in-memory drafts only."""

    def __init__(self, principal: Principal, available_tools: frozenset[str]):
        self.principal = principal
        self.available_tools = available_tools
        self.executed_calls: list[str] = []
        self._drafts: dict[str, dict[str, Any]] = {}

    def call(self, name: str, arguments: dict[str, Any]) -> Any:
        if name not in self.available_tools:
            raise ToolPolicyError(f"tool unavailable: {name}")
        if name not in self.principal.allowed_tools:
            raise ToolPolicyError(f"tool not allowed for principal: {name}")
        handlers = {
            "tickets.list_own": self._list_own_tickets,
            "ticket_drafts.create": self._create_draft,
        }
        handler = handlers.get(name)
        if handler is None:
            raise ToolPolicyError(f"tool has no trusted handler: {name}")
        result = handler(arguments)
        self.executed_calls.append(name)
        return result

    def _list_own_tickets(self, arguments: dict[str, Any]) -> list[dict[str, str]]:
        if arguments:
            raise ToolPolicyError("tickets.list_own does not accept identity arguments")
        return [
            {
                "ticket_id": row["ticket_id"],
                "category": row["category"],
                "status": row["status"],
                "summary": row["summary"],
            }
            for row in HOST_TICKETS
            if row["tenant_id"] == self.principal.tenant_id
            and row["user_id"] == self.principal.user_id
        ]

    def _create_draft(self, arguments: dict[str, Any]) -> dict[str, Any]:
        if set(arguments) != {"title", "description"}:
            raise ToolPolicyError("ticket_drafts.create accepts title and description only")
        title = arguments["title"]
        description = arguments["description"]
        if not isinstance(title, str) or not title.strip():
            raise ToolPolicyError("draft title is required")
        if not isinstance(description, str) or not description.strip():
            raise ToolPolicyError("draft description is required")
        draft_key = "\0".join(
            (self.principal.tenant_id, self.principal.user_id, title, description)
        ).encode("utf-8")
        draft_id = f"draft-{digest_bytes(draft_key)[:12]}"
        draft = {
            "artifact_type": "ticket_draft",
            "draft_id": draft_id,
            "status": "draft_only",
            "title": title,
            "description": description,
        }
        self._drafts[draft_id] = draft
        return dict(draft)


class SkillHost:
    """Deterministic teaching host; it does not emulate model selection."""

    def __init__(
        self,
        *,
        principal: Principal,
        available_tools: frozenset[str],
        content_root: Path = BASE_DIR,
        registry_path: Path = REGISTRY_PATH,
    ):
        self.principal = principal
        self.available_tools = available_tools
        self.content_root = content_root.resolve()
        self.registry = load_registry(registry_path)

    def catalog(self) -> list[dict[str, str]]:
        """Return cheap discovery metadata without reading skill resources."""
        return [
            {"name": skill_id, "description": entry["description"]}
            for skill_id, entry in sorted(self.registry["skills"].items())
        ]

    def _base_result(self, request: TaskRequest) -> dict[str, Any]:
        return {
            "status": "error",
            "code": "unclassified",
            "catalog": self.catalog(),
            "admission": {
                "source": "explicit scenario input",
                "skill_candidates": list(request.skill_candidates),
                "mode_candidates": list(request.mode_candidates),
            },
            "context": {"loaded_files": [], "loaded_bytes": 0},
            "integrity": {
                "verified_files": [],
                "verified_bytes": 0,
                "package_digest": None,
            },
            "tool_calls": {"executed_count": 0, "names": []},
        }

    def _verify_package(self, entry: dict[str, Any]) -> VerifiedPackage:
        root_rel = PurePosixPath(entry["root"])
        if root_rel.is_absolute() or ".." in root_rel.parts:
            raise SkillIntegrityError("unsafe skill root")
        unresolved_root = self.content_root
        for part in root_rel.parts:
            unresolved_root /= part
            if unresolved_root.is_symlink():
                raise SkillIntegrityError(
                    f"skill root path contains a symlink: {part}"
                )
        skill_root = unresolved_root.resolve()
        if self.content_root not in skill_root.parents:
            raise SkillIntegrityError("skill root escapes content root")

        expected = entry["resources"]
        expected_paths = set(expected)
        actual_paths: set[str] = set()
        if not skill_root.is_dir():
            raise SkillIntegrityError("skill root is missing")
        for path in skill_root.rglob("*"):
            if path.is_symlink():
                raise SkillIntegrityError("skill package contains a symlink")
            if path.is_file():
                actual_paths.add(path.relative_to(skill_root).as_posix())
        if actual_paths != expected_paths:
            missing = sorted(expected_paths - actual_paths)
            extra = sorted(actual_paths - expected_paths)
            raise SkillIntegrityError(f"resource inventory mismatch; missing={missing}, extra={extra}")

        contents: dict[str, bytes] = {}
        actual_digests: dict[str, str] = {}
        total_bytes = 0
        for relative in sorted(expected):
            rel_path = PurePosixPath(relative)
            if rel_path.is_absolute() or ".." in rel_path.parts:
                raise SkillIntegrityError(f"unsafe resource path: {relative}")
            path = skill_root / Path(*rel_path.parts)
            content = path.read_bytes()
            if not content or len(content) > MAX_RESOURCE_BYTES:
                raise SkillIntegrityError(f"resource size rejected: {relative}")
            actual = digest_bytes(content)
            if actual != expected[relative]:
                raise SkillIntegrityError(f"resource digest mismatch: {relative}")
            contents[relative] = content
            actual_digests[relative] = actual
            total_bytes += len(content)

        actual_package_digest = package_digest(actual_digests)
        if actual_package_digest != entry["package_digest"]:
            raise SkillIntegrityError("package digest mismatch")
        return VerifiedPackage(
            contents=contents,
            digests=actual_digests,
            digest=actual_package_digest,
            verified_bytes=total_bytes,
        )

    def run(self, request: TaskRequest) -> dict[str, Any]:
        result = self._base_result(request)
        if len(request.skill_candidates) != 1:
            result.update(
                status="clarification_required",
                code="ambiguous_skill" if request.skill_candidates else "skill_not_selected",
            )
            return result
        skill_id = request.skill_candidates[0]
        entry = self.registry["skills"].get(skill_id)
        if entry is None:
            result.update(status="blocked", code="skill_not_registered")
            return result

        if len(request.mode_candidates) != 1:
            result.update(
                status="clarification_required",
                code="ambiguous_mode" if request.mode_candidates else "mode_not_selected",
            )
            return result
        mode = request.mode_candidates[0]
        mode_entry = entry["modes"].get(mode)
        if mode_entry is None:
            result.update(status="blocked", code="mode_not_registered")
            return result

        missing_fields = []
        for field in mode_entry["required_fields"]:
            value = getattr(request, field, None)
            if value is None or (isinstance(value, str) and not value.strip()):
                missing_fields.append(field)
        if missing_fields:
            result.update(status="needs_input", code="missing_required_input")
            result["missing_fields"] = missing_fields
            return result

        allowed_by_skill = set(entry["required_tools"])
        proposed_tools = set(request.proposed_tools)
        outside_skill_policy = sorted(proposed_tools - allowed_by_skill)
        if outside_skill_policy:
            result.update(status="blocked", code="tool_not_allowed")
            result["denied_tools"] = outside_skill_policy
            return result

        unavailable = sorted(allowed_by_skill - self.available_tools)
        if unavailable:
            result.update(status="blocked", code="tool_unavailable")
            result["unavailable_tools"] = unavailable
            return result
        unauthorized = sorted(allowed_by_skill - self.principal.allowed_tools)
        if unauthorized:
            result.update(status="blocked", code="principal_not_authorized")
            result["denied_tools"] = unauthorized
            return result

        handler = entry["handler"]
        if not isinstance(handler, dict) or handler.get("id") != HANDLER_ID:
            result.update(status="error", code="untrusted_handler")
            return result

        try:
            verified = self._verify_package(entry)
        except (OSError, SkillIntegrityError) as exc:
            result.update(status="error", code="skill_integrity_failed")
            result["reason"] = str(exc)
            return result

        selected_files = [entry["entrypoint"], mode_entry["reference"]]
        if len(set(selected_files)) != len(selected_files):
            result.update(status="error", code="invalid_context_route")
            return result
        try:
            loaded_bytes = sum(len(verified.contents[path]) for path in selected_files)
        except KeyError:
            result.update(status="error", code="unlocked_context_resource")
            return result
        result["context"] = {
            "loaded_files": selected_files,
            "loaded_bytes": loaded_bytes,
        }
        result["integrity"] = {
            "verified_files": sorted(verified.contents),
            "verified_bytes": verified.verified_bytes,
            "package_digest": verified.digest,
        }

        runtime = ToolRuntime(self.principal, self.available_tools)
        try:
            artifact, evidence = self._run_enterprise_it_triage(
                mode=mode,
                symptom=request.symptom or "",
                runtime=runtime,
            )
        except ToolPolicyError as exc:
            result.update(status="blocked", code="runtime_policy_denied")
            result["reason"] = str(exc)
            result["tool_calls"] = {
                "executed_count": len(runtime.executed_calls),
                "names": runtime.executed_calls,
            }
            return result

        provenance = {
            "skill": skill_id,
            "version": entry["version"],
            "package_digest": verified.digest,
            "handler": {"id": HANDLER_ID, "trust": HANDLER_TRUST},
        }
        artifact["provenance"] = provenance
        result.update(status="ok", code="draft_ready")
        result["artifact"] = artifact
        result["evidence"] = evidence
        result["tool_calls"] = {
            "executed_count": len(runtime.executed_calls),
            "names": runtime.executed_calls,
        }
        return result

    def _run_enterprise_it_triage(
        self,
        *,
        mode: str,
        symptom: str,
        runtime: ToolRuntime,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        tickets = runtime.call("tickets.list_own", {})
        related = [row for row in tickets if row["category"] == mode]
        ticket_ids = [row["ticket_id"] for row in related]
        label = "VPN" if mode == "vpn" else "邮箱访问"
        evidence_text = "、".join(ticket_ids) if ticket_ids else "未找到同类本人工单"
        description = (
            f"现象：{symptom.strip()}\n"
            f"只读核查：{evidence_text}\n"
            "下一步：请服务台复核诊断信息；本产物仍是草稿，未提交工单。"
        )
        artifact = runtime.call(
            "ticket_drafts.create",
            {"title": f"{label}故障诊断草稿", "description": description},
        )
        evidence = {
            "scope": "current_principal_only",
            "matching_ticket_ids": ticket_ids,
            "matching_ticket_count": len(ticket_ids),
        }
        return artifact, evidence


def default_principal() -> Principal:
    return Principal(
        tenant_id="acme",
        user_id="employee-17",
        allowed_tools=frozenset({"tickets.list_own", "ticket_drafts.create"}),
    )


def normal_request(**changes: Any) -> TaskRequest:
    values: dict[str, Any] = {
        "skill_candidates": ("enterprise-it-triage",),
        "mode_candidates": ("vpn",),
        "symptom": "MFA 成功后 VPN 客户端持续超时",
        "proposed_tools": ("tickets.list_own", "ticket_drafts.create"),
    }
    values.update(changes)
    return TaskRequest(**values)


def run_scenario(name: str) -> dict[str, Any]:
    tools = frozenset({"tickets.list_own", "ticket_drafts.create"})
    if name == "normal":
        result = SkillHost(principal=default_principal(), available_tools=tools).run(
            normal_request()
        )
    elif name == "missing_input":
        result = SkillHost(principal=default_principal(), available_tools=tools).run(
            normal_request(symptom="")
        )
    elif name == "tool_unavailable":
        result = SkillHost(
            principal=default_principal(),
            available_tools=frozenset({"tickets.list_own"}),
        ).run(normal_request())
    elif name == "ambiguous":
        result = SkillHost(principal=default_principal(), available_tools=tools).run(
            normal_request(mode_candidates=("vpn", "email-access"))
        )
    elif name == "tampered_resource":
        with tempfile.TemporaryDirectory(prefix="ch15-skill-") as temp_dir:
            copied_root = Path(temp_dir)
            shutil.copytree(BASE_DIR / "skills", copied_root / "skills")
            reference = (
                copied_root
                / "skills"
                / "enterprise-it-triage"
                / "references"
                / "vpn.md"
            )
            reference.write_text(
                reference.read_text(encoding="utf-8") + "\nUntrusted change.\n",
                encoding="utf-8",
            )
            result = SkillHost(
                principal=default_principal(),
                available_tools=tools,
                content_root=copied_root,
            ).run(normal_request())
    elif name == "permission_boundary":
        result = SkillHost(principal=default_principal(), available_tools=tools).run(
            normal_request(
                proposed_tools=(
                    "tickets.list_own",
                    "ticket_drafts.create",
                    "ticket.submit",
                )
            )
        )
    else:
        raise ValueError(f"unknown scenario: {name}")
    return {"scenario": name, **result}


SCENARIOS = (
    "normal",
    "missing_input",
    "tool_unavailable",
    "ambiguous",
    "tampered_resource",
    "permission_boundary",
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("catalog", help="show name/description discovery metadata")
    scenarios = subparsers.add_parser("scenarios", help="run an offline scenario")
    scenarios.add_argument("name", choices=("all", *SCENARIOS))
    args = parser.parse_args()

    if args.command == "catalog":
        host = SkillHost(
            principal=default_principal(),
            available_tools=frozenset({"tickets.list_own", "ticket_drafts.create"}),
        )
        print(json.dumps(host.catalog(), ensure_ascii=False, sort_keys=True))
        return 0
    names = SCENARIOS if args.name == "all" else (args.name,)
    for name in names:
        print(json.dumps(run_scenario(name), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
