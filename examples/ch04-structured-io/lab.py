"""Offline contract gate: model output becomes a candidate, never a real ticket."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import math
from typing import Annotated, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator


MAX_PAYLOAD_BYTES = 16 * 1024
MAX_JSON_DEPTH = 16

FieldName = Literal["title", "category", "affected_users"]
Category = Literal["vpn", "email", "device", "other"]
NonblankTitle = Annotated[str, Field(min_length=1, max_length=80)]
AffectedUsers = Annotated[int, Field(ge=1, le=100_000)]


class ContractModel(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")


class Evidence(ContractModel):
    source_id: Annotated[str, Field(min_length=1, max_length=64)]
    quote: Annotated[str, Field(min_length=1, max_length=1_000)]

    @field_validator("source_id", "quote")
    @classmethod
    def reject_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("blank text is not allowed")
        return value


class TicketCandidate(ContractModel):
    schema_version: Literal["1.0"]
    status: Literal["ready", "needs_clarification"]
    title: NonblankTitle | None
    category: Category | None
    affected_users: AffectedUsers | None
    evidence: Annotated[list[Evidence], Field(max_length=8)]
    missing_fields: Annotated[list[FieldName], Field(max_length=3)]

    @field_validator("title")
    @classmethod
    def reject_blank_title(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("blank title is not allowed")
        return value


@dataclass(frozen=True)
class ProviderResult:
    """A provider adapter normalizes its terminal response into this envelope."""

    status: Literal["completed", "incomplete", "refused", "cancelled", "error"]
    payload: str | None = None


@dataclass(frozen=True)
class EvidenceSource:
    tenant_id: str
    text: str


@dataclass(frozen=True)
class SourceContext:
    """Constructed by the application after authenticating and scoping sources."""

    tenant_id: str
    actor_id: str
    sources: Mapping[str, EvidenceSource]


class GateError(ValueError):
    """Stable public error details; never interpolate model field values."""

    def __init__(self, code: str, path: str = "$", *, retryable: bool = False):
        self.code = code
        self.path = path
        # This is eligibility to regenerate a candidate, not permission to retry writes.
        self.retryable = retryable
        super().__init__(f"{code} at {path}")


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise GateError("duplicate_key", retryable=True)
        result[key] = value
    return result


def _reject_constant(_value: str) -> None:
    raise GateError("non_finite_number", retryable=True)


def _finite_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise GateError("non_finite_number", retryable=True)
    return number


def _check_json_resources(value: object) -> None:
    # Iterative traversal avoids introducing another recursive walk after decoding.
    pending = [(value, 0)]
    while pending:
        item, parent_depth = pending.pop()
        if isinstance(item, str):
            try:
                item.encode("utf-8")
            except UnicodeEncodeError:
                raise GateError("invalid_unicode", retryable=True) from None
        elif isinstance(item, (dict, list)):
            depth = parent_depth + 1
            if depth > MAX_JSON_DEPTH:
                raise GateError("json_too_deep", retryable=True)
            children = [*item.keys(), *item.values()] if isinstance(item, dict) else item
            pending.extend((child, depth) for child in children)


def parse_payload(payload: str) -> object:
    try:
        size = len(payload.encode("utf-8"))
    except UnicodeEncodeError:
        raise GateError("invalid_unicode", retryable=True) from None
    if size > MAX_PAYLOAD_BYTES:
        raise GateError("payload_too_large")
    try:
        value = json.loads(
            payload,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
            parse_float=_finite_float,
        )
    except RecursionError:
        raise GateError("json_too_deep", retryable=True) from None
    except GateError:
        raise
    except ValueError:
        raise GateError("invalid_json", retryable=True) from None
    _check_json_resources(value)
    return value


def _safe_path(location: tuple[object, ...]) -> str:
    allowed = {
        "schema_version", "status", "title", "category", "affected_users",
        "evidence", "missing_fields", "source_id", "quote",
    }
    path = "$"
    for part in location:
        if isinstance(part, int):
            path += f"[{part}]"
        else:
            path += "." + (part if part in allowed else "<unknown>")
    return path


def _check_consistency(candidate: TicketCandidate) -> None:
    missing = {
        name for name in ("title", "category", "affected_users")
        if getattr(candidate, name) is None
    }
    declared = candidate.missing_fields
    if len(declared) != len(set(declared)) or set(declared) != missing:
        raise GateError("missing_fields_mismatch", "$.missing_fields", retryable=True)
    if candidate.status == "ready":
        if missing:
            raise GateError("ready_has_missing_fields", "$.status", retryable=True)
        if not candidate.evidence:
            raise GateError("ready_requires_evidence", "$.evidence", retryable=True)
    elif not missing:
        raise GateError("clarification_requires_missing_fields", "$.status", retryable=True)


def _check_evidence(candidate: TicketCandidate, context: SourceContext) -> None:
    for index, evidence in enumerate(candidate.evidence):
        path = f"$.evidence[{index}]"
        source = context.sources.get(evidence.source_id)
        if source is None or source.tenant_id != context.tenant_id:
            raise GateError("evidence_not_allowed", path)
        if evidence.quote not in source.text:
            raise GateError("evidence_quote_mismatch", path + ".quote", retryable=True)


def validate_candidate(result: ProviderResult, context: SourceContext) -> TicketCandidate:
    """Validate a completed result; the returned object is still unapproved data."""

    terminal_errors = {
        "incomplete": ("response_incomplete", True),
        "refused": ("model_refused", False),
        "cancelled": ("response_cancelled", False),
        "error": ("provider_error", False),
    }
    if result.status != "completed":
        code, retryable = terminal_errors.get(result.status, ("response_status_invalid", False))
        raise GateError(code, retryable=retryable)
    if not isinstance(result.payload, str):
        raise GateError("missing_payload")
    value = parse_payload(result.payload)
    try:
        candidate = TicketCandidate.model_validate(value)
    except ValidationError as exc:
        first = exc.errors(include_input=False, include_url=False)[0]
        raise GateError("schema_invalid", _safe_path(first["loc"]), retryable=True) from None
    _check_consistency(candidate)
    _check_evidence(candidate, context)
    return candidate


def demo_context() -> SourceContext:
    return SourceContext(
        tenant_id="demo-company",
        actor_id="demo-employee",
        sources={
            "user-message-1": EvidenceSource(
                tenant_id="demo-company",
                text="VPN 连不上，影响 3 位同事，请帮我们准备一个 IT 工单。",
            ),
        },
    )


def demo_payload() -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "status": "ready",
        "title": "VPN 无法连接",
        "category": "vpn",
        "affected_users": 3,
        "evidence": [{"source_id": "user-message-1", "quote": "VPN 连不上，影响 3 位同事"}],
        "missing_fields": [],
    }


def demo_clarification_context() -> SourceContext:
    return SourceContext(
        tenant_id="demo-company",
        actor_id="demo-employee",
        sources={
            "user-message-1": EvidenceSource(
                tenant_id="demo-company",
                text="VPN 连不上，请帮我准备一个 IT 工单。",
            ),
        },
    )


def demo_clarification_payload() -> dict[str, object]:
    return {
        **demo_payload(),
        "status": "needs_clarification",
        "affected_users": None,
        "missing_fields": ["affected_users"],
        "evidence": [{"source_id": "user-message-1", "quote": "VPN 连不上"}],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--schema", action="store_true", help="print the local JSON Schema")
    args = parser.parse_args()
    if args.schema:
        print(json.dumps(TicketCandidate.model_json_schema(), ensure_ascii=False, indent=2))
        return
    ready = demo_payload()
    clarification = demo_clarification_payload()
    invalid = {**ready, "tenant_id": "model-chosen-tenant"}
    cases = (
        ("ready", ready, demo_context()),
        ("needs_clarification", clarification, demo_clarification_context()),
        ("extra_field", invalid, demo_context()),
    )
    for name, payload, context in cases:
        result = ProviderResult("completed", json.dumps(payload, ensure_ascii=False))
        try:
            candidate = validate_candidate(result, context)
        except GateError as exc:
            output = {"case": name, "error": {"code": exc.code, "path": exc.path,
                                             "retryable": exc.retryable}}
        else:
            # Display only these synthetic demo values; this is not a production logger.
            output = {"case": name, "candidate": candidate.model_dump()}
            if candidate.status == "needs_clarification":
                output.update(next_step="补问 missing_fields；本实验不创建工单",
                              fields_to_clarify=candidate.missing_fields)
            else:
                output["next_step"] = "展示草稿，等待业务校验与授权；本实验不创建工单"
        print(json.dumps(output, ensure_ascii=False))


if __name__ == "__main__":
    main()
