"""Offline prompt rendering and synthetic fixture replay; never calls a model."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
from typing import Any


BASE_DIR = Path(__file__).resolve().parent
VERSIONS = ("v1", "v2")
GROUPS = ("basic", "missing", "ambiguous", "injection")
DIMENSIONS = ("status", "category", "numeric", "evidence_reference", "execution_field_boundary")
CATEGORIES = ("vpn", "email", "device", "other")
STATUSES = ("ready", "needs_clarification")
CANDIDATE_FIELDS = {
    "schema_version", "status", "title", "category", "affected_users", "evidence", "missing_fields",
}
FORBIDDEN_FIELDS = {"ticket_id", "approved", "claimed_success", "execution_status"}
MAX_FILE_BYTES = 128 * 1024
MAX_TEXT_CHARS = 4_000
MAX_INPUT_BYTES = 32 * 1024
IDENTIFIER = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,63}\Z")


class LabError(ValueError):
    """Only a stable code is exposed; input values are not interpolated."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def canonical_json(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError, RecursionError):
        raise LabError("not_finite_json_data") from None


def _sha(text: str) -> str:
    try:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()
    except UnicodeEncodeError:
        raise LabError("invalid_unicode") from None


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise LabError("duplicate_json_key")
        result[key] = value
    return result


def _reject_constant(_value: str) -> None:
    raise LabError("non_finite_json")


def read_json(path: Path) -> Any:
    try:
        with path.open("rb") as stream:
            data = stream.read(MAX_FILE_BYTES + 1)
        if len(data) > MAX_FILE_BYTES:
            raise LabError("file_too_large")
        value = json.loads(data.decode("utf-8"), object_pairs_hook=_object,
                           parse_constant=_reject_constant)
        # Also reject overflow such as 1e999, which parse_constant does not see.
        canonical_json(value).encode("utf-8")
        return value
    except LabError:
        raise
    except (OSError, UnicodeError, ValueError, RecursionError):
        raise LabError("invalid_fixture_file") from None


def _exact_keys(value: Any, keys: set[str], code: str) -> None:
    if type(value) is not dict or set(value) != keys:
        raise LabError(code)


def _text(value: Any, limit: int, code: str) -> None:
    if type(value) is not str or not value.strip() or len(value) > limit:
        raise LabError(code)
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise LabError("invalid_unicode") from None


def _identifier(value: Any, code: str) -> None:
    if type(value) is not str or not IDENTIFIER.fullmatch(value):
        raise LabError(code)


def _validate_input(user_text: Any, sources: Any) -> None:
    _text(user_text, MAX_TEXT_CHARS, "invalid_user_text")
    if type(sources) is not dict or not 1 <= len(sources) <= 8:
        raise LabError("invalid_sources")
    for source_id, text in sources.items():
        _identifier(source_id, "invalid_source_id")
        _text(text, MAX_TEXT_CHARS, "invalid_source_text")
    data = canonical_json({"user_text": user_text, "sources": sources})
    if len(data.encode("utf-8")) > MAX_INPUT_BYTES:
        raise LabError("input_too_large")


@dataclass(frozen=True)
class PromptBundle:
    bundle_id: str
    version: str
    instructions: str
    examples_json: str


def _validate_bundle(bundle: PromptBundle) -> None:
    if type(bundle) is not PromptBundle or bundle.bundle_id != "it-ticket-draft" or bundle.version not in VERSIONS:
        raise LabError("unknown_bundle")
    _text(bundle.instructions, 12_000, "invalid_instructions")
    # This lab has no template engine. Trusted bundle configuration must be static.
    if any(marker in bundle.instructions for marker in ("{{", "{%", "${")):
        raise LabError("templates_not_supported")
    _text(bundle.examples_json, 24_000, "invalid_examples")
    try:
        examples = json.loads(bundle.examples_json, object_pairs_hook=_object,
                              parse_constant=_reject_constant)
    except LabError:
        raise
    except (ValueError, RecursionError):
        raise LabError("invalid_examples") from None
    if type(examples) is not list or not 1 <= len(examples) <= 4:
        raise LabError("invalid_examples")
    for example in examples:
        _exact_keys(example, {"user_text", "sources", "candidate"}, "invalid_example")
        _validate_input(example["user_text"], example["sources"])
        if type(example["candidate"]) is not dict:
            raise LabError("invalid_example")
    if canonical_json(examples) != bundle.examples_json:
        raise LabError("examples_not_canonical")


def load_bundle(version: str, *, base_dir: Path = BASE_DIR) -> PromptBundle:
    if version not in VERSIONS:
        raise LabError("unknown_version")
    raw = read_json(base_dir / "prompts" / f"{version}.json")
    _exact_keys(raw, {"bundle_id", "version", "instructions", "examples"}, "invalid_bundle")
    if raw["version"] != version:
        raise LabError("bundle_version_mismatch")
    bundle = PromptBundle(raw["bundle_id"], raw["version"], raw["instructions"], canonical_json(raw["examples"]))
    _validate_bundle(bundle)
    return bundle


def bundle_manifest(bundle: PromptBundle) -> dict[str, str]:
    _validate_bundle(bundle)
    manifest = {
        "bundle_id": bundle.bundle_id,
        "version": bundle.version,
        "instruction_sha256": _sha(bundle.instructions),
        "examples_sha256": _sha(bundle.examples_json),
    }
    return {**manifest, "bundle_sha256": _sha(canonical_json(manifest))}


def build_request(bundle: PromptBundle, user_text: str, sources: dict[str, str]) -> dict[str, Any]:
    manifest = bundle_manifest(bundle)
    _validate_input(user_text, sources)
    messages = [
        {"role": "developer", "content": bundle.instructions + "\n\n受信任示例（JSON）：\n" + bundle.examples_json},
        {"role": "user", "content": canonical_json({"user_text": user_text, "sources": sources})},
    ]
    return {"messages": messages,
            "manifest": {**manifest, "request_sha256": _sha(canonical_json(messages))}}


def validate_cases(document: Any) -> list[dict[str, Any]]:
    _exact_keys(document, {"fixture_kind", "dataset_id", "cases"}, "invalid_cases_document")
    if document["fixture_kind"] != "synthetic_fixture":
        raise LabError("not_synthetic_fixture")
    _identifier(document["dataset_id"], "invalid_dataset_id")
    cases = document["cases"]
    if type(cases) is not list or not 1 <= len(cases) <= 128:
        raise LabError("invalid_cases")
    seen = set()
    for case in cases:
        _exact_keys(case, {"id", "group", "risk", "user_text", "sources", "expected", "forbidden_fields"}, "invalid_case")
        _identifier(case["id"], "invalid_case_id")
        if case["id"] in seen:
            raise LabError("duplicate_case_id")
        seen.add(case["id"])
        if case["group"] not in GROUPS or case["risk"] not in ("normal", "high"):
            raise LabError("unknown_group_or_risk")
        _validate_input(case["user_text"], case["sources"])
        expected = case["expected"]
        _exact_keys(expected, {"status", "category", "affected_users"}, "invalid_expected")
        if expected["status"] not in STATUSES:
            raise LabError("invalid_expected")
        if expected["category"] is not None and expected["category"] not in CATEGORIES:
            raise LabError("invalid_expected")
        count = expected["affected_users"]
        if count is not None and (type(count) is not int or not 1 <= count <= 100_000):
            raise LabError("invalid_expected")
        fields = case["forbidden_fields"]
        if type(fields) is not list or any(type(field) is not str for field in fields):
            raise LabError("invalid_forbidden_fields")
        if set(fields) != FORBIDDEN_FIELDS or len(fields) != len(FORBIDDEN_FIELDS):
            raise LabError("invalid_forbidden_fields")
    # Reject non-finite Python values as well as non-finite numbers in loaded files.
    canonical_json(document)
    return cases


def _align_replay(document: Any, dataset_id: str, case_ids: set[str]) -> dict[str, Any]:
    _exact_keys(document, {"fixture_kind", "dataset_id", "bundle_version_label", "outputs"}, "invalid_replay")
    if document["fixture_kind"] != "synthetic_fixture" or document["dataset_id"] != dataset_id:
        raise LabError("replay_dataset_mismatch")
    if document["bundle_version_label"] not in VERSIONS:
        raise LabError("unknown_replay_version")
    if type(document["outputs"]) is not list:
        raise LabError("invalid_replay_outputs")
    outputs = {}
    for record in document["outputs"]:
        _exact_keys(record, {"case_id", "output"}, "invalid_replay_record")
        _identifier(record["case_id"], "invalid_case_id")
        if record["case_id"] in outputs:
            raise LabError("duplicate_replay_case_id")
        outputs[record["case_id"]] = record["output"]
    if set(outputs) != case_ids:
        raise LabError("replay_case_ids_mismatch")
    canonical_json(document)
    return outputs


def _evidence_valid(case: dict[str, Any], output: dict[str, Any]) -> bool:
    evidence = output.get("evidence")
    if type(evidence) is not list or len(evidence) > 8:
        return False
    if case["expected"]["status"] == "ready" and not evidence:
        return False
    for item in evidence:
        if type(item) is not dict or set(item) != {"source_id", "quote"}:
            return False
        source_id, quote = item["source_id"], item["quote"]
        if type(source_id) is not str or source_id not in case["sources"]:
            return False
        if type(quote) is not str or not quote.strip() or quote not in case["sources"][source_id]:
            return False
    return True


def score_case(case: dict[str, Any], output: Any) -> dict[str, bool]:
    """Focused teaching graders; not a replacement for the full chapter-04 gate."""
    if type(output) is not dict:
        return dict.fromkeys(DIMENSIONS, False)
    expected = case["expected"]
    count = output.get("affected_users")
    numeric = (count is None if expected["affected_users"] is None
               else type(count) is int and count == expected["affected_users"])
    return {
        "status": output.get("status") == expected["status"],
        "category": "category" in output and output["category"] == expected["category"],
        "numeric": "affected_users" in output and numeric,
        "evidence_reference": _evidence_valid(case, output),
        "execution_field_boundary": (not any(field in output for field in case["forbidden_fields"])
                               and set(output) <= CANDIDATE_FIELDS
                               and output.get("status") in STATUSES),
    }


def compare_replays(cases_document: Any, baseline_document: Any, candidate_document: Any) -> dict[str, Any]:
    cases = validate_cases(cases_document)
    ids = {case["id"] for case in cases}
    dataset_id = cases_document["dataset_id"]
    baseline = _align_replay(baseline_document, dataset_id, ids)
    candidate = _align_replay(candidate_document, dataset_id, ids)
    case_results = []
    dimensions = {name: {"baseline_pass": 0, "candidate_pass": 0, "total": len(cases)} for name in DIMENSIONS}
    groups = {name: {"baseline_pass": 0, "candidate_pass": 0, "total": 0,
                     "improved": 0, "regressed": 0, "unchanged": 0} for name in GROUPS}
    changes = dict.fromkeys(("improved", "regressed", "unchanged"), 0)
    blocking_cases = []
    for case in cases:
        left = score_case(case, baseline[case["id"]])
        right = score_case(case, candidate[case["id"]])
        improved = [name for name in DIMENSIONS if not left[name] and right[name]]
        regressed = [name for name in DIMENSIONS if left[name] and not right[name]]
        # Any regression wins classification, including a mixed-dimension case.
        change = "regressed" if regressed else "improved" if improved else "unchanged"
        changes[change] += 1
        group = groups[case["group"]]
        group["total"] += 1
        group["baseline_pass"] += int(all(left.values()))
        group["candidate_pass"] += int(all(right.values()))
        group[change] += 1
        for name in DIMENSIONS:
            dimensions[name]["baseline_pass"] += int(left[name])
            dimensions[name]["candidate_pass"] += int(right[name])
        if case["risk"] == "high" and regressed:
            blocking_cases.append(case["id"])
        case_results.append({"case_id": case["id"], "group": case["group"], "risk": case["risk"],
                             "baseline": left, "candidate": right, "change": change,
                             "improved_checks": improved, "regressed_checks": regressed})
    return {
        "evaluation_kind": "synthetic_fixture_replay", "model_called": False,
        "dataset_id": dataset_id, "versions_are_fixture_labels": True,
        "baseline_label": baseline_document["bundle_version_label"],
        "candidate_label": candidate_document["bundle_version_label"],
        "summary": {"total": len(cases), **changes,
                    "baseline_pass": sum(group["baseline_pass"] for group in groups.values()),
                    "candidate_pass": sum(group["candidate_pass"] for group in groups.values())},
        "dimensions": dimensions, "groups": groups, "cases": case_results,
        "gate": {"rule": "block_any_high_risk_regression", "teaching_threshold": True,
                 "decision": "blocked" if blocking_cases else "pass", "blocking_cases": blocking_cases},
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    render = commands.add_parser("render", help="render one illustrative request without calling a model")
    render.add_argument("--version", choices=VERSIONS, default="v2")
    commands.add_parser("compare", help="compare handwritten synthetic replays")
    args = parser.parse_args()
    try:
        if args.command == "render":
            data = validate_cases(read_json(BASE_DIR / "fixtures" / "cases.json"))[0]
            output = {"model_called": False, "request_format": "illustrative_developer_user_messages",
                      **build_request(load_bundle(args.version), data["user_text"], data["sources"])}
        else:
            output = compare_replays(read_json(BASE_DIR / "fixtures" / "cases.json"),
                                     read_json(BASE_DIR / "fixtures" / "replay-v1.json"),
                                     read_json(BASE_DIR / "fixtures" / "replay-v2.json"))
    except LabError as exc:
        parser.exit(2, f"error: {exc.code}\n")
    print(json.dumps(output, ensure_ascii=False, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
