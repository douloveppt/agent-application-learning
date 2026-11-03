#!/usr/bin/env python3
"""A bounded, in-process teaching subset of MCP revision 2026-07-28.

No HTTP, stdio, OAuth, LLM, network, third-party SDK or business writes.
Principal is a trusted fixture passed beside JSON, not authenticated by MCP.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
from typing import Any, Callable


VERSION = "2026-07-28"
PREFIX = "io.modelcontextprotocol/"
MAX_BYTES = 32 * 1024
MAX_PAGES = 8
READ_RETRY_POLICY = frozenset({"tickets.get_own", "knowledge.search"})


@dataclass(frozen=True)
class Principal:
    tenant_id: str
    user_id: str
    allowed_tools: frozenset[str] = READ_RETRY_POLICY


EMPLOYEE = Principal("acme", "employee-17")
TICKETS = (
    {"tenant_id": "acme", "user_id": "employee-17", "ticket_id": "IT-2048", "status": "open"},
    {"tenant_id": "acme", "user_id": "employee-99", "ticket_id": "IT-9999", "status": "open"},
    {"tenant_id": "other", "user_id": "employee-17", "ticket_id": "IT-7000", "status": "open"},
)


def schema(field: str, maximum: int) -> dict[str, Any]:
    return {"type": "object", "properties": {field: {"type": "string", "minLength": 1, "maxLength": maximum}},
            "required": [field], "additionalProperties": False}


TOOLS = (
    {"name": "knowledge.search", "description": "Search this tenant's fixed VPN knowledge fixture.",
     "inputSchema": schema("query", 200), "annotations": {"readOnlyHint": True}},
    {"name": "tickets.get_own", "description": "Read a ticket owned by the current employee in the current tenant.",
     "inputSchema": schema("ticket_id", 64), "annotations": {"readOnlyHint": True}},
)


class ProtocolError(RuntimeError):
    def __init__(self, code: int, message: str, data: Any = None):
        super().__init__(message)
        self.code, self.data = code, data


class InvalidResult(RuntimeError):
    pass


class TransportDisconnected(RuntimeError):
    pass


def encoded(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")


def complete(**fields: Any) -> dict[str, Any]:
    return {"resultType": "complete", **fields}


def tool_result(data: dict[str, Any], *, is_error: bool = False) -> dict[str, Any]:
    return complete(content=[{"type": "text", "text": json.dumps(data, ensure_ascii=False)}],
                    structuredContent=data, isError=is_error)


class Server:
    """Fixed two-tool server; manual validation covers this lab's contracts only."""

    def __init__(self) -> None:
        self.handler_calls = 0

    def handle(self, request: Any, principal: Principal) -> dict[str, Any]:
        request_id = request.get("id") if isinstance(request, dict) else None
        try:
            if (not isinstance(request, dict) or request.get("jsonrpc") != "2.0"
                    or isinstance(request_id, bool) or not isinstance(request_id, (str, int))
                    or not isinstance(request.get("method"), str)):
                raise ProtocolError(-32600, "Invalid request")
            params = request.get("params")
            if not isinstance(params, dict) or not isinstance(params.get("_meta"), dict):
                raise ProtocolError(-32602, "Required request metadata is missing")
            meta = params["_meta"]
            if not isinstance(meta.get(PREFIX + "protocolVersion"), str):
                raise ProtocolError(-32602, "protocolVersion must be a string")
            if meta[PREFIX + "protocolVersion"] != VERSION:
                raise ProtocolError(-32022, "Unsupported protocol version",
                                    {"supported": [VERSION], "requested": meta[PREFIX + "protocolVersion"]})
            if not isinstance(meta.get(PREFIX + "clientCapabilities"), dict):
                raise ProtocolError(-32602, "clientCapabilities must be an object")
            # clientInfo and tool descriptions are self-reported, never identity proof.
            result = self.dispatch(request["method"], params, principal)
            return {"jsonrpc": "2.0", "id": request_id, "result": result}
        except ProtocolError as exc:
            error = {"code": exc.code, "message": str(exc)}
            if exc.data is not None:
                error["data"] = exc.data
            return {"jsonrpc": "2.0", "id": request_id, "error": error}

    def dispatch(self, method: str, params: dict[str, Any], principal: Principal) -> dict[str, Any]:
        if method == "server/discover":
            return complete(supportedVersions=[VERSION], capabilities={"tools": {}}, ttlMs=0,
                            cacheScope="public", _meta={PREFIX + "serverInfo": {"name": "chapter16-fixture", "version": "1.0.0"}})
        if method == "tools/list":
            visible = [tool for tool in TOOLS if tool["name"] in principal.allowed_tools]
            # Empty string deliberately exercises a valid opaque cursor.
            cursor = params.get("cursor")
            if "cursor" not in params:
                index = 0
            elif cursor == "":
                index = 1
            else:
                raise ProtocolError(-32602, "Unknown cursor")
            result = complete(tools=visible[index:index + 1], ttlMs=0, cacheScope="private")
            if index + 1 < len(visible):
                result["nextCursor"] = ""
            return result
        if method != "tools/call":
            raise ProtocolError(-32601, "Method not found")
        name, arguments = params.get("name"), params.get("arguments")
        if not isinstance(name, str) or not isinstance(arguments, dict):
            raise ProtocolError(-32602, "Malformed tools/call request")
        if name not in {tool["name"] for tool in TOOLS}:
            raise ProtocolError(-32602, "Unknown tool")
        if name not in principal.allowed_tools:
            return tool_result({"status": "denied", "code": "not_authorized"}, is_error=True)
        field, maximum = ("ticket_id", 64) if name == "tickets.get_own" else ("query", 200)
        if (set(arguments) != {field} or not isinstance(arguments.get(field), str)
                or len(arguments[field]) > maximum or not arguments[field].strip()):
            return tool_result({"status": "invalid_input", "code": "invalid_arguments"}, is_error=True)
        self.handler_calls += 1
        if name == "knowledge.search":
            entries = ([{"document_id": "KB-VPN-1", "text": "Check VPN client version and the displayed error."}]
                       if principal.tenant_id == "acme" and "vpn" in arguments[field].lower() else [])
            return tool_result({"status": "ok", "entries": entries})
        ticket = next((row for row in TICKETS if row["ticket_id"] == arguments[field]
                       and row["tenant_id"] == principal.tenant_id and row["user_id"] == principal.user_id), None)
        if ticket is None:
            # Same response for missing and inaccessible IDs; do not disclose existence.
            return tool_result({"status": "not_found_or_denied", "code": "ticket_unavailable"}, is_error=True)
        return tool_result({"status": "ok", "ticket": {k: ticket[k] for k in ("ticket_id", "status")}})


class InProcessTransport:
    """JSON byte round-trip; faults are deterministic fixtures, not sockets."""

    def __init__(self, server: Server, principal: Principal = EMPLOYEE,
                 fault: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]] | None = None,
                 disconnect_once: bool = False) -> None:
        self.server, self.principal, self.fault = server, principal, fault
        self.disconnect_once = disconnect_once
        self.calls = 0
        self.requests: list[dict[str, Any]] = []

    def exchange(self, payload: bytes) -> bytes:
        self.calls += 1
        if len(payload) > MAX_BYTES:
            raise ValueError("Request exceeds lab byte budget")
        request = json.loads(payload)
        self.requests.append(request)
        response = self.server.handle(request, self.principal)
        if self.disconnect_once and request.get("method") == "tools/call":
            self.disconnect_once = False
            raise TransportDisconnected("Fixture dropped the response after the read executed")
        if self.fault:
            response = self.fault(request, response)
        return encoded(response)


class Client:
    def __init__(self, transport: InProcessTransport, version: str = VERSION) -> None:
        self.transport, self.version, self.sequence = transport, version, 0

    def rpc(self, method: str, **params: Any) -> dict[str, Any]:
        self.sequence += 1
        request_id = f"rpc-{self.sequence}"
        params["_meta"] = {PREFIX + "protocolVersion": self.version,
                           PREFIX + "clientCapabilities": {},
                           PREFIX + "clientInfo": {"name": "chapter16-host", "version": "1.0.0"}}
        raw = self.transport.exchange(encoded({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}))
        if len(raw) > MAX_BYTES:
            raise InvalidResult("Response exceeds lab byte budget")
        try:
            response = json.loads(raw)
        except (ValueError, UnicodeError) as exc:
            raise InvalidResult("Response is not valid UTF-8 JSON") from exc
        if (not isinstance(response, dict) or response.get("jsonrpc") != "2.0"
                or response.get("id") != request_id or ("result" in response) == ("error" in response)):
            raise InvalidResult("Invalid JSON-RPC envelope or correlation ID")
        if "error" in response:
            error = response["error"]
            if (not isinstance(error, dict) or type(error.get("code")) is not int
                    or not isinstance(error.get("message"), str)):
                raise InvalidResult("Invalid error object")
            raise ProtocolError(error["code"], error["message"], error.get("data"))
        result = response["result"]
        if not isinstance(result, dict) or result.get("resultType") != "complete":
            raise InvalidResult("Lab supports only explicit complete results from the pinned modern fixture")
        if method in {"server/discover", "tools/list"}:
            if (type(result.get("ttlMs")) is not int or result["ttlMs"] < 0
                    or not isinstance(result.get("cacheScope"), str)
                    or result["cacheScope"] not in {"public", "private"}):
                raise InvalidResult("Invalid cache metadata")
        return result

    def discover(self) -> dict[str, Any]:
        result = self.rpc("server/discover")
        if (not isinstance(result.get("supportedVersions"), list)
                or not all(isinstance(v, str) for v in result["supportedVersions"])
                or self.version not in result["supportedVersions"]
                or not isinstance(result.get("capabilities"), dict)):
            raise InvalidResult("Invalid discovery contract")
        return result

    def list_tools(self) -> list[dict[str, Any]]:
        tools: list[dict[str, Any]] = []
        seen_names: set[str] = set()
        seen_cursors: set[str] = set()
        params: dict[str, Any] = {}
        for _ in range(MAX_PAGES):
            result = self.rpc("tools/list", **params)
            page = result.get("tools")
            if not isinstance(page, list):
                raise InvalidResult("tools must be a list")
            for tool in page:
                if (not isinstance(tool, dict) or not isinstance(tool.get("name"), str)
                        or not tool["name"] or tool["name"] in seen_names
                        or not isinstance(tool.get("inputSchema"), dict)):
                    raise InvalidResult("Invalid or duplicate tool definition")
                seen_names.add(tool["name"])
                tools.append(tool)
            if "nextCursor" not in result:
                return tools
            cursor = result["nextCursor"]
            if not isinstance(cursor, str) or cursor in seen_cursors:
                raise InvalidResult("Invalid or repeated pagination cursor")
            seen_cursors.add(cursor)
            params = {"cursor": cursor}
        raise InvalidResult("Pagination exceeded lab page budget")

    def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        # Policy is host-owned; annotations from tools/list do not authorize retry.
        attempts = 2 if name in READ_RETRY_POLICY else 1
        for attempt in range(attempts):
            try:
                result = self.rpc("tools/call", name=name, arguments=arguments)
                break
            except TransportDisconnected:
                if attempt + 1 == attempts:
                    raise
        if (type(result.get("isError")) is not bool or not isinstance(result.get("content"), list)
                or len(result["content"]) != 1
                or any(not isinstance(item, dict) or item.get("type") != "text"
                       or not isinstance(item.get("text"), str) for item in result["content"])
                or not isinstance(result.get("structuredContent"), dict)):
            raise InvalidResult("Invalid lab tool result contract")
        data = result["structuredContent"]
        # This fixture promises one JSON text mirror. General MCP may use other
        # content layouts; equality here is our application contract, not MCP's.
        try:
            text_data = json.loads(result["content"][0]["text"])
        except ValueError as exc:
            raise InvalidResult("Lab text mirror is not valid JSON") from exc
        if text_data != data:
            raise InvalidResult("Lab text mirror conflicts with structuredContent")
        if result["isError"]:
            if not isinstance(data.get("status"), str) or not isinstance(data.get("code"), str):
                raise InvalidResult("Invalid lab tool error contract")
        elif name == "tickets.get_own":
            ticket = data.get("ticket")
            if (data.get("status") != "ok" or not isinstance(ticket, dict)
                    or set(ticket) != {"ticket_id", "status"}
                    or not all(isinstance(value, str) for value in ticket.values())
                    or ticket["ticket_id"] != arguments.get("ticket_id")):
                raise InvalidResult("Invalid ticket output")
        elif name == "knowledge.search":
            entries = data.get("entries")
            if (data.get("status") != "ok" or not isinstance(entries, list)
                    or any(not isinstance(item, dict) or set(item) != {"document_id", "text"}
                           or not all(isinstance(v, str) for v in item.values()) for item in entries)):
                raise InvalidResult("Invalid knowledge output")
        return result


def setup(**kwargs: Any) -> tuple[Server, InProcessTransport, Client]:
    server = Server()
    transport = InProcessTransport(server, **kwargs)
    return server, transport, Client(transport)


def observe(action: Callable[[], Any]) -> dict[str, Any]:
    try:
        return {"outcome": "result", "value": action()}
    except ProtocolError as exc:
        return {"outcome": "protocol_error", "code": exc.code, "data": exc.data}
    except (InvalidResult, TransportDisconnected) as exc:
        return {"outcome": type(exc).__name__, "message": str(exc)}


def run_scenario(name: str) -> dict[str, Any]:
    server, transport, client = setup()
    if name == "normal":
        return {"discovery": client.discover(), "result": client.call("tickets.get_own", {"ticket_id": "IT-2048"})}
    if name == "pagination":
        return {"tools": [tool["name"] for tool in client.list_tools()], "pages": transport.calls,
                "second_cursor": transport.requests[1]["params"]["cursor"]}
    if name == "incompatible_version":
        client.version = "1900-01-01"
        return observe(client.discover)
    if name == "identity_injection":
        result = client.call("tickets.get_own", {"ticket_id": "IT-9999", "user_id": "employee-99"})
        return {"result": result, "handler_calls": server.handler_calls}
    if name == "resource_boundary":
        return {ticket_id: client.call("tickets.get_own", {"ticket_id": ticket_id})["structuredContent"]
                for ticket_id in ("IT-9999", "IT-7000", "IT-MISSING")}
    if name == "error_layers":
        return {"unknown_tool": observe(lambda: client.rpc("tools/call", name="not.installed", arguments={})),
                "tool_error": client.call("tickets.get_own", {"ticket_id": "IT-MISSING"})}
    if name == "read_retry":
        server, transport, client = setup(disconnect_once=True)
        result = client.call("tickets.get_own", {"ticket_id": "IT-2048"})
        return {"result": result, "attempts": transport.calls, "handler_calls": server.handler_calls,
                "request_ids": [request["id"] for request in transport.requests]}
    if name == "invalid_results":
        def wrong_id(_request: dict[str, Any], response: dict[str, Any]) -> dict[str, Any]:
            return {**response, "id": "other-request"}
        def oversized(_request: dict[str, Any], response: dict[str, Any]) -> dict[str, Any]:
            response["result"]["content"][0]["text"] = "x" * (MAX_BYTES + 1)
            return response
        def bad_shape(_request: dict[str, Any], response: dict[str, Any]) -> dict[str, Any]:
            response["result"]["structuredContent"] = {"status": "ok", "ticket": "not-an-object"}
            response["result"]["content"][0]["text"] = json.dumps(response["result"]["structuredContent"])
            return response
        def wrong_ticket(_request: dict[str, Any], response: dict[str, Any]) -> dict[str, Any]:
            response["result"]["structuredContent"]["ticket"]["ticket_id"] = "IT-9999"
            response["result"]["content"][0]["text"] = json.dumps(response["result"]["structuredContent"])
            return response
        def text_conflict(_request: dict[str, Any], response: dict[str, Any]) -> dict[str, Any]:
            response["result"]["content"][0]["text"] = json.dumps({"status": "ok", "ticket": {"ticket_id": "IT-9999", "status": "open"}})
            return response
        return {label: observe(lambda c=setup(fault=fault)[2]: c.call("tickets.get_own", {"ticket_id": "IT-2048"}))
                for label, fault in (("wrong_id", wrong_id), ("oversized", oversized), ("bad_shape", bad_shape),
                                     ("wrong_ticket", wrong_ticket), ("text_conflict", text_conflict))}
    raise ValueError(f"Unknown scenario: {name}")


SCENARIOS = ("normal", "pagination", "incompatible_version", "identity_injection", "resource_boundary",
             "error_layers", "read_retry", "invalid_results")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["scenarios"])
    parser.add_argument("scenario", choices=["all", *SCENARIOS], default="all", nargs="?")
    args = parser.parse_args()
    names = SCENARIOS if args.scenario == "all" else (args.scenario,)
    print(json.dumps({name: run_scenario(name) for name in names}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
