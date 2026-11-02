"""Deterministic routing and read-only workflow teaching lab; Python 3.11+."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass
import hashlib
import json
import re
from typing import Any


ROUTES = frozenset({"knowledge", "ticket_status", "combined", "clarify"})
RUNNABLE_ROUTES = ROUTES - {"clarify"}
READ_CAPABILITIES = frozenset({"search_knowledge", "get_ticket"})
CAPABILITIES = READ_CAPABILITIES | {"compose_answer"}
TICKET_STATUSES = frozenset({"open", "in_progress", "resolved", "closed"})
MAX_NODES = 8
MAX_TOTAL_ATTEMPTS = 12
ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,39}$")


class CandidateError(ValueError):
    """A model-proposed route is structurally invalid."""


class PlanError(ValueError):
    """A proposed plan is unsafe or internally inconsistent."""


class RetryableReadError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class AdmissionError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Scope:
    """Server-injected identity scope; the model cannot supply these fields."""

    tenant_id: str = "demo-company"
    user_id: str = "employee-a"


@dataclass(frozen=True)
class TaskRequest:
    """Structured task input, including user-confirmed (not attested) device facts."""

    issue: str = "VPN E104"
    confirmed_os: str | None = "Windows"
    confirmed_client_version: str | None = "5.2"
    ticket_id: str | None = "IT-100"

    def __post_init__(self) -> None:
        if type(self.issue) is not str or not self.issue.strip():
            raise ValueError("issue_must_be_nonempty_text")
        for value in (self.confirmed_os, self.confirmed_client_version, self.ticket_id):
            if value is not None and (type(value) is not str or not value.strip()):
                raise ValueError("optional_request_fields_must_be_nonempty_text")


@dataclass(frozen=True)
class RouteDecision:
    route: str
    reason: str
    confidence: float
    missing_fields: tuple[str, ...] = ()

    @property
    def runnable(self) -> bool:
        return self.route in RUNNABLE_ROUTES


def route_candidate(route: str, confidence: float = 0.92,
                    rationale: str = "按任务目标选择最小可用流程") -> dict[str, Any]:
    return {"route": route, "confidence": confidence, "rationale": rationale}


def _require_text(value: Any, *, maximum: int = 300) -> None:
    if type(value) is not str or not value.strip() or len(value) > maximum:
        raise CandidateError("invalid_text")


def admit_route(request: TaskRequest, candidate: Any,
                *, minimum_confidence: float = 0.75) -> RouteDecision:
    """Validate a model candidate, then apply program-owned route admission rules."""
    if (type(minimum_confidence) not in (int, float)
            or isinstance(minimum_confidence, bool)
            or not 0 <= minimum_confidence <= 1):
        raise ValueError("invalid_minimum_confidence")
    if type(candidate) is not dict or set(candidate) != {"route", "confidence", "rationale"}:
        raise CandidateError("invalid_route_candidate_fields")
    route = candidate["route"]
    confidence = candidate["confidence"]
    _require_text(route, maximum=40)
    _require_text(candidate["rationale"])
    if route not in ROUTES:
        raise CandidateError("route_not_allowed")
    if (type(confidence) not in (int, float)
            or isinstance(confidence, bool) or not 0 <= confidence <= 1):
        raise CandidateError("invalid_confidence")

    required = {
        "knowledge": ("confirmed_os", "confirmed_client_version"),
        "ticket_status": ("ticket_id",),
        "combined": ("confirmed_os", "confirmed_client_version", "ticket_id"),
        "clarify": (),
    }[route]
    missing = tuple(field for field in required if getattr(request, field) is None)
    if route == "clarify":
        return RouteDecision("clarify", "model_requested_clarification", float(confidence), missing)
    if missing:
        return RouteDecision("clarify", "required_input_missing", float(confidence), missing)
    if confidence < minimum_confidence:
        return RouteDecision("clarify", "confidence_below_threshold", float(confidence))
    return RouteDecision(route, "route_admitted", float(confidence))


@dataclass(frozen=True)
class InputRef:
    source: str
    field: str
    node_id: str | None = None


def request_ref(field: str) -> InputRef:
    return InputRef("request", field)


def step_ref(node_id: str) -> InputRef:
    return InputRef("step", "result", node_id)


@dataclass(frozen=True)
class Node:
    id: str
    capability: str
    depends_on: tuple[str, ...]
    inputs: dict[str, InputRef]
    max_attempts: int = 1


@dataclass(frozen=True)
class Plan:
    plan_id: str
    route: str
    nodes: tuple[Node, ...]
    delivery_node: str


def build_plan(route: str) -> Plan:
    """Create the small reference plan used by the lab; no LLM is called."""
    if route not in RUNNABLE_ROUTES:
        raise PlanError("route_has_no_executable_plan")
    nodes: list[Node] = []
    delivery_inputs: dict[str, InputRef] = {}
    dependencies: list[str] = []
    if route in {"knowledge", "combined"}:
        nodes.append(Node(
            "knowledge", "search_knowledge", (),
            {"issue": request_ref("issue"), "os": request_ref("confirmed_os"),
             "client_version": request_ref("confirmed_client_version")},
            max_attempts=2,
        ))
        dependencies.append("knowledge")
        delivery_inputs["knowledge"] = step_ref("knowledge")
    if route in {"ticket_status", "combined"}:
        nodes.append(Node(
            "ticket", "get_ticket", (), {"ticket_id": request_ref("ticket_id")},
            max_attempts=2,
        ))
        dependencies.append("ticket")
        delivery_inputs["ticket"] = step_ref("ticket")
    nodes.append(Node("deliver", "compose_answer", tuple(dependencies), delivery_inputs))
    return Plan(f"{route}-v1", route, tuple(nodes), "deliver")


def validate_plan(plan: Plan, request: TaskRequest) -> None:
    """Validate structure, data flow, route scope, resources, and DAG reachability."""
    if type(plan) is not Plan:
        raise PlanError("invalid_plan_type")
    if type(plan.plan_id) is not str or not plan.plan_id.strip():
        raise PlanError("invalid_plan_id")
    if plan.route not in RUNNABLE_ROUTES:
        raise PlanError("invalid_plan_route")
    if type(plan.nodes) is not tuple or not 1 <= len(plan.nodes) <= MAX_NODES:
        raise PlanError("node_budget_exceeded")
    if type(plan.delivery_node) is not str:
        raise PlanError("invalid_delivery_node")

    ids = [node.id for node in plan.nodes if type(node) is Node]
    if len(ids) != len(plan.nodes):
        raise PlanError("invalid_node_type")
    if len(ids) != len(set(ids)):
        raise PlanError("duplicate_node_id")
    if any(not ID_PATTERN.fullmatch(node_id) for node_id in ids):
        raise PlanError("invalid_node_id")
    by_id = {node.id: node for node in plan.nodes}
    if plan.delivery_node not in by_id:
        raise PlanError("delivery_node_missing")

    if any(node.capability not in CAPABILITIES for node in plan.nodes):
        raise PlanError("unknown_capability")

    allowed_by_route = {
        "knowledge": {"search_knowledge", "compose_answer"},
        "ticket_status": {"get_ticket", "compose_answer"},
        "combined": CAPABILITIES,
    }[plan.route]
    required_by_route = allowed_by_route
    actual_capabilities = {node.capability for node in plan.nodes}
    if not required_by_route <= actual_capabilities or not actual_capabilities <= allowed_by_route:
        raise PlanError("capability_does_not_match_route")
    if by_id[plan.delivery_node].capability != "compose_answer":
        raise PlanError("delivery_node_must_compose_answer")
    if sum(node.capability == "compose_answer" for node in plan.nodes) != 1:
        raise PlanError("plan_requires_one_delivery_node")

    total_attempts = 0
    request_fields = set(TaskRequest.__dataclass_fields__)
    for node in plan.nodes:
        if type(node.depends_on) is not tuple or len(node.depends_on) != len(set(node.depends_on)):
            raise PlanError("invalid_dependencies")
        if any(dep not in by_id for dep in node.depends_on):
            raise PlanError("unknown_dependency")
        if node.id in node.depends_on:
            raise PlanError("self_dependency")
        if type(node.max_attempts) is not int or isinstance(node.max_attempts, bool) \
                or not 1 <= node.max_attempts <= 3:
            raise PlanError("invalid_attempt_limit")
        if node.max_attempts > 1 and node.capability not in READ_CAPABILITIES:
            raise PlanError("only_read_capabilities_may_retry")
        total_attempts += node.max_attempts
        if type(node.inputs) is not dict or len(node.inputs) > 8:
            raise PlanError("invalid_node_inputs")
        if any(type(name) is not str or not name for name in node.inputs):
            raise PlanError("invalid_input_name")
        expected = {
            "search_knowledge": {"issue", "os", "client_version"},
            "get_ticket": {"ticket_id"},
        }.get(node.capability)
        if expected is not None and set(node.inputs) != expected:
            raise PlanError("capability_input_contract_mismatch")
        if node.capability == "compose_answer":
            delivery_inputs = {
                "knowledge": {"knowledge"},
                "ticket_status": {"ticket"},
                "combined": {"knowledge", "ticket"},
            }[plan.route]
            if set(node.inputs) != delivery_inputs:
                raise PlanError("delivery_input_contract_mismatch")
        for ref in node.inputs.values():
            if type(ref) is not InputRef:
                raise PlanError("invalid_input_reference")
        read_input_sources = {
            "search_knowledge": {
                "issue": "issue",
                "os": "confirmed_os",
                "client_version": "confirmed_client_version",
            },
            "get_ticket": {"ticket_id": "ticket_id"},
        }.get(node.capability)
        if read_input_sources is not None:
            for name, request_field in read_input_sources.items():
                ref = node.inputs[name]
                if (ref.source != "request" or ref.field != request_field
                        or ref.node_id is not None):
                    raise PlanError("capability_input_source_mismatch")
        if node.capability == "compose_answer":
            expected_sources = {
                "knowledge": "search_knowledge",
                "ticket": "get_ticket",
            }
            for name, ref in node.inputs.items():
                if (ref.source != "step" or ref.node_id not in by_id
                        or by_id[ref.node_id].capability != expected_sources[name]):
                    raise PlanError("delivery_input_source_mismatch")
        for ref in node.inputs.values():
            if ref.source == "request":
                if ref.node_id is not None or ref.field not in request_fields:
                    raise PlanError("invalid_request_reference")
                value = getattr(request, ref.field)
                if value is None:
                    raise PlanError("plan_references_missing_request_field")
            elif ref.source == "step":
                if ref.field != "result" or ref.node_id not in by_id:
                    raise PlanError("invalid_step_reference")
                if ref.node_id not in node.depends_on:
                    raise PlanError("step_reference_requires_direct_dependency")
            else:
                raise PlanError("unknown_input_reference_source")
    if total_attempts > MAX_TOTAL_ATTEMPTS:
        raise PlanError("attempt_budget_exceeded")

    # Kahn's algorithm rejects cycles before any execution begins.
    indegree = {node.id: len(node.depends_on) for node in plan.nodes}
    dependents: dict[str, list[str]] = {node.id: [] for node in plan.nodes}
    for node in plan.nodes:
        for dependency in node.depends_on:
            dependents[dependency].append(node.id)
    ready = sorted(node_id for node_id, degree in indegree.items() if degree == 0)
    visited = 0
    while ready:
        node_id = ready.pop(0)
        visited += 1
        for dependent in sorted(dependents[node_id]):
            indegree[dependent] -= 1
            if indegree[dependent] == 0:
                ready.append(dependent)
                ready.sort()
    if visited != len(plan.nodes):
        raise PlanError("plan_contains_cycle")

    ancestors: set[str] = set()
    stack = [plan.delivery_node]
    while stack:
        node_id = stack.pop()
        if node_id in ancestors:
            continue
        ancestors.add(node_id)
        stack.extend(by_id[node_id].depends_on)
    if ancestors != set(by_id):
        raise PlanError("node_cannot_reach_delivery")


class ReadCache:
    """Successful read results scoped to fixed fixture data and policy revisions."""

    def __init__(self, *, data_revision: str = "fixture-data-v1",
                 policy_revision: str = "fixture-policy-v1"):
        for revision in (data_revision, policy_revision):
            if type(revision) is not str or not revision.strip():
                raise ValueError("cache_revision_must_be_nonempty_text")
        self.data_revision = data_revision
        self.policy_revision = policy_revision
        self._values: dict[str, dict[str, Any]] = {}
        self.hits = 0

    def signature(self, scope: Scope, capability: str, inputs: dict[str, Any]) -> str:
        canonical = json.dumps({
            "data_revision": self.data_revision,
            "policy_revision": self.policy_revision,
            "scope": asdict(scope),
            "capability": capability,
            "inputs": inputs,
        }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()

    def get(self, scope: Scope, capability: str,
            inputs: dict[str, Any]) -> dict[str, Any] | None:
        key = self.signature(scope, capability, inputs)
        value = self._values.get(key)
        if value is None:
            return None
        if validate_backend_result(capability, inputs, value) != ("ok", None):
            self._values.pop(key, None)
            return None
        self.hits += 1
        return deepcopy(value)

    def put(self, scope: Scope, capability: str, inputs: dict[str, Any],
            result: dict[str, Any]) -> bool:
        if (capability in READ_CAPABILITIES
                and validate_backend_result(capability, inputs, result) == ("ok", None)):
            self._values[self.signature(scope, capability, inputs)] = deepcopy(result)
            return True
        return False


class SyntheticBackend:
    """Deterministic read fixtures plus an internal answer composer."""

    def __init__(self, *, fail_first: dict[str, int] | None = None,
                 revoked_ticket_users: set[str] | None = None):
        self.fail_first = dict(fail_first or {})
        self.revoked_ticket_users = set(revoked_ticket_users or ())
        self.dispatches: list[dict[str, Any]] = []
        self.admissions: list[tuple[str, str]] = []

    def admit(self, scope: Scope, capability: str, inputs: dict[str, Any]) -> None:
        """Re-check scope on every run, including before a possible cache hit."""
        self.admissions.append((capability, scope.user_id))
        if scope.tenant_id != "demo-company":
            raise AdmissionError("scope_not_allowed")
        if capability == "get_ticket":
            if scope.user_id in self.revoked_ticket_users:
                raise AdmissionError("permission_revoked")
            owner = {"IT-100": "employee-a", "IT-200": "employee-b"}.get(inputs["ticket_id"])
            if owner != scope.user_id:
                raise AdmissionError("not_found_or_forbidden")

    def execute(self, scope: Scope, capability: str,
                inputs: dict[str, Any]) -> dict[str, Any]:
        self.dispatches.append({"capability": capability, "user_id": scope.user_id,
                                "inputs": deepcopy(inputs)})
        remaining = self.fail_first.get(capability, 0)
        if remaining > 0:
            self.fail_first[capability] = remaining - 1
            raise RetryableReadError("read_timeout")
        if capability == "search_knowledge":
            found = (inputs["os"].casefold() == "windows"
                     and inputs["client_version"] == "5.2"
                     and "e104" in inputs["issue"].casefold())
            documents = ([{"evidence_id": "KB-E104-g2",
                           "topic": "VPN E104 Windows 5.2",
                           "text": "校时无效时联系 IT，并提供客户端日志。"}]
                         if found else [])
            return {"status": "ok", "documents": documents}
        if capability == "get_ticket":
            return {"status": "ok", "ticket": {
                "ticket_id": inputs["ticket_id"], "ticket_status": "in_progress"}}
        if capability == "compose_answer":
            if "knowledge" in inputs and not inputs["knowledge"]["documents"]:
                return {"status": "error", "code": "knowledge_evidence_missing"}
            result: dict[str, Any] = {"status": "ok"}
            if "knowledge" in inputs:
                documents = inputs["knowledge"].get("documents", [])
                result["evidence_ids"] = [doc["evidence_id"] for doc in documents]
            if "ticket" in inputs:
                result["ticket"] = deepcopy(inputs["ticket"]["ticket"])
            result["answer"] = "已根据本次成功查询的结构化结果生成答复。"
            return result
        raise ValueError("backend_received_unknown_capability")


def _nonempty_text(value: Any) -> bool:
    return type(value) is str and bool(value.strip())


def _valid_ticket(ticket: Any, expected_id: str) -> bool:
    return (type(ticket) is dict
            and set(ticket) == {"ticket_id", "ticket_status"}
            and ticket["ticket_id"] == expected_id
            and _nonempty_text(ticket["ticket_id"])
            and _nonempty_text(ticket["ticket_status"])
            and ticket["ticket_status"] in TICKET_STATUSES)


def validate_backend_result(capability: str, inputs: dict[str, Any],
                            result: Any) -> tuple[str, str | None]:
    """Return (ok|error|invalid, code) for one capability's result contract."""
    if type(result) is not dict:
        return "invalid", "invalid_backend_result"
    if result.get("status") == "error":
        if set(result) == {"status", "code"} and _nonempty_text(result["code"]):
            return "error", result["code"]
        return "invalid", "invalid_backend_result"
    if result.get("status") != "ok":
        return "invalid", "invalid_backend_result"

    if capability == "search_knowledge":
        if set(result) != {"status", "documents"} or type(result["documents"]) is not list:
            return "invalid", "invalid_backend_result"
        evidence_ids: list[str] = []
        for document in result["documents"]:
            if (type(document) is not dict
                    or set(document) != {"evidence_id", "topic", "text"}
                    or not all(_nonempty_text(document.get(field))
                               for field in ("evidence_id", "topic", "text"))):
                return "invalid", "invalid_backend_result"
            evidence_ids.append(document["evidence_id"])
        if len(evidence_ids) != len(set(evidence_ids)):
            return "invalid", "invalid_backend_result"
    elif capability == "get_ticket":
        if (set(result) != {"status", "ticket"}
                or not _valid_ticket(result["ticket"], inputs["ticket_id"])):
            return "invalid", "invalid_backend_result"
    elif capability == "compose_answer":
        expected_fields = {"status", "answer"}
        if "knowledge" in inputs:
            expected_fields.add("evidence_ids")
        if "ticket" in inputs:
            expected_fields.add("ticket")
        if set(result) != expected_fields or not _nonempty_text(result["answer"]):
            return "invalid", "invalid_backend_result"
        if "knowledge" in inputs:
            expected_ids = [doc["evidence_id"] for doc in inputs["knowledge"]["documents"]]
            if (not expected_ids or type(result["evidence_ids"]) is not list
                    or result["evidence_ids"] != expected_ids):
                return "invalid", "invalid_backend_result"
        if "ticket" in inputs:
            upstream = inputs["ticket"]["ticket"]
            if (not _valid_ticket(result["ticket"], upstream["ticket_id"])
                    or result["ticket"] != upstream):
                return "invalid", "invalid_backend_result"
    else:
        return "invalid", "invalid_backend_result"
    return "ok", None


class Executor:
    """Validate a plan, then simulate deterministic ready-wave scheduling."""

    def __init__(self, scope: Scope, *, backend: SyntheticBackend | None = None,
                 cache: ReadCache | None = None):
        self.scope = scope
        self.backend = backend if backend is not None else SyntheticBackend()
        self.cache = cache if cache is not None else ReadCache()
        self.events: list[dict[str, Any]] = []

    def _event(self, event: str, **fields: Any) -> None:
        self.events.append({"seq": len(self.events) + 1, "event": event, **fields})

    @staticmethod
    def _resolve(node: Node, request: TaskRequest,
                 states: dict[str, dict[str, Any]]) -> dict[str, Any]:
        values: dict[str, Any] = {}
        for name, ref in node.inputs.items():
            if ref.source == "request":
                values[name] = getattr(request, ref.field)
            else:
                values[name] = deepcopy(states[ref.node_id]["result"])
        return values

    def _execute_node(self, node: Node, request: TaskRequest,
                      states: dict[str, dict[str, Any]]) -> None:
        state = states[node.id]
        inputs = self._resolve(node, request, states)
        try:
            self.backend.admit(self.scope, node.capability, inputs)
        except AdmissionError as error:
            state.update(status="failed", error={"status": "error", "code": error.code})
            self._event("node_rejected", node_id=node.id, code=error.code)
            return

        if node.capability in READ_CAPABILITIES:
            cached = self.cache.get(self.scope, node.capability, inputs)
            if cached is not None:
                state.update(status="reused", result=cached)
                self._event("node_reused", node_id=node.id)
                return

        for attempt in range(1, node.max_attempts + 1):
            state["attempts"] = attempt
            self._event("node_started", node_id=node.id, attempt=attempt)
            try:
                result = self.backend.execute(self.scope, node.capability, inputs)
            except RetryableReadError as error:
                state["error"] = {"status": "error", "code": error.code}
                self._event("node_attempt_failed", node_id=node.id, attempt=attempt,
                            code=error.code, retrying=attempt < node.max_attempts)
                if attempt < node.max_attempts:
                    continue
                state["status"] = "failed"
                return
            outcome, code = validate_backend_result(node.capability, inputs, result)
            if outcome != "ok":
                error = (deepcopy(result) if outcome == "error"
                         else {"status": "error", "code": "invalid_backend_result"})
                state.update(status="failed", error=error)
                self._event("node_failed", node_id=node.id,
                            code=code)
                return
            state.update(status="succeeded", result=deepcopy(result), error=None)
            self.cache.put(self.scope, node.capability, inputs, result)
            self._event("node_succeeded", node_id=node.id, attempt=attempt)
            return

    def run(self, plan: Plan, request: TaskRequest) -> dict[str, Any]:
        validate_plan(plan, request)
        states = {node.id: {"status": "pending", "attempts": 0,
                            "result": None, "error": None} for node in plan.nodes}
        waves: list[list[str]] = []
        done = {"succeeded", "reused"}

        while any(state["status"] == "pending" for state in states.values()):
            # Propagate dependency failure before selecting the next ready wave.
            changed = True
            while changed:
                changed = False
                for node in plan.nodes:
                    if states[node.id]["status"] != "pending":
                        continue
                    failed_dependencies = [
                        dep for dep in node.depends_on
                        if states[dep]["status"] in {"failed", "blocked"}
                    ]
                    if failed_dependencies:
                        states[node.id].update(
                            status="blocked",
                            error={"status": "not_executed", "code": "dependency_failed",
                                   "dependencies": failed_dependencies},
                        )
                        self._event("node_blocked", node_id=node.id,
                                    dependencies=failed_dependencies)
                        changed = True
            ready = [node for node in plan.nodes
                     if states[node.id]["status"] == "pending"
                     and all(states[dep]["status"] in done for dep in node.depends_on)]
            if not ready:
                break
            wave = [node.id for node in ready]
            waves.append(wave)
            self._event("wave_ready", wave=len(waves), nodes=wave)
            # Deliberately sequential: a wave shows readiness, not real parallel execution.
            for node in ready:
                self._execute_node(node, request, states)

        delivery = states[plan.delivery_node]
        successful = {node_id: deepcopy(state["result"])
                      for node_id, state in states.items()
                      if state["status"] in done and node_id != plan.delivery_node}
        if delivery["status"] in done:
            status, reason, output = "completed", "delivery_succeeded", deepcopy(delivery["result"])
        elif successful:
            code = (delivery["error"].get("code")
                    if delivery["status"] == "failed"
                    and type(delivery["error"]) is dict else None)
            status, reason, output = "partial", code or "delivery_not_completed", None
        else:
            status, reason, output = "failed", "no_deliverable_result", None
        self._event("run_ended", status=status, reason=reason)
        return {
            "status": status,
            "reason": reason,
            "route": plan.route,
            "waves": waves,
            "steps": deepcopy(states),
            "partial_results": successful if status == "partial" else {},
            "output": output,
            "events": deepcopy(self.events),
        }


def run_workflow(scope: Scope, request: TaskRequest, candidate: dict[str, Any], *,
                 backend: SyntheticBackend | None = None,
                 cache: ReadCache | None = None) -> dict[str, Any]:
    """Run route admission for every independent run, then validate and execute."""
    decision = admit_route(request, candidate)
    if not decision.runnable:
        return {
            "status": "awaiting_input", "reason": decision.reason,
            "route": decision.route, "missing_fields": list(decision.missing_fields),
            "confidence": decision.confidence,
        }
    result = Executor(scope, backend=backend, cache=cache).run(build_plan(decision.route), request)
    result["confidence"] = decision.confidence
    return result


def _rejected(reason: str) -> dict[str, Any]:
    return {"status": "rejected", "reason": reason}


def run_demo_scenarios() -> dict[str, dict[str, Any]]:
    scope, request = Scope(), TaskRequest()
    combined = route_candidate("combined")
    scenarios: dict[str, dict[str, Any]] = {
        "normal": run_workflow(scope, request, combined),
        "low_confidence": run_workflow(
            scope, request, route_candidate("combined", confidence=0.40)),
        "missing_input": run_workflow(
            scope, TaskRequest(ticket_id=None), combined),
    }
    try:
        run_workflow(scope, request, route_candidate("delete_ticket"))
    except CandidateError as error:
        scenarios["invalid_route"] = _rejected(str(error))

    valid = build_plan("combined")
    cyclic = Plan("cycle", "combined", (
        Node("knowledge", "search_knowledge", ("deliver",), valid.nodes[0].inputs, 2),
        valid.nodes[1], valid.nodes[2],
    ), "deliver")
    try:
        validate_plan(cyclic, request)
    except PlanError as error:
        scenarios["cyclic_plan"] = _rejected(str(error))

    failure_backend = SyntheticBackend(fail_first={"search_knowledge": 2})
    scenarios["branch_failure"] = run_workflow(
        scope, request, combined, backend=failure_backend)
    retry_backend = SyntheticBackend(fail_first={"search_knowledge": 1})
    scenarios["retry_then_success"] = run_workflow(
        scope, request, combined, backend=retry_backend)

    cache = ReadCache()
    cache_backend = SyntheticBackend()
    first = run_workflow(scope, request, combined, backend=cache_backend, cache=cache)
    second = run_workflow(scope, request, combined, backend=cache_backend, cache=cache)
    second["previous_status"] = first["status"]
    second["cache_hits"] = cache.hits
    second["actual_read_dispatches"] = sum(
        call["capability"] in READ_CAPABILITIES for call in cache_backend.dispatches)
    scenarios["cache_reuse"] = second

    changed_cache = ReadCache()
    changed_backend = SyntheticBackend()
    run_workflow(scope, request, route_candidate("knowledge"),
                 backend=changed_backend, cache=changed_cache)
    changed = run_workflow(
        scope, TaskRequest(confirmed_client_version="5.3"), route_candidate("knowledge"),
        backend=changed_backend, cache=changed_cache)
    changed["cache_hits"] = changed_cache.hits
    changed["actual_search_dispatches"] = sum(
        call["capability"] == "search_knowledge" for call in changed_backend.dispatches)
    scenarios["input_changed"] = changed
    return scenarios


def main() -> None:
    for name, result in run_demo_scenarios().items():
        summary = {key: result[key] for key in ("status", "reason")}
        for key in ("route", "missing_fields", "waves", "cache_hits",
                    "actual_read_dispatches", "actual_search_dispatches"):
            if key in result:
                summary[key] = result[key]
        print(f"{name}: {json.dumps(summary, ensure_ascii=False, sort_keys=True)}")


if __name__ == "__main__":
    main()
