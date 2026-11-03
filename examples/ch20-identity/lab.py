"""Offline identity/authorization lab. Fixed sessions are fixtures, not authentication."""

from __future__ import annotations

import argparse
from dataclasses import dataclass, replace
import hashlib
import json
import re
import secrets
import sqlite3
import time
from typing import Any, Callable


class Rejected(Exception):
    """A stable, non-sensitive denial code; never include supplied text in it."""


@dataclass(frozen=True)
class RequestContext:
    tenant_id: str
    actor_id: str
    session_id: str
    role_snapshot: str
    membership_revision: int
    request_id: str


@dataclass(frozen=True)
class ToolCall:
    resource_id: str
    resolution: str
    action: str = "ticket.close"


def args_digest(call: ToolCall) -> str:
    if call.action != "ticket.close":
        raise Rejected("unsupported_action")
    if not isinstance(call.resource_id, str) or not re.fullmatch(r"[a-z][a-z0-9-]{1,63}", call.resource_id):
        raise Rejected("invalid_resource_id")
    if not isinstance(call.resolution, str) or not call.resolution.strip():
        raise Rejected("invalid_resolution")
    if len(call.resolution) > 200:
        raise Rejected("invalid_resolution")
    payload = json.dumps({"resolution": call.resolution}, ensure_ascii=False,
                         sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


class IdentityLab:
    """One connection, one process, synthetic data; no network and no LLM."""

    def __init__(self, now: Callable[[], float] = time.time):
        self.now = now
        self.db = sqlite3.connect(":memory:", isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.cache: dict[tuple[str, str, str], tuple[int, str]] = {}
        self.cache_hits = 0
        self.issued_requests: dict[str, tuple[str, str, str]] = {}
        self.db.executescript("""
            PRAGMA foreign_keys = ON;
            CREATE TABLE memberships (
                tenant TEXT, actor TEXT, role TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1, revision INTEGER NOT NULL DEFAULT 1,
                PRIMARY KEY (tenant, actor)
            );
            CREATE TABLE sessions (
                id TEXT PRIMARY KEY, tenant TEXT NOT NULL, actor TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                FOREIGN KEY (tenant, actor) REFERENCES memberships (tenant, actor)
            );
            CREATE TABLE policy (
                tenant TEXT PRIMARY KEY, revision INTEGER NOT NULL DEFAULT 1,
                writes_enabled INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE tickets (
                tenant TEXT, id TEXT, owner TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'open', version INTEGER NOT NULL DEFAULT 1,
                resolution TEXT,
                PRIMARY KEY (tenant, id)
            );
            CREATE TABLE documents (
                tenant TEXT, id TEXT, owner TEXT NOT NULL, visibility TEXT NOT NULL,
                acl_revision INTEGER NOT NULL DEFAULT 1, body TEXT NOT NULL,
                PRIMARY KEY (tenant, id)
            );
            CREATE TABLE memories (
                tenant TEXT, actor TEXT, id TEXT, body TEXT NOT NULL,
                PRIMARY KEY (tenant, actor, id)
            );
            CREATE TABLE approvals (
                id TEXT PRIMARY KEY, tenant TEXT, actor TEXT, session_id TEXT,
                action TEXT, resource_id TEXT, args_hash TEXT,
                resource_version INTEGER, membership_revision INTEGER, policy_revision INTEGER,
                expires_at REAL, consumed_at REAL
            );
            CREATE TABLE receipts (
                tenant TEXT, actor TEXT, operation_key TEXT, contract_hash TEXT NOT NULL,
                resource_id TEXT NOT NULL, receipt_id TEXT NOT NULL, result_json TEXT NOT NULL,
                PRIMARY KEY (tenant, actor, operation_key)
            );
            CREATE TABLE jobs (
                id TEXT PRIMARY KEY, tenant TEXT, actor TEXT, session_id TEXT,
                call_json TEXT, approval_id TEXT, operation_key TEXT
            );
            CREATE TABLE audit (
                sequence INTEGER PRIMARY KEY, at REAL, request_id TEXT,
                tenant TEXT, actor TEXT, action TEXT, resource TEXT,
                decision TEXT, reason TEXT
            );
        """)
        self.db.executemany("INSERT INTO memberships(tenant, actor, role) VALUES (?, ?, ?)", [
            ("tenant-a", "alice", "employee"), ("tenant-a", "bob", "employee"),
            ("tenant-b", "carol", "employee"), ("tenant-b", "alice", "employee"),
        ])
        self.db.executemany("INSERT INTO sessions(id, tenant, actor) VALUES (?, ?, ?)", [
            ("session-alice-a", "tenant-a", "alice"), ("session-bob-a", "tenant-a", "bob"),
            ("session-carol-b", "tenant-b", "carol"), ("session-alice-b", "tenant-b", "alice"),
        ])
        self.db.executemany("INSERT INTO policy(tenant) VALUES (?)", [("tenant-a",), ("tenant-b",)])
        self.db.executemany("INSERT INTO tickets(tenant, id, owner) VALUES (?, ?, ?)", [
            ("tenant-a", "ticket-own", "alice"), ("tenant-a", "ticket-bob", "bob"),
            ("tenant-b", "ticket-own", "alice"), ("tenant-b", "ticket-carol", "carol"),
        ])
        self.db.executemany("INSERT INTO documents(tenant, id, owner, visibility, body) VALUES (?, ?, ?, ?, ?)", [
            ("tenant-a", "doc-team", "bob", "tenant", "VPN: tenant-a shared guide"),
            ("tenant-a", "doc-alice", "alice", "private", "VPN: Alice private notes"),
            ("tenant-a", "doc-bob", "bob", "private", "VPN: Bob private notes"),
            ("tenant-b", "doc-team", "carol", "tenant", "VPN: tenant-b shared guide"),
        ])
        self.db.executemany("INSERT INTO memories VALUES (?, ?, ?, ?)", [
            ("tenant-a", "alice", "memory-self", "Alice in tenant-a"),
            ("tenant-a", "bob", "memory-self", "Bob in tenant-a"),
            ("tenant-a", "bob", "memory-bob", "Bob private preference"),
            ("tenant-b", "alice", "memory-self", "Alice in tenant-b"),
            ("tenant-b", "carol", "memory-self", "Carol in tenant-b"),
        ])

    def close(self) -> None:
        self.db.close()

    def authenticate(self, session_id: str, untrusted_claims: dict | None = None) -> RequestContext:
        """Only a server-owned session selects actor and tenant; ignore caller claims."""
        row = self.db.execute("""
            SELECT s.*, m.role, m.revision, m.active AS member_active
            FROM sessions s JOIN memberships m ON m.tenant = s.tenant AND m.actor = s.actor
            WHERE s.id = ?
        """, (session_id,)).fetchone()
        if row is None or not row["active"] or not row["member_active"]:
            raise Rejected("identity_unavailable")
        return self._context(row["tenant"], row["actor"], row["id"], row["role"], row["revision"])

    def _context(self, tenant: str, actor: str, session_id: str, role: str, revision: int) -> RequestContext:
        request_id = secrets.token_hex(8)
        self.issued_requests[request_id] = (tenant, actor, session_id)
        return RequestContext(tenant, actor, session_id, role, revision, request_id)

    def _live(self, ctx: RequestContext) -> tuple[sqlite3.Row, sqlite3.Row]:
        """An old context transports identity; it does not preserve old authority."""
        session = self.db.execute("SELECT * FROM sessions WHERE id = ?", (ctx.session_id,)).fetchone()
        if (session is None or not session["active"] or session["actor"] != ctx.actor_id
                or session["tenant"] != ctx.tenant_id):
            raise Rejected("identity_unavailable")
        member = self.db.execute("SELECT * FROM memberships WHERE tenant = ? AND actor = ?",
                                 (ctx.tenant_id, ctx.actor_id)).fetchone()
        if member is None or not member["active"]:
            raise Rejected("membership_revoked")
        policy = self.db.execute("SELECT * FROM policy WHERE tenant = ?", (ctx.tenant_id,)).fetchone()
        if policy is None:
            raise Rejected("policy_unavailable")
        return member, policy

    def _ticket(self, ctx: RequestContext, resource_id: str, write: bool = False):
        member, policy = self._live(ctx)
        ticket = self.db.execute("SELECT * FROM tickets WHERE tenant = ? AND id = ?",
                                 (ctx.tenant_id, resource_id)).fetchone()
        if ticket is None:
            raise Rejected("resource_unavailable")
        if ticket["owner"] != ctx.actor_id:
            raise Rejected("resource_unavailable")
        if write and (member["role"] != "employee" or not policy["writes_enabled"]):
            raise Rejected("action_denied")
        return ticket, member, policy

    def read_ticket(self, ctx: RequestContext, resource_id: str) -> dict:
        try:
            ticket, _, _ = self._ticket(ctx, resource_id)
        except Rejected as error:
            self._audit(ctx, "ticket.read", resource_id, "denied", str(error))
            raise
        self._audit(ctx, "ticket.read", resource_id, "allowed", "owner")
        return dict(ticket)

    def retrieve(self, ctx: RequestContext, term: str) -> list[dict]:
        """Metadata filtering happens in SQL before bodies can enter model context."""
        self._live(ctx)
        rows = self.db.execute("""
            SELECT id, body FROM documents
            WHERE tenant = ? AND (visibility = 'tenant' OR owner = ?) AND instr(body, ?) > 0
            ORDER BY id
        """, (ctx.tenant_id, ctx.actor_id, term)).fetchall()
        self._audit(ctx, "document.search", "documents", "allowed", "scoped_query")
        return [dict(row) for row in rows]

    def read_memory(self, ctx: RequestContext, memory_id: str) -> str:
        self._live(ctx)
        row = self.db.execute("SELECT body FROM memories WHERE tenant = ? AND actor = ? AND id = ?",
                              (ctx.tenant_id, ctx.actor_id, memory_id)).fetchone()
        if row is None:
            self._audit(ctx, "memory.read", memory_id, "denied", "resource_unavailable")
            raise Rejected("resource_unavailable")
        self._audit(ctx, "memory.read", memory_id, "allowed", "scoped_query")
        return row["body"]

    def cached_document(self, ctx: RequestContext, document_id: str) -> str:
        """Even a cache hit requires a fresh metadata/ACL check."""
        self._live(ctx)
        meta = self.db.execute("SELECT owner, visibility, acl_revision FROM documents WHERE tenant = ? AND id = ?",
                               (ctx.tenant_id, document_id)).fetchone()
        if meta is None or (meta["owner"] != ctx.actor_id and meta["visibility"] != "tenant"):
            self._audit(ctx, "document.read", document_id, "denied", "resource_unavailable")
            raise Rejected("resource_unavailable")
        key = (ctx.tenant_id, ctx.actor_id, document_id)
        cached = self.cache.get(key)
        if cached is not None and cached[0] == meta["acl_revision"]:
            self.cache_hits += 1
            body = cached[1]
        else:
            body = self.db.execute("SELECT body FROM documents WHERE tenant = ? AND id = ?",
                                   (ctx.tenant_id, document_id)).fetchone()["body"]
            self.cache[key] = (meta["acl_revision"], body)
        self._audit(ctx, "document.read", document_id, "allowed", "current_acl")
        return body

    def approve(self, ctx: RequestContext, call: ToolCall, ttl: float = 60) -> str:
        """Trusted confirmation fixture, deliberately absent from the model tool surface."""
        digest = args_digest(call)
        ticket, member, policy = self._ticket(ctx, call.resource_id, write=True)
        if ticket["status"] != "open":
            raise Rejected("resource_not_open")
        approval_id = secrets.token_urlsafe(24)
        self.db.execute("""
            INSERT INTO approvals VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
        """, (approval_id, ctx.tenant_id, ctx.actor_id, ctx.session_id, call.action,
              call.resource_id, digest, ticket["version"], member["revision"],
              policy["revision"], self.now() + ttl))
        self._audit(ctx, call.action, call.resource_id, "approval_created", "trusted_confirmation")
        return approval_id

    def queue(self, ctx: RequestContext, call: ToolCall, approval_id: str, operation_key: str) -> str:
        digest = args_digest(call)
        self._validate_operation_key(operation_key)
        self.db.execute("BEGIN IMMEDIATE")
        try:
            ticket, member, policy = self._ticket(ctx, call.resource_id, write=True)
            self._validate_approval(ctx, call, approval_id, digest, ticket, member, policy)
            job_id = secrets.token_hex(12)
            call_json = json.dumps({"resource_id": call.resource_id, "resolution": call.resolution, "action": call.action})
            self.db.execute("INSERT INTO jobs VALUES (?, ?, ?, ?, ?, ?, ?)",
                            (job_id, ctx.tenant_id, ctx.actor_id, ctx.session_id, call_json, approval_id, operation_key))
            self._audit(ctx, call.action, call.resource_id, "allowed", "queued")
            self.db.execute("COMMIT")
            return job_id
        except Rejected as error:
            self.db.execute("ROLLBACK")
            self._audit(ctx, call.action, call.resource_id, "denied", str(error))
            raise
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    @staticmethod
    def _validate_operation_key(operation_key: str) -> None:
        if not isinstance(operation_key, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", operation_key):
            raise Rejected("invalid_operation_key")

    def _validate_approval(self, ctx: RequestContext, call: ToolCall, approval_id: str,
                           digest: str, ticket: sqlite3.Row, member: sqlite3.Row,
                           policy: sqlite3.Row) -> sqlite3.Row:
        """Check admission or execution against live state without consuming approval."""
        approval = self.db.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,)).fetchone()
        if approval is None:
            raise Rejected("approval_unavailable")
        expected = (ctx.tenant_id, ctx.actor_id, ctx.session_id, call.action, call.resource_id, digest)
        actual = tuple(approval[key] for key in ("tenant", "actor", "session_id", "action", "resource_id", "args_hash"))
        if expected != actual:
            raise Rejected("approval_scope_mismatch")
        if approval["consumed_at"] is not None:
            raise Rejected("approval_consumed")
        if approval["expires_at"] <= self.now():
            raise Rejected("approval_expired")
        if approval["membership_revision"] != member["revision"]:
            raise Rejected("approval_stale_membership")
        if approval["policy_revision"] != policy["revision"]:
            raise Rejected("approval_stale_policy")
        if approval["resource_version"] != ticket["version"]:
            raise Rejected("approval_stale_resource")
        if ticket["status"] != "open":
            raise Rejected("resource_not_open")
        return approval

    def run_job(self, job_id: str) -> dict:
        """Internal worker entry point; the queue carries references, not old roles."""
        job = self.db.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if job is None:
            raise Rejected("job_unavailable")
        # _live verifies the original binding and current membership inside execute_close.
        ctx = self._context(job["tenant"], job["actor"], job["session_id"], "unused", 0)
        return self.execute_close(ctx, ToolCall(**json.loads(job["call_json"])), job["approval_id"], job["operation_key"])

    def execute_close(self, ctx: RequestContext, call: ToolCall, approval_id: str, operation_key: str) -> dict:
        digest = args_digest(call)
        self._validate_operation_key(operation_key)
        contract = hashlib.sha256(json.dumps([call.action, call.resource_id, digest]).encode()).hexdigest()
        self.db.execute("BEGIN IMMEDIATE")
        try:
            ticket, member, policy = self._ticket(ctx, call.resource_id, write=True)
            receipt = self.db.execute("SELECT * FROM receipts WHERE tenant = ? AND actor = ? AND operation_key = ?",
                                      (ctx.tenant_id, ctx.actor_id, operation_key)).fetchone()
            if receipt is not None:
                if receipt["contract_hash"] != contract:
                    raise Rejected("idempotency_conflict")
                result = json.loads(receipt["result_json"])
                result["replayed_receipt"] = True
                self._audit(ctx, call.action, call.resource_id, "allowed", "existing_receipt")
                self.db.execute("COMMIT")
                return result
            self._validate_approval(ctx, call, approval_id, digest, ticket, member, policy)
            consumed = self.db.execute("UPDATE approvals SET consumed_at = ? WHERE id = ? AND consumed_at IS NULL",
                                       (self.now(), approval_id)).rowcount
            if consumed != 1:
                raise Rejected("approval_consumed")
            written = self.db.execute("""
                UPDATE tickets SET status = 'closed', version = version + 1, resolution = ?
                WHERE tenant = ? AND id = ? AND version = ? AND status = 'open'
            """, (call.resolution, ctx.tenant_id, call.resource_id, ticket["version"])).rowcount
            if written != 1:
                raise Rejected("resource_changed")
            result = {"tenant_id": ctx.tenant_id, "actor_id": ctx.actor_id,
                      "resource_id": call.resource_id, "status": "closed", "version": ticket["version"] + 1,
                      "receipt_id": secrets.token_hex(12), "replayed_receipt": False}
            self.db.execute("INSERT INTO receipts VALUES (?, ?, ?, ?, ?, ?, ?)",
                            (ctx.tenant_id, ctx.actor_id, operation_key, contract, call.resource_id,
                             result["receipt_id"], json.dumps(result)))
            self._audit(ctx, call.action, call.resource_id, "allowed", "committed")
            self.db.execute("COMMIT")
            return result
        except Rejected as error:
            self.db.execute("ROLLBACK")
            self._audit(ctx, call.action, call.resource_id, "denied", str(error))
            raise
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def _audit(self, ctx: RequestContext, action: str, resource: str, decision: str, reason: str) -> None:
        # Explicit allowlist: no resolution/body, session handle, approval handle, or bearer token.
        safe_resource = resource if isinstance(resource, str) and re.fullmatch(r"[a-z][a-z0-9-]{1,63}", resource) else "invalid-id"
        session = self.db.execute("SELECT * FROM sessions WHERE id = ?", (ctx.session_id,)).fetchone()
        binding = (ctx.tenant_id, ctx.actor_id, ctx.session_id)
        verified = (session is not None and session["active"]
                    and session["tenant"] == ctx.tenant_id and session["actor"] == ctx.actor_id)
        # A rejected, edited context must never create a false event attributed to its claimed actor.
        tenant, actor = (session["tenant"], session["actor"]) if verified else (None, None)
        request_id = (ctx.request_id if verified and self.issued_requests.get(ctx.request_id) == binding
                      else secrets.token_hex(8))
        self.db.execute("INSERT INTO audit(at, request_id, tenant, actor, action, resource, decision, reason) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (self.now(), request_id, tenant, actor, action, safe_resource, decision, reason))

    def audit_events(self) -> list[dict]:
        """Trusted operator fixture, not an endpoint accessible to tenant users."""
        return [dict(row) for row in self.db.execute("SELECT * FROM audit ORDER BY sequence")]

    # Administrative test fixtures below are not client or model tools.
    def change_membership(self, tenant: str, actor: str, *, role: str | None = None, active: bool = True) -> None:
        self.db.execute("UPDATE memberships SET role = COALESCE(?, role), active = ?, revision = revision + 1 WHERE tenant = ? AND actor = ?",
                        (role, int(active), tenant, actor))

    def change_policy(self, tenant: str, *, writes_enabled: bool = True) -> None:
        self.db.execute("UPDATE policy SET writes_enabled = ?, revision = revision + 1 WHERE tenant = ?",
                        (int(writes_enabled), tenant))

    def privatize_document(self, tenant: str, document_id: str) -> None:
        self.db.execute("UPDATE documents SET visibility = 'private', acl_revision = acl_revision + 1 WHERE tenant = ? AND id = ?",
                        (tenant, document_id))


SCENARIOS = (
    "success", "tenant_spoof", "cross_tenant_resource", "same_tenant_other_owner",
    "revoked_membership", "role_downgrade", "forged_approval", "changed_args",
    "resource_changed", "approval_replay", "cache_acl_change", "memory_isolation",
    "retrieval_filter", "idempotency_scope",
)


def scenario(name: str) -> dict[str, Any]:
    lab = IdentityLab()
    ctx = lab.authenticate("session-alice-a", {"tenant_id": "tenant-b", "actor_id": "carol"})
    call = ToolCall("ticket-own", "VPN access restored; synthetic data")
    try:
        if name == "tenant_spoof":
            return {"scenario": name, "status": "server_scope_preserved", "tenant_id": ctx.tenant_id, "actor_id": ctx.actor_id}
        if name == "cross_tenant_resource":
            lab.read_ticket(ctx, "ticket-carol")
        elif name == "same_tenant_other_owner":
            lab.read_ticket(ctx, "ticket-bob")
        elif name == "cache_acl_change":
            lab.cached_document(ctx, "doc-team")
            lab.cached_document(ctx, "doc-team")
            lab.privatize_document("tenant-a", "doc-team")
            lab.cached_document(ctx, "doc-team")
        elif name == "memory_isolation":
            lab.read_memory(ctx, "memory-bob")
        elif name == "retrieval_filter":
            return {"scenario": name, "status": "scoped", "document_ids": [row["id"] for row in lab.retrieve(ctx, "VPN")]}
        elif name == "idempotency_scope":
            first = lab.execute_close(ctx, call, lab.approve(ctx, call), "same-key")
            other = lab.authenticate("session-alice-b")
            second = lab.execute_close(other, call, lab.approve(other, call), "same-key")
            return {"scenario": name, "status": "two_scoped_receipts", "tenants": [first["tenant_id"], second["tenant_id"]],
                    "receipt_count": lab.db.execute("SELECT count(*) FROM receipts").fetchone()[0]}
        else:
            approval = lab.approve(ctx, call)
            if name == "forged_approval":
                lab.queue(ctx, call, "invented-by-model", "operation-1")
            elif name == "changed_args":
                lab.queue(ctx, replace(call, resolution="Unapproved changed text"), approval, "operation-1")
            job = lab.queue(ctx, call, approval, "operation-1")
            if name == "revoked_membership":
                lab.change_membership("tenant-a", "alice", active=False)
            elif name == "role_downgrade":
                lab.change_membership("tenant-a", "alice", role="viewer")
            elif name == "resource_changed":
                lab.db.execute("UPDATE tickets SET version = version + 1 WHERE tenant = ? AND id = ?", (ctx.tenant_id, call.resource_id))
            elif name == "approval_replay":
                lab.run_job(job)
                lab.execute_close(ctx, call, approval, "operation-2")
            result = lab.run_job(job)
            return {"scenario": name, "status": result["status"], "version": result["version"], "receipt_count": 1}
        raise AssertionError("scenario expected a rejection")
    except Rejected as error:
        return {"scenario": name, "status": "denied", "reason": str(error),
                "receipt_count": lab.db.execute("SELECT count(*) FROM receipts").fetchone()[0]}
    finally:
        lab.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", choices=("all",) + SCENARIOS, default="all")
    args = parser.parse_args()
    for name in SCENARIOS if args.scenario == "all" else (args.scenario,):
        print(json.dumps(scenario(name), ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
