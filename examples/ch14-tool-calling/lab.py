"""Offline tool-execution boundary lab; Python 3.11+ standard library only."""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import tempfile
import time
from typing import Any, Callable


MAX_CALL_BYTES = 4_096
MAX_RESULT_BYTES = 1_024
MAX_AUDIT_TEXT = 80
CALL_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
TOOL_NAMES = frozenset({
    "tickets.list_own",
    "tickets.preview",
    "tickets.revise",
    "tickets.submit",
    "tickets.reconcile",
})


class ToolFailure(Exception):
    def __init__(self, code: str, *, retryable: bool = False):
        super().__init__(code)
        self.code = code
        self.retryable = retryable


class ReceiptLost(Exception):
    """The provider committed, but the caller did not receive its response."""


class ProviderConflict(Exception):
    """One provider operation ID was reused for a different payload."""


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def payload_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _reject_constant(value: str) -> None:
    raise ValueError(f"non_finite_number:{value}")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_key")
        result[key] = value
    return result


def parse_call(raw: str) -> dict[str, Any]:
    """Parse a bounded JSON object while rejecting duplicates and NaN/Infinity."""
    if type(raw) is not str:
        raise ToolFailure("invalid_json")
    try:
        encoded = raw.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ToolFailure("invalid_json") from exc
    if len(encoded) > MAX_CALL_BYTES:
        raise ToolFailure("request_too_large")
    try:
        value = json.loads(raw, object_pairs_hook=_unique_object,
                           parse_constant=_reject_constant)
        # json.loads accepts escaped lone surrogates. Verify that every parsed
        # string can cross the UTF-8 boundary used by the executor and audit.
        canonical_json(value).encode("utf-8")
    except (json.JSONDecodeError, ValueError, UnicodeEncodeError, RecursionError) as exc:
        raise ToolFailure("invalid_json") from exc
    if type(value) is not dict:
        raise ToolFailure("call_must_be_object")
    return value


@dataclass(frozen=True)
class Principal:
    """Authenticated identity and grants supplied by the trusted application layer."""

    tenant_id: str
    user_id: str
    allowed_tools: frozenset[str]

    def __post_init__(self) -> None:
        if not self.tenant_id or not self.user_id:
            raise ValueError("principal_identity_required")
        if not self.allowed_tools <= TOOL_NAMES:
            raise ValueError("principal_has_unknown_tool")


def default_principal(*, tenant_id: str = "acme", user_id: str = "employee-7") -> Principal:
    return Principal(tenant_id, user_id, TOOL_NAMES)


def initialize(runtime_db: Path | str, provider_db: Path | str) -> None:
    with sqlite3.connect(runtime_db) as db:
        db.executescript(
            """
            PRAGMA foreign_keys = ON;
            CREATE TABLE IF NOT EXISTS drafts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                draft_id TEXT UNIQUE,
                tenant_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                version INTEGER NOT NULL,
                payload_json TEXT NOT NULL,
                payload_hash TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS approvals (
                approval_id TEXT PRIMARY KEY,
                operation_id TEXT NOT NULL,
                draft_id TEXT NOT NULL,
                tenant_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                action TEXT NOT NULL,
                draft_version INTEGER NOT NULL,
                payload_hash TEXT NOT NULL,
                expires_at INTEGER NOT NULL,
                created_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS actions (
                operation_id TEXT PRIMARY KEY,
                approval_id TEXT NOT NULL,
                draft_id TEXT NOT NULL,
                tenant_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                payload_hash TEXT NOT NULL,
                status TEXT NOT NULL,
                receipt_json TEXT,
                updated_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS audit_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                at INTEGER NOT NULL,
                tenant_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                call_id TEXT,
                tool_name TEXT,
                status TEXT NOT NULL,
                code TEXT NOT NULL,
                draft_id TEXT,
                operation_id TEXT
            );
            """
        )
    with sqlite3.connect(provider_db) as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS tickets (
                operation_id TEXT PRIMARY KEY,
                payload_hash TEXT NOT NULL,
                tenant_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                title TEXT NOT NULL,
                description TEXT NOT NULL,
                priority TEXT NOT NULL,
                receipt_id TEXT NOT NULL UNIQUE,
                created_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS submit_calls (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                operation_id TEXT NOT NULL,
                payload_hash TEXT NOT NULL,
                outcome TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS lookup_calls (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                operation_id TEXT NOT NULL
            );
            """
        )


def _provider_receipt(row: sqlite3.Row, *, deduplicated: bool) -> dict[str, Any]:
    return {
        "receipt_id": row["receipt_id"],
        "operation_id": row["operation_id"],
        "payload_hash": row["payload_hash"],
        "deduplicated": deduplicated,
    }


def provider_submit(provider_db: Path | str, *, operation_id: str,
                    principal: Principal, payload: dict[str, Any],
                    expected_hash: str, now: int,
                    lose_receipt: bool = False) -> dict[str, Any]:
    """Simulate a provider-local idempotency transaction."""
    actual_hash = payload_hash(payload)
    if actual_hash != expected_hash:
        raise ProviderConflict("caller_payload_hash_mismatch")
    with sqlite3.connect(provider_db) as db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN IMMEDIATE")
        existing = db.execute(
            "SELECT * FROM tickets WHERE operation_id = ?", (operation_id,)
        ).fetchone()
        if existing is not None:
            outcome = "deduplicated" if existing["payload_hash"] == expected_hash else "conflict"
            db.execute(
                "INSERT INTO submit_calls(operation_id, payload_hash, outcome) VALUES (?, ?, ?)",
                (operation_id, expected_hash, outcome),
            )
            db.commit()
            if outcome == "conflict":
                raise ProviderConflict("operation_payload_conflict")
            receipt = _provider_receipt(existing, deduplicated=True)
        else:
            receipt_id = "ticket-" + hashlib.sha256(operation_id.encode()).hexdigest()[:12]
            db.execute(
                """INSERT INTO tickets(
                       operation_id, payload_hash, tenant_id, user_id, title,
                       description, priority, receipt_id, created_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (operation_id, expected_hash, principal.tenant_id, principal.user_id,
                 payload["title"], payload["description"], payload["priority"],
                 receipt_id, now),
            )
            db.execute(
                "INSERT INTO submit_calls(operation_id, payload_hash, outcome) VALUES (?, ?, 'created')",
                (operation_id, expected_hash),
            )
            db.commit()
            with sqlite3.connect(provider_db) as read_db:
                read_db.row_factory = sqlite3.Row
                row = read_db.execute(
                    "SELECT * FROM tickets WHERE operation_id = ?", (operation_id,)
                ).fetchone()
                assert row is not None
                receipt = _provider_receipt(row, deduplicated=False)
    if lose_receipt:
        raise ReceiptLost(operation_id)
    return receipt


def provider_lookup(provider_db: Path | str, operation_id: str) -> dict[str, Any] | None:
    with sqlite3.connect(provider_db) as db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN IMMEDIATE")
        db.execute("INSERT INTO lookup_calls(operation_id) VALUES (?)", (operation_id,))
        row = db.execute("SELECT * FROM tickets WHERE operation_id = ?", (operation_id,)).fetchone()
        db.commit()
    return None if row is None else _provider_receipt(row, deduplicated=True)


def provider_stats(provider_db: Path | str) -> dict[str, int]:
    with sqlite3.connect(provider_db) as db:
        return {
            "tickets": db.execute("SELECT COUNT(*) FROM tickets").fetchone()[0],
            "submit_calls": db.execute("SELECT COUNT(*) FROM submit_calls").fetchone()[0],
            "lookup_calls": db.execute("SELECT COUNT(*) FROM lookup_calls").fetchone()[0],
        }


def _require_fields(arguments: Any, fields: set[str]) -> dict[str, Any]:
    if type(arguments) is not dict or set(arguments) != fields:
        raise ToolFailure("invalid_arguments")
    return arguments


def _bounded_text(value: Any, *, minimum: int, maximum: int) -> str:
    if type(value) is not str:
        raise ToolFailure("invalid_arguments")
    stripped = value.strip()
    if not minimum <= len(stripped) <= maximum:
        raise ToolFailure("invalid_arguments")
    return stripped


class ToolRuntime:
    """Trusted router: parse, authorize, validate, execute, bound output and audit."""

    def __init__(self, runtime_db: Path | str, provider_db: Path | str,
                 principal: Principal, *, now_fn: Callable[[], int] | None = None,
                 lose_next_receipt: bool = False):
        self.runtime_db = Path(runtime_db)
        self.provider_db = Path(provider_db)
        self.principal = principal
        self.now_fn = now_fn or (lambda: int(time.time()))
        self.lose_next_receipt = lose_next_receipt
        initialize(self.runtime_db, self.provider_db)
        self._handlers = {
            "tickets.list_own": self._list_own,
            "tickets.preview": self._preview,
            "tickets.revise": self._revise,
            "tickets.submit": self._submit,
            "tickets.reconcile": self._reconcile,
        }

    @property
    def now(self) -> int:
        return int(self.now_fn())

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.runtime_db)
        db.row_factory = sqlite3.Row
        return db

    def _envelope(self, call_id: str | None, tool: str | None, *, status: str,
                  code: str, data: dict[str, Any] | None = None,
                  retryable: bool = False, truncated: bool = False) -> dict[str, Any]:
        return {
            "call_id": call_id,
            "tool": tool,
            "status": status,
            "code": code,
            "retryable": retryable,
            "truncated": truncated,
            "data": data,
        }

    def _audit(self, result: dict[str, Any]) -> None:
        data = result.get("data") if type(result.get("data")) is dict else {}
        call_id = result.get("call_id")
        tool = result.get("tool")
        with self._connect() as db:
            db.execute(
                """INSERT INTO audit_events(
                       at, tenant_id, user_id, call_id, tool_name, status, code,
                       draft_id, operation_id
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (self.now, self.principal.tenant_id, self.principal.user_id,
                 call_id[:MAX_AUDIT_TEXT] if type(call_id) is str else None,
                 tool[:MAX_AUDIT_TEXT] if type(tool) is str else None,
                 result["status"], result["code"], data.get("draft_id"),
                 data.get("operation_id")),
            )

    def _bound_list_result(self, result: dict[str, Any]) -> dict[str, Any]:
        items = result.get("data", {}).get("items")
        if type(items) is not list:
            return result
        if len(canonical_json(result).encode("utf-8")) <= MAX_RESULT_BYTES:
            return result
        while len(canonical_json(result).encode("utf-8")) > MAX_RESULT_BYTES and items:
            items.pop()
        result["status"] = "partial"
        result["code"] = "result_truncated"
        result["truncated"] = True
        result["data"]["returned_count"] = len(items)
        result["data"]["has_more"] = len(items) < result["data"]["total_count"]
        while len(canonical_json(result).encode("utf-8")) > MAX_RESULT_BYTES and items:
            items.pop()
            result["data"]["returned_count"] = len(items)
            result["data"]["has_more"] = len(items) < result["data"]["total_count"]
        return result

    def _bound_result(self, result: dict[str, Any]) -> dict[str, Any]:
        result = self._bound_list_result(result)
        if len(canonical_json(result).encode("utf-8")) <= MAX_RESULT_BYTES:
            return result
        original = result.get("data") if type(result.get("data")) is dict else {}
        summary: dict[str, Any] = {"full_result_hash": payload_hash(original)}
        for key in ("draft_id", "version", "payload_hash", "operation_id",
                    "requires_trusted_ui_approval", "requires_new_approval",
                    "next_action"):
            if key in original and type(original[key]) in (str, int, bool):
                summary[key] = original[key]
        preview = original.get("preview")
        if type(preview) is dict:
            summary["preview_summary"] = {
                "title": preview.get("title"),
                "priority": preview.get("priority"),
                "description_chars": len(preview.get("description", "")),
            }
        result["data"] = summary
        result["status"] = "partial"
        result["code"] = "result_truncated"
        result["truncated"] = True
        return result

    def execute_call(self, raw: str) -> dict[str, Any]:
        call_id: str | None = None
        tool: str | None = None
        try:
            call = parse_call(raw)
            if set(call) != {"call_id", "name", "arguments"}:
                raise ToolFailure("invalid_call_envelope")
            call_id, tool = call["call_id"], call["name"]
            if type(call_id) is not str or CALL_ID_RE.fullmatch(call_id) is None:
                raise ToolFailure("invalid_call_id")
            if type(tool) is not str or tool not in self._handlers:
                raise ToolFailure("tool_not_allowed")
            if tool not in self.principal.allowed_tools:
                raise ToolFailure("tool_not_allowed")
            status, code, data = self._handlers[tool](call["arguments"])
            result = self._envelope(call_id, tool, status=status, code=code, data=data)
            result = self._bound_result(result)
        except ToolFailure as exc:
            result = self._envelope(call_id, tool, status="error", code=exc.code,
                                    retryable=exc.retryable)
        except Exception:
            # The model receives a stable error category, never a stack trace or secret.
            result = self._envelope(call_id, tool, status="error", code="internal_error",
                                    retryable=True)
        self._audit(result)
        return result

    def _validated_payload(self, arguments: Any, *, include_identity: bool = False) -> dict[str, str]:
        fields = {"title", "description", "priority"}
        args = _require_fields(arguments, fields)
        priority = args["priority"]
        if type(priority) is not str or priority not in {"low", "normal", "high"}:
            raise ToolFailure("invalid_arguments")
        return {
            "title": _bounded_text(args["title"], minimum=3, maximum=120),
            "description": _bounded_text(args["description"], minimum=3, maximum=2_000),
            "priority": priority,
        }

    def _list_own(self, arguments: Any) -> tuple[str, str, dict[str, Any]]:
        args = _require_fields(arguments, {"limit"})
        limit = args["limit"]
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ToolFailure("invalid_arguments")
        with sqlite3.connect(self.provider_db) as db:
            db.row_factory = sqlite3.Row
            total = db.execute(
                "SELECT COUNT(*) FROM tickets WHERE tenant_id = ? AND user_id = ?",
                (self.principal.tenant_id, self.principal.user_id),
            ).fetchone()[0]
            rows = db.execute(
                """SELECT receipt_id, title, priority, created_at
                   FROM tickets WHERE tenant_id = ? AND user_id = ?
                   ORDER BY created_at DESC, receipt_id LIMIT ?""",
                (self.principal.tenant_id, self.principal.user_id, limit),
            ).fetchall()
        items = [dict(row) for row in rows]
        return "ok", "list_complete", {
            "items": items,
            "returned_count": len(items),
            "total_count": total,
            "has_more": len(items) < total,
        }

    def _preview(self, arguments: Any) -> tuple[str, str, dict[str, Any]]:
        payload = self._validated_payload(arguments)
        digest = payload_hash(payload)
        with self._connect() as db:
            cursor = db.execute(
                """INSERT INTO drafts(
                       tenant_id, user_id, version, payload_json, payload_hash,
                       created_at, updated_at
                   ) VALUES (?, ?, 1, ?, ?, ?, ?)""",
                (self.principal.tenant_id, self.principal.user_id,
                 canonical_json(payload), digest, self.now, self.now),
            )
            draft_id = f"draft-{cursor.lastrowid:04d}"
            db.execute("UPDATE drafts SET draft_id = ? WHERE id = ?",
                       (draft_id, cursor.lastrowid))
        return "ok", "preview_created", {
            "draft_id": draft_id,
            "version": 1,
            "payload_hash": digest,
            "preview": payload,
            "requires_trusted_ui_approval": True,
        }

    def _get_owned_draft(self, db: sqlite3.Connection, draft_id: Any) -> sqlite3.Row:
        if type(draft_id) is not str or len(draft_id) > 80:
            raise ToolFailure("invalid_arguments")
        row = db.execute(
            "SELECT * FROM drafts WHERE draft_id = ? AND tenant_id = ? AND user_id = ?",
            (draft_id, self.principal.tenant_id, self.principal.user_id),
        ).fetchone()
        if row is None:
            raise ToolFailure("draft_not_found")
        return row

    def _revise(self, arguments: Any) -> tuple[str, str, dict[str, Any]]:
        args = _require_fields(
            arguments,
            {"draft_id", "expected_version", "title", "description", "priority"},
        )
        expected_version = args["expected_version"]
        if type(expected_version) is not int or expected_version < 1:
            raise ToolFailure("invalid_arguments")
        payload = self._validated_payload({key: args[key] for key in
                                           ("title", "description", "priority")})
        digest = payload_hash(payload)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self._get_owned_draft(db, args["draft_id"])
            if row["version"] != expected_version:
                raise ToolFailure("stale_draft_version")
            action = db.execute(
                "SELECT status FROM actions WHERE draft_id = ?", (row["draft_id"],)
            ).fetchone()
            if action is not None:
                raise ToolFailure("draft_execution_started")
            new_version = expected_version + 1
            db.execute(
                """UPDATE drafts SET version = ?, payload_json = ?, payload_hash = ?,
                   updated_at = ? WHERE draft_id = ?""",
                (new_version, canonical_json(payload), digest, self.now, row["draft_id"]),
            )
        return "ok", "preview_revised", {
            "draft_id": row["draft_id"],
            "version": new_version,
            "payload_hash": digest,
            "preview": payload,
            "requires_new_approval": True,
        }

    def approve_from_ui(self, *, draft_id: str, expected_version: int,
                        expected_hash: str, action: str,
                        expires_at: int) -> dict[str, Any]:
        """Record a trusted UI decision. This method is deliberately not a model tool."""
        if action != "tickets.submit":
            raise ToolFailure("approval_action_not_allowed")
        if type(expires_at) is not int or expires_at <= self.now:
            raise ToolFailure("approval_expired")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self._get_owned_draft(db, draft_id)
            if row["version"] != expected_version or row["payload_hash"] != expected_hash:
                raise ToolFailure("stale_approval_target")
            binding = {
                "tenant_id": self.principal.tenant_id,
                "user_id": self.principal.user_id,
                "draft_id": draft_id,
                "version": expected_version,
                "payload_hash": expected_hash,
                "action": action,
                "expires_at": expires_at,
            }
            approval_id = "approval-" + payload_hash(binding)[:16]
            operation_binding = {key: binding[key] for key in
                                 ("tenant_id", "user_id", "draft_id", "version",
                                  "payload_hash", "action")}
            operation_id = "op-" + payload_hash(operation_binding)[:24]
            db.execute(
                """INSERT OR IGNORE INTO approvals(
                       approval_id, operation_id, draft_id, tenant_id, user_id,
                       action, draft_version, payload_hash, expires_at, created_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (approval_id, operation_id, draft_id, self.principal.tenant_id,
                 self.principal.user_id, action, expected_version, expected_hash,
                 expires_at, self.now),
            )
        return {
            "approval_id": approval_id,
            "operation_id": operation_id,
            "draft_id": draft_id,
            "draft_version": expected_version,
            "payload_hash": expected_hash,
            "action": action,
            "expires_at": expires_at,
        }

    def _load_submission(self, db: sqlite3.Connection, arguments: Any, *,
                         require_unexpired: bool = True
                         ) -> tuple[sqlite3.Row, sqlite3.Row, dict[str, Any]]:
        args = _require_fields(arguments, {"draft_id", "approval_id"})
        draft = self._get_owned_draft(db, args["draft_id"])
        approval_id = args["approval_id"]
        if type(approval_id) is not str or len(approval_id) > 100:
            raise ToolFailure("invalid_arguments")
        approval = db.execute(
            """SELECT * FROM approvals WHERE approval_id = ? AND draft_id = ?
               AND tenant_id = ? AND user_id = ?""",
            (approval_id, draft["draft_id"], self.principal.tenant_id,
             self.principal.user_id),
        ).fetchone()
        if approval is None:
            raise ToolFailure("approval_not_found")
        if approval["action"] != "tickets.submit":
            raise ToolFailure("approval_action_mismatch")
        if require_unexpired and self.now >= approval["expires_at"]:
            raise ToolFailure("approval_expired")
        if (draft["version"] != approval["draft_version"]
                or draft["payload_hash"] != approval["payload_hash"]):
            raise ToolFailure("approval_stale")
        return draft, approval, json.loads(draft["payload_json"])

    def _submit(self, arguments: Any) -> tuple[str, str, dict[str, Any]]:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            draft, approval, payload = self._load_submission(db, arguments)
            action = db.execute(
                "SELECT * FROM actions WHERE operation_id = ?", (approval["operation_id"],)
            ).fetchone()
            if action is not None:
                if action["payload_hash"] != draft["payload_hash"]:
                    raise ToolFailure("operation_payload_conflict")
                if action["status"] == "SUCCEEDED":
                    return "ok", "already_submitted", {
                        "draft_id": draft["draft_id"],
                        "operation_id": action["operation_id"],
                        "receipt": json.loads(action["receipt_json"]),
                    }
                if action["status"] in {"DISPATCHING", "UNKNOWN"}:
                    return "unknown", "reconciliation_required", {
                        "draft_id": draft["draft_id"],
                        "operation_id": action["operation_id"],
                    }
                raise ToolFailure("submission_blocked")
            db.execute(
                """INSERT INTO actions(
                       operation_id, approval_id, draft_id, tenant_id, user_id,
                       payload_hash, status, updated_at
                   ) VALUES (?, ?, ?, ?, ?, ?, 'DISPATCHING', ?)""",
                (approval["operation_id"], approval["approval_id"], draft["draft_id"],
                 self.principal.tenant_id, self.principal.user_id,
                 draft["payload_hash"], self.now),
            )
        lose_receipt = self.lose_next_receipt
        self.lose_next_receipt = False
        try:
            receipt = provider_submit(
                self.provider_db,
                operation_id=approval["operation_id"],
                principal=self.principal,
                payload=payload,
                expected_hash=draft["payload_hash"],
                now=self.now,
                lose_receipt=lose_receipt,
            )
        except ReceiptLost:
            with self._connect() as db:
                db.execute(
                    "UPDATE actions SET status = 'UNKNOWN', updated_at = ? WHERE operation_id = ?",
                    (self.now, approval["operation_id"]),
                )
            return "unknown", "outcome_unknown", {
                "draft_id": draft["draft_id"],
                "operation_id": approval["operation_id"],
                "next_action": "tickets.reconcile",
            }
        except ProviderConflict:
            with self._connect() as db:
                db.execute(
                    "UPDATE actions SET status = 'BLOCKED', updated_at = ? WHERE operation_id = ?",
                    (self.now, approval["operation_id"]),
                )
            raise ToolFailure("operation_payload_conflict")
        with self._connect() as db:
            db.execute(
                """UPDATE actions SET status = 'SUCCEEDED', receipt_json = ?, updated_at = ?
                   WHERE operation_id = ?""",
                (canonical_json(receipt), self.now, approval["operation_id"]),
            )
        return "ok", "submitted", {
            "draft_id": draft["draft_id"],
            "operation_id": approval["operation_id"],
            "receipt": receipt,
        }

    def _reconcile(self, arguments: Any) -> tuple[str, str, dict[str, Any]]:
        with self._connect() as db:
            # Reconciliation is a read/receipt repair for an already-dispatched action.
            # Its original authorization is preserved even if approval expires later.
            draft, approval, _ = self._load_submission(
                db, arguments, require_unexpired=False
            )
            action = db.execute(
                "SELECT * FROM actions WHERE operation_id = ?", (approval["operation_id"],)
            ).fetchone()
        if action is None:
            raise ToolFailure("submission_not_started")
        if action["status"] == "SUCCEEDED":
            return "ok", "already_reconciled", {
                "draft_id": draft["draft_id"],
                "operation_id": action["operation_id"],
                "receipt": json.loads(action["receipt_json"]),
            }
        if action["status"] not in {"DISPATCHING", "UNKNOWN"}:
            raise ToolFailure("submission_blocked")
        receipt = provider_lookup(self.provider_db, action["operation_id"])
        if receipt is None:
            with self._connect() as db:
                db.execute(
                    "UPDATE actions SET status = 'UNKNOWN', updated_at = ? WHERE operation_id = ?",
                    (self.now, action["operation_id"]),
                )
            return "unknown", "provider_receipt_not_found", {
                "draft_id": draft["draft_id"],
                "operation_id": action["operation_id"],
            }
        if receipt["payload_hash"] != action["payload_hash"]:
            raise ToolFailure("operation_payload_conflict")
        with self._connect() as db:
            db.execute(
                """UPDATE actions SET status = 'SUCCEEDED', receipt_json = ?, updated_at = ?
                   WHERE operation_id = ?""",
                (canonical_json(receipt), self.now, action["operation_id"]),
            )
        return "ok", "reconciled", {
            "draft_id": draft["draft_id"],
            "operation_id": action["operation_id"],
            "receipt": receipt,
        }


def make_call(call_id: str, name: str, arguments: dict[str, Any]) -> str:
    return canonical_json({"call_id": call_id, "name": name, "arguments": arguments})


def _draft(runtime: ToolRuntime, suffix: str = "") -> dict[str, Any]:
    result = runtime.execute_call(make_call(
        "preview" + suffix.replace(" ", "-"),
        "tickets.preview",
        {"title": "VPN 反复断线" + suffix,
         "description": "客户端显示 TLS 握手超时，请协助排查。",
         "priority": "normal"},
    ))
    assert result["status"] == "ok"
    return result["data"]


def run_scenario(name: str) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="tool-calling-lab-") as directory:
        runtime_db = Path(directory) / "runtime.db"
        provider_db = Path(directory) / "provider.db"
        clock = [1_000]
        runtime = ToolRuntime(runtime_db, provider_db, default_principal(),
                              now_fn=lambda: clock[0],
                              lose_next_receipt=name == "unknown_reconcile")
        if name == "approved_submit":
            draft = _draft(runtime)
            approval = runtime.approve_from_ui(
                draft_id=draft["draft_id"], expected_version=draft["version"],
                expected_hash=draft["payload_hash"], action="tickets.submit",
                expires_at=1_300,
            )
            result = runtime.execute_call(make_call(
                "submit", "tickets.submit",
                {"draft_id": draft["draft_id"], "approval_id": approval["approval_id"]},
            ))
            return {"scenario": name, "status": result["status"], "code": result["code"],
                    **provider_stats(provider_db)}
        if name == "invalid_and_overreach":
            duplicate = runtime.execute_call(
                '{"call_id":"x","name":"tickets.list_own","arguments":{"limit":5,"limit":7}}'
            )
            identity = runtime.execute_call(make_call(
                "identity", "tickets.list_own", {"limit": 5, "tenant_id": "other"}
            ))
            forbidden = runtime.execute_call(make_call("admin", "tickets.delete_all", {}))
            return {"scenario": name, "duplicate_json": duplicate["code"],
                    "identity_injection": identity["code"], "unknown_tool": forbidden["code"]}
        if name == "stale_and_expired_approval":
            first = _draft(runtime, " A")
            first_approval = runtime.approve_from_ui(
                draft_id=first["draft_id"], expected_version=1,
                expected_hash=first["payload_hash"], action="tickets.submit", expires_at=1_300,
            )
            revised = runtime.execute_call(make_call(
                "revise", "tickets.revise",
                {"draft_id": first["draft_id"], "expected_version": 1,
                 "title": "VPN 反复断线 B", "description": "补充：只在公司网络发生。",
                 "priority": "high"},
            ))
            stale = runtime.execute_call(make_call(
                "stale", "tickets.submit",
                {"draft_id": first["draft_id"], "approval_id": first_approval["approval_id"]},
            ))
            second = _draft(runtime, " C")
            expired_approval = runtime.approve_from_ui(
                draft_id=second["draft_id"], expected_version=1,
                expected_hash=second["payload_hash"], action="tickets.submit", expires_at=1_001,
            )
            clock[0] = 1_001
            expired = runtime.execute_call(make_call(
                "expired", "tickets.submit",
                {"draft_id": second["draft_id"], "approval_id": expired_approval["approval_id"]},
            ))
            return {"scenario": name, "revision": revised["code"],
                    "changed_content": stale["code"], "expired": expired["code"],
                    **provider_stats(provider_db)}
        if name == "duplicate_submit":
            draft = _draft(runtime)
            approval = runtime.approve_from_ui(
                draft_id=draft["draft_id"], expected_version=1,
                expected_hash=draft["payload_hash"], action="tickets.submit", expires_at=1_300,
            )
            args = {"draft_id": draft["draft_id"], "approval_id": approval["approval_id"]}
            first = runtime.execute_call(make_call("submit-1", "tickets.submit", args))
            second = runtime.execute_call(make_call("submit-2", "tickets.submit", args))
            return {"scenario": name, "first": first["code"], "second": second["code"],
                    "same_receipt": first["data"]["receipt"]["receipt_id"]
                    == second["data"]["receipt"]["receipt_id"],
                    **provider_stats(provider_db)}
        if name == "unknown_reconcile":
            draft = _draft(runtime)
            approval = runtime.approve_from_ui(
                draft_id=draft["draft_id"], expected_version=1,
                expected_hash=draft["payload_hash"], action="tickets.submit", expires_at=1_300,
            )
            args = {"draft_id": draft["draft_id"], "approval_id": approval["approval_id"]}
            unknown = runtime.execute_call(make_call("submit-1", "tickets.submit", args))
            blocked_retry = runtime.execute_call(make_call("submit-2", "tickets.submit", args))
            reconciled = runtime.execute_call(make_call("reconcile", "tickets.reconcile", args))
            return {"scenario": name, "first": unknown["code"],
                    "blind_retry": blocked_retry["code"], "verify": reconciled["code"],
                    **provider_stats(provider_db)}
        raise ValueError(f"unknown scenario: {name}")


SCENARIOS = (
    "approved_submit",
    "invalid_and_overreach",
    "stale_and_expired_approval",
    "duplicate_submit",
    "unknown_reconcile",
)


def main() -> None:
    parser = argparse.ArgumentParser(description="Chapter 14 offline tool-calling lab")
    parser.add_argument("command", choices=("scenarios",))
    parser.add_argument("scenario", choices=(*SCENARIOS, "all"))
    args = parser.parse_args()
    names = SCENARIOS if args.scenario == "all" else (args.scenario,)
    for name in names:
        print(canonical_json(run_scenario(name)))


if __name__ == "__main__":
    main()
