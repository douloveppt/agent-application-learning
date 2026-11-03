"""Tenant-bound ticket connector lab. Python 3.11+, SQLite, no network.

The runtime ledger and fake provider deliberately use independent databases.
Once dispatch begins, an uncertain result is reconciled by client_reference and
is never replayed as another create call.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
from typing import Mapping


CREATE_OWN = "tickets:create:own"
TERMINAL_STATUSES = frozenset({"succeeded", "rejected"})


class ConnectorError(Exception):
    """Base class for caller-visible connector errors."""


class AuthorizationDenied(ConnectorError):
    pass


class InvalidArguments(ConnectorError):
    pass


class OperationConflict(ConnectorError):
    pass


class ProviderProtocolError(Exception):
    pass


@dataclass(frozen=True)
class RequestContext:
    """Trusted request context populated by the host, not model arguments."""

    tenant_id: str
    user_id: str
    permissions: frozenset[str]


@dataclass(frozen=True)
class OperationResult:
    operation_id: str
    status: str
    ticket_id: str | None = None
    rejection_code: str | None = None
    source: str = "ledger"

    def as_dict(self) -> dict[str, str | None]:
        return {
            "operation_id": self.operation_id,
            "status": self.status,
            "ticket_id": self.ticket_id,
            "rejection_code": self.rejection_code,
            "source": self.source,
        }


@contextmanager
def database(path: Path | str):
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA synchronous = FULL")
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def encode(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def digest(value: object) -> str:
    return hashlib.sha256(encode(value).encode("utf-8")).hexdigest()


def initialize(ledger_db: Path | str, provider_db: Path | str) -> None:
    with database(ledger_db) as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS operations (
                operation_id TEXT PRIMARY KEY,
                connection_tenant TEXT NOT NULL,
                requester_user_id TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                payload_hash TEXT NOT NULL,
                status TEXT NOT NULL CHECK (
                    status IN ('prepared', 'dispatched', 'succeeded', 'rejected', 'unknown')
                ),
                receipt_json TEXT,
                rejection_code TEXT,
                last_error TEXT
            );
            """
        )
    with database(provider_db) as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS tickets (
                row_id INTEGER PRIMARY KEY AUTOINCREMENT,
                client_reference TEXT NOT NULL UNIQUE,
                tenant_id TEXT NOT NULL,
                requester_user_id TEXT NOT NULL,
                title TEXT NOT NULL,
                description TEXT NOT NULL,
                payload_hash TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS create_calls (
                call_id INTEGER PRIMARY KEY AUTOINCREMENT,
                client_reference TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS lookup_calls (
                call_id INTEGER PRIMARY KEY AUTOINCREMENT,
                client_reference TEXT NOT NULL
            );
            """
        )


class FakeProvider:
    """Persistent fake supplier with trusted, test-only fault injection."""

    MODES = frozenset(
        {"success", "business_reject", "commit_then_timeout", "timeout_before_commit", "malformed_receipt"}
    )

    def __init__(self, provider_db: Path | str, mode: str = "success") -> None:
        if mode not in self.MODES:
            raise ValueError(f"unsupported provider mode: {mode}")
        self.provider_db = Path(provider_db)
        self.mode = mode

    @staticmethod
    def _receipt(row: sqlite3.Row) -> dict[str, str]:
        return {
            "outcome": "succeeded",
            "ticket_id": f"TICKET-{row['row_id']:06d}",
            "client_reference": row["client_reference"],
            "tenant_id": row["tenant_id"],
            "requester_user_id": row["requester_user_id"],
            "payload_hash": row["payload_hash"],
        }

    def create_ticket(self, client_reference: str, payload: Mapping[str, str], payload_hash: str) -> dict[str, str]:
        # Count an invocation separately so a provider UNIQUE constraint cannot
        # hide an accidental retry by the connector.
        with database(self.provider_db) as db:
            db.execute("INSERT INTO create_calls (client_reference) VALUES (?)", (client_reference,))

        if self.mode == "business_reject":
            return {
                "outcome": "rejected",
                "code": "QUEUE_CLOSED",
                "message": "The selected support queue is closed",
                "effect": "not_applied",
                "client_reference": client_reference,
                "tenant_id": payload["tenant_id"],
                "requester_user_id": payload["requester_user_id"],
                "payload_hash": payload_hash,
            }
        if self.mode == "timeout_before_commit":
            raise TimeoutError("provider response timed out before a visible commit")

        with database(self.provider_db) as db:
            cursor = db.execute(
                """INSERT INTO tickets (
                    client_reference, tenant_id, requester_user_id, title, description, payload_hash
                ) VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    client_reference,
                    payload["tenant_id"],
                    payload["requester_user_id"],
                    payload["title"],
                    payload["description"],
                    payload_hash,
                ),
            )
            row = db.execute("SELECT * FROM tickets WHERE row_id=?", (cursor.lastrowid,)).fetchone()

        if self.mode == "commit_then_timeout":
            raise TimeoutError("provider committed but its response was lost")
        receipt = self._receipt(row)
        if self.mode == "malformed_receipt":
            receipt["client_reference"] = "wrong-reference"
        return receipt

    def find_by_client_reference(self, client_reference: str) -> dict[str, str] | None:
        with database(self.provider_db) as db:
            db.execute("INSERT INTO lookup_calls (client_reference) VALUES (?)", (client_reference,))
            row = db.execute(
                "SELECT * FROM tickets WHERE client_reference=?", (client_reference,)
            ).fetchone()
        return self._receipt(row) if row else None


class TicketConnector:
    """A single-tenant connector that only creates the current user's ticket."""

    def __init__(
        self,
        ledger_db: Path | str,
        connection_tenant: str,
        provider: FakeProvider,
    ) -> None:
        if not connection_tenant.strip():
            raise ValueError("connection_tenant is required")
        self.ledger_db = Path(ledger_db)
        self.connection_tenant = connection_tenant
        self.provider = provider

    def _authorize(self, context: RequestContext) -> None:
        if context.tenant_id != self.connection_tenant or CREATE_OWN not in context.permissions:
            raise AuthorizationDenied("request is not authorized for this tenant-bound connection")

    @staticmethod
    def _normalize(operation_id: str, context: RequestContext, arguments: Mapping[str, object]) -> tuple[str, dict[str, str], str]:
        if not isinstance(operation_id, str) or not operation_id.strip() or len(operation_id) > 128:
            raise InvalidArguments("operation_id must be a non-empty string of at most 128 characters")
        if not isinstance(arguments, Mapping):
            raise InvalidArguments("arguments must be an object")
        if set(arguments) != {"title", "description"}:
            raise InvalidArguments("only title and description are accepted model arguments")

        raw_title = arguments["title"]
        raw_description = arguments["description"]
        if not isinstance(raw_title, str) or not isinstance(raw_description, str):
            raise InvalidArguments("title and description must be strings")
        if len(raw_title) > 120 or len(raw_description) > 2000:
            raise InvalidArguments("title or description exceeds the connector contract")
        title = raw_title.strip()
        description = raw_description.strip()
        if not title or not description:
            raise InvalidArguments("title and description cannot be blank")

        normalized_id = operation_id.strip()
        payload = {
            "tenant_id": context.tenant_id,
            "requester_user_id": context.user_id,
            "title": title,
            "description": description,
        }
        return normalized_id, payload, digest(payload)

    def submit(
        self,
        context: RequestContext,
        operation_id: str,
        arguments: Mapping[str, object],
    ) -> OperationResult:
        self._authorize(context)
        operation_id, payload, payload_hash = self._normalize(operation_id, context, arguments)

        with database(self.ledger_db) as db:
            row = db.execute(
                "SELECT * FROM operations WHERE operation_id=?", (operation_id,)
            ).fetchone()
            if row is None:
                db.execute(
                    """INSERT INTO operations (
                        operation_id, connection_tenant, requester_user_id,
                        payload_json, payload_hash, status
                    ) VALUES (?, ?, ?, ?, ?, 'prepared')""",
                    (
                        operation_id,
                        self.connection_tenant,
                        context.user_id,
                        encode(payload),
                        payload_hash,
                    ),
                )
                status = "prepared"
            else:
                if row["payload_hash"] != payload_hash:
                    raise OperationConflict("operation_id is already bound to different normalized input")
                status = row["status"]

        if status in TERMINAL_STATUSES:
            return self._stored_result(operation_id)
        if status in {"dispatched", "unknown"}:
            return self._reconcile(operation_id, payload_hash)
        return self._dispatch(operation_id, payload, payload_hash)

    def _dispatch(self, operation_id: str, payload: Mapping[str, str], payload_hash: str) -> OperationResult:
        # Commit the dispatch boundary before crossing into the provider database.
        with database(self.ledger_db) as db:
            updated = db.execute(
                """UPDATE operations SET status='dispatched', last_error=NULL
                WHERE operation_id=? AND status='prepared'""",
                (operation_id,),
            )
            if updated.rowcount != 1:
                raise RuntimeError("operation is not in the prepared state")
        try:
            response = self.provider.create_ticket(operation_id, payload, payload_hash)
            return self._accept_provider_response(operation_id, payload_hash, response)
        except (TimeoutError, ConnectionError, ProviderProtocolError) as exc:
            self._mark_unknown(operation_id, str(exc))
            return OperationResult(operation_id, "unknown", source="provider_error")

    def _accept_provider_response(
        self, operation_id: str, payload_hash: str, response: object
    ) -> OperationResult:
        if not isinstance(response, dict) or response.get("client_reference") != operation_id:
            raise ProviderProtocolError("provider response has the wrong client_reference")
        with database(self.ledger_db) as db:
            operation = db.execute(
                "SELECT requester_user_id FROM operations WHERE operation_id=?", (operation_id,)
            ).fetchone()
        if operation is None:
            raise ProviderProtocolError("provider response refers to an unknown local operation")
        if not (
            response.get("tenant_id") == self.connection_tenant
            and response.get("requester_user_id") == operation["requester_user_id"]
            and response.get("payload_hash") == payload_hash
        ):
            raise ProviderProtocolError("provider response does not match the operation")

        outcome = response.get("outcome")
        if outcome == "rejected":
            code = response.get("code")
            if (
                not isinstance(code, str)
                or not code
                or response.get("effect") != "not_applied"
            ):
                raise ProviderProtocolError(
                    "provider rejection lacks contractual no-effect evidence"
                )
            with database(self.ledger_db) as db:
                db.execute(
                    """UPDATE operations SET status='rejected', receipt_json=?,
                    rejection_code=?, last_error=NULL WHERE operation_id=?""",
                    (encode(response), code, operation_id),
                )
            return OperationResult(operation_id, "rejected", rejection_code=code, source="provider_receipt")
        if outcome != "succeeded":
            raise ProviderProtocolError("provider response has an unknown outcome")

        ticket_id = response.get("ticket_id")
        if not isinstance(ticket_id, str) or not ticket_id:
            raise ProviderProtocolError("provider success receipt does not match the operation")
        with database(self.ledger_db) as db:
            db.execute(
                """UPDATE operations SET status='succeeded', receipt_json=?,
                rejection_code=NULL, last_error=NULL WHERE operation_id=?""",
                (encode(response), operation_id),
            )
        return OperationResult(operation_id, "succeeded", ticket_id=ticket_id, source="provider_receipt")

    def _reconcile(self, operation_id: str, payload_hash: str) -> OperationResult:
        self._mark_unknown(operation_id, "dispatch outcome requires reconciliation")
        try:
            response = self.provider.find_by_client_reference(operation_id)
            if response is None:
                return OperationResult(operation_id, "unknown", source="provider_lookup")
            result = self._accept_provider_response(operation_id, payload_hash, response)
            return OperationResult(
                result.operation_id,
                result.status,
                ticket_id=result.ticket_id,
                rejection_code=result.rejection_code,
                source="provider_lookup",
            )
        except (TimeoutError, ConnectionError, ProviderProtocolError) as exc:
            self._mark_unknown(operation_id, str(exc))
            return OperationResult(operation_id, "unknown", source="provider_lookup")

    def _mark_unknown(self, operation_id: str, error: str) -> None:
        with database(self.ledger_db) as db:
            db.execute(
                """UPDATE operations SET status='unknown', last_error=?
                WHERE operation_id=? AND status IN ('dispatched', 'unknown')""",
                (error, operation_id),
            )

    def _stored_result(self, operation_id: str) -> OperationResult:
        row = operation_snapshot(self.ledger_db, operation_id)
        receipt = row["receipt"] or {}
        return OperationResult(
            operation_id,
            row["status"],
            ticket_id=receipt.get("ticket_id"),
            rejection_code=row["rejection_code"],
            source="ledger",
        )


def operation_snapshot(ledger_db: Path | str, operation_id: str) -> dict[str, object]:
    with database(ledger_db) as db:
        row = db.execute("SELECT * FROM operations WHERE operation_id=?", (operation_id,)).fetchone()
    if row is None:
        raise ValueError("unknown operation")
    result = dict(row)
    result["payload"] = json.loads(result.pop("payload_json"))
    result["receipt"] = json.loads(result.pop("receipt_json")) if result["receipt_json"] else None
    result.pop("receipt_json", None)
    return result


def operation_count(ledger_db: Path | str) -> int:
    with database(ledger_db) as db:
        return db.execute("SELECT COUNT(*) FROM operations").fetchone()[0]


def provider_stats(provider_db: Path | str) -> dict[str, int]:
    with database(provider_db) as db:
        return {
            name: db.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
            for name in ("tickets", "create_calls", "lookup_calls")
        }


def launch_worker(
    ledger_db: Path | str,
    provider_db: Path | str,
    connection_tenant: str,
    context: RequestContext,
    operation_id: str,
    arguments: Mapping[str, object],
    provider_mode: str = "success",
) -> subprocess.CompletedProcess[str]:
    """Start a fresh interpreter against the same durable databases."""
    return subprocess.run(
        [
            sys.executable,
            str(Path(__file__).resolve()),
            "worker",
            "--ledger",
            str(ledger_db),
            "--provider",
            str(provider_db),
            "--connection-tenant",
            connection_tenant,
            "--context-tenant",
            context.tenant_id,
            "--user",
            context.user_id,
            "--operation",
            operation_id,
            "--title",
            str(arguments["title"]),
            "--description",
            str(arguments["description"]),
            "--provider-mode",
            provider_mode,
        ],
        check=False,
        capture_output=True,
        text=True,
    )


def demo() -> None:
    scenarios = (
        ("success", "success"),
        ("business_reject", "business_reject"),
        ("commit_then_timeout", "commit_then_timeout"),
        ("timeout_before_commit", "timeout_before_commit"),
    )
    for name, mode in scenarios:
        with tempfile.TemporaryDirectory(prefix="connector-lab-") as directory:
            ledger_db = Path(directory) / "ledger.db"
            provider_db = Path(directory) / "provider.db"
            initialize(ledger_db, provider_db)
            context = RequestContext("tenant-a", "employee-7", frozenset({CREATE_OWN}))
            arguments = {"title": "VPN 无法连接", "description": "客户端提示证书已过期"}
            first_connector = TicketConnector(
                ledger_db, "tenant-a", FakeProvider(provider_db, mode=mode)
            )
            operation_id = f"op-{name}"
            first = first_connector.submit(context, operation_id, arguments)
            restarted = launch_worker(
                ledger_db,
                provider_db,
                "tenant-a",
                context,
                operation_id,
                arguments,
            )
            if restarted.returncode != 0:
                raise RuntimeError(restarted.stderr or "restarted worker failed")
            second = json.loads(restarted.stdout)
            print(
                encode(
                    {
                        "scenario": name,
                        "first": first.as_dict(),
                        "after_restart": second,
                        **provider_stats(provider_db),
                    }
                )
            )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", nargs="?", choices=("worker",))
    parser.add_argument("--ledger", type=Path)
    parser.add_argument("--provider", type=Path)
    parser.add_argument("--connection-tenant")
    parser.add_argument("--context-tenant")
    parser.add_argument("--user")
    parser.add_argument("--operation")
    parser.add_argument("--title")
    parser.add_argument("--description")
    parser.add_argument("--provider-mode", choices=tuple(sorted(FakeProvider.MODES)), default="success")
    args = parser.parse_args()
    if args.command == "worker":
        required = (
            args.ledger,
            args.provider,
            args.connection_tenant,
            args.context_tenant,
            args.user,
            args.operation,
            args.title,
            args.description,
        )
        if not all(value is not None for value in required):
            parser.error("worker requires durable database, identity, operation and ticket arguments")
        connector = TicketConnector(
            args.ledger,
            args.connection_tenant,
            FakeProvider(args.provider, args.provider_mode),
        )
        context = RequestContext(args.context_tenant, args.user, frozenset({CREATE_OWN}))
        result = connector.submit(
            context,
            args.operation,
            {"title": args.title, "description": args.description},
        )
        print(encode(result.as_dict()))
    else:
        demo()


if __name__ == "__main__":
    main()
