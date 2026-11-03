"""Chapter 26 reliability lab: Outbox, idempotency, and reconciliation.

Python 3.11+ standard library only. ``runtime.db`` and ``provider.db`` are
deliberately independent transaction boundaries. The injected crash exception
stands in for process loss only after a committed boundary; tests then reopen
both stores before recovery.
"""
from __future__ import annotations

import argparse
from contextlib import closing, contextmanager
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import tempfile
from typing import Any, Iterator


class Conflict(Exception):
    """A stable key or event was reused with different content."""


class Rejected(Exception):
    """Input or state does not satisfy the teaching contract."""


class InjectedFailure(Exception):
    """A failure injected inside one local transaction."""


class SimulatedCrash(Exception):
    """The process disappeared after the preceding transaction committed."""


@dataclass(frozen=True)
class Scope:
    """Server-trusted operation scope; not a client supplied identity object."""

    tenant_id: str
    requester_id: str
    action: str = "ticket.create"


TRUSTED_SCOPE = Scope("tenant-acme", "employee-alice")
_SAFE_ID = re.compile(r"^[A-Za-z0-9._:-]{1,80}$")
_CATEGORIES = {"account", "hardware", "vpn"}


def encode(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def fingerprint(value: Any) -> str:
    return hashlib.sha256(encode(value).encode("utf-8")).hexdigest()


def canonical_payload(payload: dict[str, Any]) -> dict[str, str]:
    if not isinstance(payload, dict) or set(payload) != {"title", "category"}:
        raise Rejected("payload must contain exactly title and category")
    if not isinstance(payload["title"], str) or not isinstance(payload["category"], str):
        raise Rejected("title and category must be strings")
    title = " ".join(payload["title"].split())
    category = payload["category"].strip().lower()
    if not 1 <= len(title) <= 120:
        raise Rejected("title must contain 1 to 120 characters")
    if category not in _CATEGORIES:
        raise Rejected("category must be account, hardware, or vpn")
    return {"category": category, "title": title}


def _validate_scope(scope: Scope) -> None:
    if not isinstance(scope, Scope):
        raise Rejected("scope must come from trusted service code")
    if not _SAFE_ID.fullmatch(scope.tenant_id) or not _SAFE_ID.fullmatch(scope.requester_id):
        raise Rejected("invalid trusted scope identifier")
    if scope.action != "ticket.create":
        raise Rejected("this lab only permits ticket.create")


def _validate_client_key(client_key: str) -> None:
    if not isinstance(client_key, str) or not _SAFE_ID.fullmatch(client_key):
        raise Rejected("client key must be a stable 1 to 80 character identifier")


def _operation_identity(scope: Scope, client_key: str) -> tuple[str, str, str]:
    identity = {
        "action": scope.action,
        "client_key": client_key,
        "requester_id": scope.requester_id,
        "tenant_id": scope.tenant_id,
    }
    digest = fingerprint(identity)
    return "operation_" + digest[:24], "op_" + digest, "event_" + digest[:24]


@contextmanager
def transaction(path: Path) -> Iterator[sqlite3.Connection]:
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        connection.execute("BEGIN IMMEDIATE")
        yield connection
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


@contextmanager
def read_only(path: Path) -> Iterator[sqlite3.Connection]:
    uri = path.resolve().as_uri() + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    try:
        yield connection
    finally:
        connection.close()


class RuntimeStore:
    """Owns local operation intent, Outbox state, and recovered receipts."""

    def __init__(self, path: Path):
        self.path = Path(path)

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("PRAGMA foreign_keys = ON")
            with db:
                db.executescript(
                    """
                CREATE TABLE IF NOT EXISTS operations (
                    operation_id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    requester_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    client_key TEXT NOT NULL,
                    operation_key TEXT NOT NULL UNIQUE,
                    payload_json TEXT NOT NULL,
                    payload_fp TEXT NOT NULL,
                    state TEXT NOT NULL CHECK (
                        state IN ('PREPARED','DISPATCHING','UNKNOWN','SUCCEEDED')
                    ),
                    review_required INTEGER NOT NULL DEFAULT 0 CHECK (review_required IN (0,1)),
                    receipt_json TEXT,
                    UNIQUE (tenant_id, requester_id, action, client_key)
                );
                CREATE TABLE IF NOT EXISTS outbox (
                    event_id TEXT PRIMARY KEY,
                    operation_id TEXT NOT NULL UNIQUE REFERENCES operations(operation_id),
                    message_json TEXT NOT NULL,
                    message_fp TEXT NOT NULL,
                    state TEXT NOT NULL CHECK (
                        state IN ('PENDING','SENDING','UNKNOWN','DELIVERED')
                    ),
                    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0)
                );
                """
                )

    def prepare(
        self,
        scope: Scope,
        client_key: str,
        payload: dict[str, Any],
        *,
        fault: str | None = None,
    ) -> tuple[str, bool]:
        """Atomically save one operation and its Outbox message."""
        _validate_scope(scope)
        _validate_client_key(client_key)
        normalized = canonical_payload(payload)
        payload_fp = fingerprint(normalized)
        operation_id, operation_key, event_id = _operation_identity(scope, client_key)
        with transaction(self.path) as db:
            existing = db.execute(
                """SELECT operation_id, payload_fp FROM operations
                   WHERE tenant_id=? AND requester_id=? AND action=? AND client_key=?""",
                (scope.tenant_id, scope.requester_id, scope.action, client_key),
            ).fetchone()
            if existing:
                if existing["payload_fp"] != payload_fp:
                    raise Conflict("operation key reused with different payload")
                return existing["operation_id"], False

            db.execute(
                """INSERT INTO operations
                   (operation_id, tenant_id, requester_id, action, client_key,
                    operation_key, payload_json, payload_fp, state,
                    review_required, receipt_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'PREPARED', 0, NULL)""",
                (
                    operation_id,
                    scope.tenant_id,
                    scope.requester_id,
                    scope.action,
                    client_key,
                    operation_key,
                    encode(normalized),
                    payload_fp,
                ),
            )
            if fault == "after_operation_insert":
                raise InjectedFailure("failure before the Outbox insert")
            if fault is not None:
                raise Rejected("unknown prepare fault")

            message = {
                "action": scope.action,
                "event_id": event_id,
                "operation_key": operation_key,
                "payload": normalized,
                "payload_fp": payload_fp,
                "requester_id": scope.requester_id,
                "schema_version": 1,
                "tenant_id": scope.tenant_id,
            }
            db.execute(
                """INSERT INTO outbox
                   (event_id, operation_id, message_json, message_fp, state, attempts)
                   VALUES (?, ?, ?, ?, 'PENDING', 0)""",
                (event_id, operation_id, encode(message), fingerprint(message)),
            )
        return operation_id, True

    def snapshot(self, operation_id: str) -> dict[str, Any]:
        with read_only(self.path) as db:
            row = db.execute(
                """SELECT o.*, x.event_id, x.message_json, x.message_fp,
                          x.state AS outbox_state, x.attempts
                   FROM operations o JOIN outbox x USING(operation_id)
                   WHERE operation_id=?""",
                (operation_id,),
            ).fetchone()
        if row is None:
            raise Rejected("unknown operation")
        result = dict(row)
        result["payload"] = json.loads(result.pop("payload_json"))
        result["message"] = json.loads(result.pop("message_json"))
        result["receipt"] = (
            json.loads(result["receipt_json"]) if result["receipt_json"] else None
        )
        return result

    def counts(self) -> dict[str, int]:
        with read_only(self.path) as db:
            return {
                "operations": db.execute("SELECT COUNT(*) FROM operations").fetchone()[0],
                "outbox": db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0],
            }

    def begin_dispatch(self, operation_id: str) -> dict[str, Any]:
        with transaction(self.path) as db:
            row = db.execute(
                """SELECT o.state AS operation_state, x.state AS outbox_state,
                          x.message_json
                   FROM operations o JOIN outbox x USING(operation_id)
                   WHERE operation_id=?""",
                (operation_id,),
            ).fetchone()
            if row is None:
                raise Rejected("unknown operation")
            if (row["operation_state"], row["outbox_state"]) != ("PREPARED", "PENDING"):
                raise Rejected("only a prepared operation can be dispatched")
            db.execute(
                "UPDATE operations SET state='DISPATCHING' WHERE operation_id=?",
                (operation_id,),
            )
            db.execute(
                """UPDATE outbox SET state='SENDING', attempts=attempts+1
                   WHERE operation_id=?""",
                (operation_id,),
            )
            return json.loads(row["message_json"])

    def mark_unknown(self, operation_id: str) -> None:
        with transaction(self.path) as db:
            row = db.execute(
                "SELECT state FROM operations WHERE operation_id=?", (operation_id,)
            ).fetchone()
            if row is None:
                raise Rejected("unknown operation")
            if row["state"] == "DISPATCHING":
                db.execute(
                    "UPDATE operations SET state='UNKNOWN' WHERE operation_id=?",
                    (operation_id,),
                )
                db.execute(
                    "UPDATE outbox SET state='UNKNOWN' WHERE operation_id=?",
                    (operation_id,),
                )
            elif row["state"] != "UNKNOWN":
                raise Rejected("operation is not at an ambiguous dispatch boundary")

    @staticmethod
    def _ticket_matches(operation: dict[str, Any], ticket: dict[str, Any]) -> bool:
        expected = {
            "action": operation["action"],
            "operation_key": operation["operation_key"],
            "payload_fp": operation["payload_fp"],
            "requester_id": operation["requester_id"],
            "tenant_id": operation["tenant_id"],
        }
        return ticket.get("status") == "OPEN" and all(
            ticket.get(key) == value for key, value in expected.items()
        )

    def confirm(self, operation_id: str, ticket: dict[str, Any]) -> None:
        with transaction(self.path) as db:
            row = db.execute(
                "SELECT * FROM operations WHERE operation_id=?", (operation_id,)
            ).fetchone()
            if row is None:
                raise Rejected("unknown operation")
            operation = dict(row)
            if operation["state"] not in {"DISPATCHING", "UNKNOWN"}:
                raise Rejected("operation is not awaiting a receipt")
            if not self._ticket_matches(operation, ticket):
                raise Conflict("provider result does not match operation identity and payload")
            db.execute(
                """UPDATE operations SET state='SUCCEEDED', review_required=0,
                          receipt_json=?
                   WHERE operation_id=?""",
                (encode(ticket), operation_id),
            )
            db.execute(
                "UPDATE outbox SET state='DELIVERED' WHERE operation_id=?",
                (operation_id,),
            )

    def require_manual_review(self, operation_id: str) -> None:
        with transaction(self.path) as db:
            result = db.execute(
                """UPDATE operations SET review_required=1
                   WHERE operation_id=? AND state='UNKNOWN'""",
                (operation_id,),
            )
            if result.rowcount != 1:
                raise Rejected("only an unknown operation can require manual review")


class ProviderStore:
    """A separate ticket system with atomic Inbox deduplication."""

    def __init__(self, path: Path):
        self.path = Path(path)

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path)) as db:
            with db:
                db.executescript(
                    """
                CREATE TABLE IF NOT EXISTS tickets (
                    ticket_id TEXT PRIMARY KEY,
                    operation_key TEXT NOT NULL UNIQUE,
                    tenant_id TEXT NOT NULL,
                    requester_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    payload_fp TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (status = 'OPEN')
                );
                CREATE TABLE IF NOT EXISTS inbox (
                    event_id TEXT PRIMARY KEY,
                    message_fp TEXT NOT NULL,
                    operation_key TEXT NOT NULL,
                    result_json TEXT NOT NULL
                );
                """
                )

    @staticmethod
    def _validate_message(message: dict[str, Any]) -> dict[str, Any]:
        required = {
            "action",
            "event_id",
            "operation_key",
            "payload",
            "payload_fp",
            "requester_id",
            "schema_version",
            "tenant_id",
        }
        if not isinstance(message, dict) or set(message) != required:
            raise Rejected("invalid Outbox message schema")
        if message["schema_version"] != 1 or message["action"] != "ticket.create":
            raise Rejected("unsupported Outbox message")
        for field in ("event_id", "operation_key", "requester_id", "tenant_id"):
            value = message[field]
            if not isinstance(value, str) or not value:
                raise Rejected(f"invalid {field}")
        payload = canonical_payload(message["payload"])
        if fingerprint(payload) != message["payload_fp"]:
            raise Conflict("payload fingerprint does not match message content")
        return {**message, "payload": payload}

    def consume(
        self,
        message: dict[str, Any],
        *,
        fault: str | None = None,
        crash_after_commit: bool = False,
    ) -> dict[str, Any]:
        """Commit Inbox receipt and ticket in one provider-local transaction."""
        message = self._validate_message(message)
        message_fp = fingerprint(message)
        with transaction(self.path) as db:
            seen = db.execute(
                "SELECT * FROM inbox WHERE event_id=?", (message["event_id"],)
            ).fetchone()
            if seen:
                if seen["message_fp"] != message_fp:
                    raise Conflict("event id reused with different message")
                result = json.loads(seen["result_json"])
            else:
                ticket = db.execute(
                    "SELECT * FROM tickets WHERE operation_key=?",
                    (message["operation_key"],),
                ).fetchone()
                if ticket:
                    result = dict(ticket)
                    result.pop("payload_json")
                    expected = {
                        "action": message["action"],
                        "payload_fp": message["payload_fp"],
                        "requester_id": message["requester_id"],
                        "tenant_id": message["tenant_id"],
                    }
                    if any(result[key] != value for key, value in expected.items()):
                        raise Conflict("operation key reused with different scope or payload")
                else:
                    ticket_id = "ticket_" + hashlib.sha256(
                        message["operation_key"].encode("utf-8")
                    ).hexdigest()[:16]
                    result = {
                        "action": message["action"],
                        "operation_key": message["operation_key"],
                        "payload_fp": message["payload_fp"],
                        "requester_id": message["requester_id"],
                        "status": "OPEN",
                        "tenant_id": message["tenant_id"],
                        "ticket_id": ticket_id,
                    }
                    db.execute(
                        """INSERT INTO tickets
                           (ticket_id, operation_key, tenant_id, requester_id, action,
                            payload_fp, payload_json, status)
                           VALUES (?, ?, ?, ?, ?, ?, ?, 'OPEN')""",
                        (
                            ticket_id,
                            message["operation_key"],
                            message["tenant_id"],
                            message["requester_id"],
                            message["action"],
                            message["payload_fp"],
                            encode(message["payload"]),
                        ),
                    )
                    if fault == "after_ticket_insert":
                        raise InjectedFailure("failure before Inbox receipt")
                if fault not in {None, "after_ticket_insert"}:
                    raise Rejected("unknown provider fault")
                db.execute(
                    "INSERT INTO inbox VALUES (?, ?, ?, ?)",
                    (
                        message["event_id"],
                        message_fp,
                        message["operation_key"],
                        encode(result),
                    ),
                )
        if crash_after_commit:
            raise SimulatedCrash("provider committed, but its response was lost")
        return result

    def lookup(self, operation_key: str) -> dict[str, Any] | None:
        """Read only: reconciliation never creates or updates provider data."""
        with read_only(self.path) as db:
            row = db.execute(
                "SELECT * FROM tickets WHERE operation_key=?", (operation_key,)
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result.pop("payload_json")
        return result

    def counts(self) -> dict[str, int]:
        with read_only(self.path) as db:
            return {
                "tickets": db.execute("SELECT COUNT(*) FROM tickets").fetchone()[0],
                "inbox": db.execute("SELECT COUNT(*) FROM inbox").fetchone()[0],
            }


def dispatch(
    runtime: RuntimeStore,
    provider: ProviderStore,
    operation_id: str,
    *,
    crash_at: str | None = None,
) -> str:
    """Dispatch once. An ambiguous restart path must use ``reconcile``."""
    message = runtime.begin_dispatch(operation_id)
    if crash_at == "before_provider":
        raise SimulatedCrash("dispatch intent committed before provider call")
    if crash_at not in {None, "after_provider_commit"}:
        raise Rejected("unknown dispatch crash point")
    ticket = provider.consume(
        message, crash_after_commit=crash_at == "after_provider_commit"
    )
    runtime.confirm(operation_id, ticket)
    return "SUCCEEDED"


def reconcile(runtime: RuntimeStore, provider: ProviderStore, operation_id: str) -> str:
    """Recover an ambiguous dispatch by provider lookup only, never by create."""
    state = runtime.snapshot(operation_id)
    if state["state"] == "SUCCEEDED":
        return "SUCCEEDED"
    if state["review_required"]:
        return "MANUAL_REVIEW"
    if state["state"] == "DISPATCHING":
        runtime.mark_unknown(operation_id)
        state = runtime.snapshot(operation_id)
    if state["state"] != "UNKNOWN":
        raise Rejected("only an ambiguous operation can be reconciled")

    ticket = provider.lookup(state["operation_key"])
    if ticket is None:
        # This provider does not promise that a negative lookup is authoritative.
        # Keep UNKNOWN and do not increment Outbox attempts or call consume again.
        return "STILL_UNKNOWN"
    if not RuntimeStore._ticket_matches(state, ticket):
        runtime.require_manual_review(operation_id)
        return "MANUAL_REVIEW"
    runtime.confirm(operation_id, ticket)
    return "SUCCEEDED"


def demo() -> None:
    with tempfile.TemporaryDirectory(prefix="ch26-reliability-") as directory:
        base = Path(directory)
        runtime_path = base / "runtime.db"
        provider_path = base / "provider.db"
        runtime = RuntimeStore(runtime_path)
        provider = ProviderStore(provider_path)
        runtime.initialize()
        provider.initialize()

        operation_id, created = runtime.prepare(
            TRUSTED_SCOPE,
            "vpn-ticket-001",
            {"title": "VPN 无法连接", "category": "vpn"},
        )
        print("1 operation + outbox:", runtime.counts(), "created=", created)
        try:
            dispatch(runtime, provider, operation_id, crash_at="after_provider_commit")
        except SimulatedCrash:
            pass
        print(
            "2 lost receipt:",
            "local=", runtime.snapshot(operation_id)["state"],
            "provider=", provider.counts(),
        )

        # Reopen both stores to make the durable boundary visible.
        runtime = RuntimeStore(runtime_path)
        provider = ProviderStore(provider_path)
        recovered = reconcile(runtime, provider, operation_id)
        state = runtime.snapshot(operation_id)
        print(
            "3 lookup recovery:",
            "result=", recovered,
            "attempts=", state["attempts"],
            "ticket=", state["receipt"]["ticket_id"],
        )

        second_id, _ = runtime.prepare(
            TRUSTED_SCOPE,
            "vpn-ticket-002",
            {"title": "VPN 间歇断线", "category": "vpn"},
        )
        try:
            dispatch(runtime, provider, second_id, crash_at="before_provider")
        except SimulatedCrash:
            pass
        unresolved = reconcile(RuntimeStore(runtime_path), ProviderStore(provider_path), second_id)
        second = RuntimeStore(runtime_path).snapshot(second_id)
        print(
            "4 negative lookup:",
            "result=", unresolved,
            "state=", second["state"],
            "attempts=", second["attempts"],
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    demo()


if __name__ == "__main__":
    main()
