"""Persistent human approval, exact revisions, and an uncertain external write.

Offline teaching code: Context is supplied by a trusted host, not authentication.
Only synthetic data is used. Two SQLite files represent two transaction domains.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import tempfile
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


class Refused(Exception):
    """A deterministic domain rejection, suitable for an explicit UI result."""


@dataclass(frozen=True)
class Context:
    tenant: str
    subject: str
    run: str


ALICE = Context("tenant-a", "alice", "run-1")
BOB = Context("tenant-a", "bob", "run-1")
CAROL = Context("tenant-b", "carol", "run-1")


def canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: object) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def connect(path: Path) -> sqlite3.Connection:
    db = sqlite3.connect(path, timeout=10, isolation_level=None)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    return db


@contextmanager
def transaction(path: Path):
    db = connect(path)
    try:
        db.execute("BEGIN IMMEDIATE")
        yield db
        db.commit()
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()


class TicketSystem:
    """A synthetic downstream system with an operation receipt lookup contract."""

    def __init__(self, path: Path):
        self.path = Path(path)
        with transaction(self.path) as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS tickets (
                    id TEXT PRIMARY KEY, tenant TEXT, owner TEXT, version INTEGER);
                CREATE TABLE IF NOT EXISTS entries (
                    operation_id TEXT PRIMARY KEY, payload_hash TEXT, receipt TEXT,
                    body TEXT);
                CREATE TABLE IF NOT EXISTS calls (id INTEGER PRIMARY KEY, operation_id TEXT);
                INSERT OR IGNORE INTO tickets VALUES ('A-100','tenant-a','alice',1);
            """)

    def ticket(self) -> dict:
        db = connect(self.path)
        try:
            return dict(db.execute("SELECT * FROM tickets WHERE id='A-100'").fetchone())
        finally:
            db.close()

    def append(self, operation_id: str, action: dict) -> dict:
        with transaction(self.path) as db:
            db.execute("INSERT INTO calls(operation_id) VALUES (?)", (operation_id,))
            old = db.execute("SELECT * FROM entries WHERE operation_id=?", (operation_id,)).fetchone()
            if old:
                if old["payload_hash"] != digest(action):
                    return {"status": "rejected", "reason": "operation_payload_conflict",
                            "operation_id": operation_id, "ticket_id": action["ticket_id"]}
                return json.loads(old["receipt"])
            ticket = db.execute("SELECT * FROM tickets WHERE id=?", (action["ticket_id"],)).fetchone()
            if (not ticket or ticket["tenant"] != action["tenant"] or
                    ticket["owner"] != action["actor"] or
                    ticket["version"] != action["ticket_version"]):
                return {"status": "rejected", "reason": "resource_changed",
                        "operation_id": operation_id, "ticket_id": action["ticket_id"]}
            receipt = {"status": "succeeded", "operation_id": operation_id,
                       "entry_id": "entry-" + operation_id, "ticket_id": action["ticket_id"]}
            db.execute("INSERT INTO entries VALUES (?,?,?,?)",
                       (operation_id, digest(action), canonical(receipt), action["body"]))
            db.execute("UPDATE tickets SET version=version+1 WHERE id=?", (action["ticket_id"],))
            return receipt

    def lookup(self, operation_id: str) -> dict | None:
        db = connect(self.path)
        try:
            row = db.execute("SELECT receipt FROM entries WHERE operation_id=?", (operation_id,)).fetchone()
            return json.loads(row[0]) if row else None
        finally:
            db.close()

    def counts(self) -> dict:
        db = connect(self.path)
        try:
            return {"external_calls": db.execute("SELECT count(*) FROM calls").fetchone()[0],
                    "committed_entries": db.execute("SELECT count(*) FROM entries").fetchone()[0]}
        finally:
            db.close()

    def change_version(self):
        with transaction(self.path) as db:
            db.execute("UPDATE tickets SET version=version+1 WHERE id='A-100'")


class ApprovalService:
    def __init__(self, path: Path, downstream: TicketSystem,
                 clock: Callable[[], float] = time.time):
        self.path = Path(path)
        self.downstream = downstream
        self.clock = clock
        with transaction(self.path) as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS members (
                    tenant TEXT, subject TEXT, active INTEGER,
                    PRIMARY KEY(tenant,subject));
                CREATE TABLE IF NOT EXISTS policy (id INTEGER PRIMARY KEY, version INTEGER);
                CREATE TABLE IF NOT EXISTS approvals (
                    id TEXT, revision INTEGER, row_version INTEGER, state TEXT,
                    action TEXT, action_hash TEXT, expires_at REAL,
                    consumed_operation TEXT,
                    PRIMARY KEY(id,revision));
                CREATE TABLE IF NOT EXISTS decisions (
                    tenant TEXT, decision_id TEXT, request_hash TEXT, response TEXT,
                    PRIMARY KEY(tenant,decision_id));
                CREATE TABLE IF NOT EXISTS operations (
                    id TEXT PRIMARY KEY, approval_id TEXT, revision INTEGER,
                    state TEXT, receipt TEXT,
                    UNIQUE(approval_id,revision));
                CREATE TABLE IF NOT EXISTS audit (
                    sequence INTEGER PRIMARY KEY, event TEXT, approval_id TEXT,
                    revision INTEGER, subject TEXT, operation_id TEXT);
                INSERT OR IGNORE INTO members VALUES ('tenant-a','alice',1);
                INSERT OR IGNORE INTO members VALUES ('tenant-a','bob',1);
                INSERT OR IGNORE INTO members VALUES ('tenant-b','carol',1);
                INSERT OR IGNORE INTO policy VALUES (1,1);
            """)

    @staticmethod
    def _body(body: str):
        if not isinstance(body, str) or not body.strip() or len(body) > 500:
            raise Refused("invalid_body")

    @staticmethod
    def _revision(revision: int, expected_version: int):
        if type(revision) is not int or revision < 1 or type(expected_version) is not int or expected_version < 1:
            raise Refused("invalid_version")

    def _authorize(self, db, ctx: Context, action: dict | None = None):
        member = db.execute("SELECT active FROM members WHERE tenant=? AND subject=?",
                            (ctx.tenant, ctx.subject)).fetchone()
        ticket = self.downstream.ticket()
        if (not member or not member[0] or ticket["tenant"] != ctx.tenant or
                ticket["owner"] != ctx.subject):
            raise Refused("not_allowed")
        if action and any(action[k] != v for k, v in
                          (("tenant", ctx.tenant), ("actor", ctx.subject), ("run", ctx.run))):
            raise Refused("context_mismatch")
        return ticket

    @staticmethod
    def _load(db, approval_id, revision):
        row = db.execute("SELECT * FROM approvals WHERE id=? AND revision=?",
                         (approval_id, revision)).fetchone()
        if not row:
            raise Refused("approval_not_found")
        return row

    @staticmethod
    def _snapshot(row):
        return {"approval_id": row["id"], "revision": row["revision"],
                "version": row["row_version"], "state": row["state"]}

    @staticmethod
    def _audit(db, event, approval_id, revision, ctx, operation_id=None):
        db.execute("INSERT INTO audit(event,approval_id,revision,subject,operation_id) VALUES (?,?,?,?,?)",
                   (event, approval_id, revision, ctx.subject, operation_id))

    @staticmethod
    def _no_unresolved(db, ctx):
        rows = db.execute("""SELECT a.action FROM operations o JOIN approvals a
            ON a.id=o.approval_id AND a.revision=o.revision
            WHERE o.state IN ('reserved','unknown')""")
        for row in rows:
            action = json.loads(row[0])
            if (action["tenant"], action["actor"], action["run"]) == (ctx.tenant, ctx.subject, ctx.run):
                raise Refused("unresolved_operation_in_run")

    def create(self, ctx: Context, body: str, ttl: float = 600) -> dict:
        self._body(body)
        if type(ttl) not in (int, float) or not 0 < ttl <= 3600:
            raise Refused("invalid_ttl")
        with transaction(self.path) as db:
            ticket = self._authorize(db, ctx)
            self._no_unresolved(db, ctx)
            action = {"tool": "ticket.append", "ticket_id": ticket["id"], "body": body,
                      "ticket_version": ticket["version"], "tenant": ctx.tenant,
                      "actor": ctx.subject, "run": ctx.run,
                      "policy_version": db.execute("SELECT version FROM policy WHERE id=1").fetchone()[0]}
            aid = "approval-" + uuid.uuid4().hex
            db.execute("INSERT INTO approvals VALUES (?,1,1,'draft',?,?,?,NULL)",
                       (aid, canonical(action), digest(action), self.clock() + ttl))
            self._audit(db, "draft_created", aid, 1, ctx)
            return self._snapshot(self._load(db, aid, 1))

    def submit(self, ctx, approval_id, revision, expected_version) -> dict:
        self._revision(revision, expected_version)
        with transaction(self.path) as db:
            row = self._load(db, approval_id, revision)
            self._authorize(db, ctx, json.loads(row["action"]))
            if row["state"] != "draft" or row["row_version"] != expected_version:
                raise Refused("stale_or_not_draft")
            if self.clock() >= row["expires_at"]:
                raise Refused("expired")
            db.execute("UPDATE approvals SET state='pending',row_version=row_version+1 WHERE id=? AND revision=?",
                       (approval_id, revision))
            self._audit(db, "approval_requested", approval_id, revision, ctx)
            return self._snapshot(self._load(db, approval_id, revision))

    def edit(self, ctx, approval_id, revision, expected_version, body) -> dict:
        self._body(body)
        self._revision(revision, expected_version)
        with transaction(self.path) as db:
            row = self._load(db, approval_id, revision)
            action = json.loads(row["action"])
            ticket = self._authorize(db, ctx, action)
            if (row["row_version"] != expected_version or row["consumed_operation"] or
                    row["state"] not in ("draft", "pending", "approved")):
                raise Refused("stale_or_not_editable")
            if self.clock() >= row["expires_at"]:
                raise Refused("expired")
            action.update(body=body, ticket_version=ticket["version"],
                          policy_version=db.execute("SELECT version FROM policy WHERE id=1").fetchone()[0])
            db.execute("UPDATE approvals SET state='superseded',row_version=row_version+1 WHERE id=? AND revision=?",
                       (approval_id, revision))
            new_revision = revision + 1
            db.execute("INSERT INTO approvals VALUES (?,?,1,'draft',?,?,?,NULL)",
                       (approval_id, new_revision, canonical(action), digest(action), row["expires_at"]))
            self._audit(db, "revision_created", approval_id, new_revision, ctx)
            return self._snapshot(self._load(db, approval_id, new_revision))

    def decide(self, approval_id, revision, expected_version, decision_id, decision, ctx) -> dict:
        self._revision(revision, expected_version)
        if decision not in ("approve", "reject") or not isinstance(decision_id, str) or not 1 <= len(decision_id) <= 100:
            raise Refused("invalid_decision")
        request_hash = digest([approval_id, revision, expected_version, decision,
                               ctx.tenant, ctx.subject, ctx.run])
        with transaction(self.path) as db:
            row = self._load(db, approval_id, revision)
            self._authorize(db, ctx, json.loads(row["action"]))
            old = db.execute("SELECT * FROM decisions WHERE tenant=? AND decision_id=?",
                             (ctx.tenant, decision_id)).fetchone()
            if old:
                if old["request_hash"] != request_hash:
                    raise Refused("decision_id_conflict")
                return json.loads(old["response"])
            if row["state"] != "pending" or row["row_version"] != expected_version:
                raise Refused("stale_or_not_pending")
            state = "expired" if self.clock() >= row["expires_at"] else (
                "approved" if decision == "approve" else "rejected")
            changed = db.execute("""UPDATE approvals SET state=?,row_version=row_version+1
                WHERE id=? AND revision=? AND row_version=? AND state='pending'""",
                                 (state, approval_id, revision, expected_version)).rowcount
            if changed != 1:
                raise Refused("decision_conflict")
            result = self._snapshot(self._load(db, approval_id, revision))
            db.execute("INSERT INTO decisions VALUES (?,?,?,?)",
                       (ctx.tenant, decision_id, request_hash, canonical(result)))
            self._audit(db, state, approval_id, revision, ctx)
            return result

    def cancel(self, ctx, approval_id, revision, expected_version) -> dict:
        self._revision(revision, expected_version)
        with transaction(self.path) as db:
            row = self._load(db, approval_id, revision)
            self._authorize(db, ctx, json.loads(row["action"]))
            if (row["row_version"] != expected_version or row["consumed_operation"] or
                    row["state"] not in ("draft", "pending", "approved")):
                raise Refused("too_late_or_stale")
            db.execute("UPDATE approvals SET state='cancelled',row_version=row_version+1 WHERE id=? AND revision=?",
                       (approval_id, revision))
            self._audit(db, "cancelled", approval_id, revision, ctx)
            return self._snapshot(self._load(db, approval_id, revision))

    def execute(self, ctx, approval_id, revision, fault=None) -> dict:
        if type(revision) is not int or revision < 1 or fault not in (None, "before_dispatch", "lost_receipt"):
            raise Refused("invalid_execution")
        with transaction(self.path) as db:
            row = self._load(db, approval_id, revision)
            action = json.loads(row["action"])
            ticket = self._authorize(db, ctx, action)
            op = db.execute("SELECT * FROM operations WHERE approval_id=? AND revision=?",
                            (approval_id, revision)).fetchone()
            if op:
                if op["state"] in ("succeeded", "failed"):
                    return json.loads(op["receipt"])
                db.execute("UPDATE operations SET state='unknown' WHERE id=?", (op["id"],))
                return {"status": "unknown", "operation_id": op["id"], "reason": "reconcile_only"}
            if row["state"] != "approved":
                raise Refused("not_approved")
            self._no_unresolved(db, ctx)
            if self.clock() >= row["expires_at"]:
                db.execute("UPDATE approvals SET state='expired',row_version=row_version+1 WHERE id=? AND revision=?",
                           (approval_id, revision))
                self._audit(db, "expired", approval_id, revision, ctx)
                return {"status": "expired"}
            if ticket["version"] != action["ticket_version"]:
                raise Refused("resource_changed")
            if db.execute("SELECT version FROM policy WHERE id=1").fetchone()[0] != action["policy_version"]:
                raise Refused("policy_changed")
            if digest(action) != row["action_hash"]:
                raise Refused("snapshot_corrupt")
            operation_id = "operation-" + uuid.uuid4().hex
            db.execute("INSERT INTO operations VALUES (?,?,?,'reserved',NULL)",
                       (operation_id, approval_id, revision))
            db.execute("UPDATE approvals SET consumed_operation=?,row_version=row_version+1 WHERE id=? AND revision=?",
                       (operation_id, approval_id, revision))
            self._audit(db, "dispatch_reserved", approval_id, revision, ctx, operation_id)
        # There is deliberately no transaction spanning the two databases.
        if fault == "before_dispatch":
            return self._unknown(operation_id, "simulated_crash_before_send")
        try:
            receipt = self.downstream.append(operation_id, action)
        except (OSError, sqlite3.Error):
            return self._unknown(operation_id, "transport_uncertain")
        if fault == "lost_receipt":
            return self._unknown(operation_id, "simulated_receipt_loss")
        return self._finish(operation_id, receipt, ctx)

    def _unknown(self, operation_id, reason):
        with transaction(self.path) as db:
            db.execute("UPDATE operations SET state='unknown' WHERE id=? AND state='reserved'", (operation_id,))
        return {"status": "unknown", "operation_id": operation_id, "reason": reason}

    @staticmethod
    def _receipt_state(operation_id, receipt, action):
        if (not isinstance(receipt, dict) or receipt.get("operation_id") != operation_id or
                receipt.get("ticket_id") != action["ticket_id"]):
            return None
        if (set(receipt) == {"status", "operation_id", "ticket_id", "entry_id"} and
                receipt["status"] == "succeeded" and isinstance(receipt["entry_id"], str) and
                1 <= len(receipt["entry_id"]) <= 200):
            return "succeeded"
        # This adapter's resource_changed contract guarantees no write occurred.
        # A payload conflict or arbitrary error does not provide that guarantee.
        if (set(receipt) == {"status", "operation_id", "ticket_id", "reason"} and
                receipt["status"] == "rejected" and receipt["reason"] == "resource_changed"):
            return "failed"
        return None

    def _finish(self, operation_id, receipt, ctx):
        with transaction(self.path) as db:
            op = db.execute("SELECT * FROM operations WHERE id=?", (operation_id,)).fetchone()
            if op["state"] in ("succeeded", "failed"):
                return json.loads(op["receipt"])
            row = self._load(db, op["approval_id"], op["revision"])
            state = self._receipt_state(operation_id, receipt, json.loads(row["action"]))
            if state is None:
                db.execute("UPDATE operations SET state='unknown' WHERE id=?", (operation_id,))
                return {"status": "unknown", "operation_id": operation_id, "reason": "invalid_receipt"}
            db.execute("UPDATE operations SET state=?,receipt=? WHERE id=?", (state, canonical(receipt), operation_id))
            self._audit(db, state, op["approval_id"], op["revision"], ctx, operation_id)
        return receipt

    def reconcile(self, ctx, approval_id, revision) -> dict:
        with transaction(self.path) as db:
            row = self._load(db, approval_id, revision)
            self._authorize(db, ctx, json.loads(row["action"]))
            op = db.execute("SELECT * FROM operations WHERE approval_id=? AND revision=?",
                            (approval_id, revision)).fetchone()
            if not op:
                raise Refused("no_operation")
            if op["state"] in ("succeeded", "failed"):
                return json.loads(op["receipt"])
            operation_id = op["id"]
        receipt = self.downstream.lookup(operation_id)
        if receipt is None:
            return self._unknown(operation_id, "receipt_not_found_keep_unknown")
        return self._finish(operation_id, receipt, ctx)

    def view(self, ctx, approval_id, revision):
        with transaction(self.path) as db:
            row = self._load(db, approval_id, revision)
            self._authorize(db, ctx, json.loads(row["action"]))
            return {**self._snapshot(row), "action": json.loads(row["action"]),
                    "expires_at": row["expires_at"], "operation_id": row["consumed_operation"]}

    def revoke(self):
        with transaction(self.path) as db:
            db.execute("UPDATE members SET active=0 WHERE tenant='tenant-a' AND subject='alice'")

    def change_policy(self):
        with transaction(self.path) as db:
            db.execute("UPDATE policy SET version=version+1 WHERE id=1")


def pending(service, body="VPN reconnect still fails."):
    row = service.create(ALICE, body)
    return service.submit(ALICE, row["approval_id"], row["revision"], row["version"])


def approved(service):
    row = pending(service)
    return service.decide(row["approval_id"], row["revision"], row["version"],
                          uuid.uuid4().hex, "approve", ALICE)


def demo():
    outcomes = {}
    with tempfile.TemporaryDirectory(prefix="ch22-hitl-") as directory:
        root = Path(directory)
        provider = TicketSystem(root / "tickets.sqlite")
        service = ApprovalService(root / "approvals.sqlite", provider)
        row = pending(service)
        rejected = service.decide(row["approval_id"], 1, row["version"], "reject-1", "reject", ALICE)
        outcomes["human_rejected"] = rejected["state"]
        try:
            service.execute(ALICE, row["approval_id"], 1)
        except Refused as exc:
            outcomes["rejected_write"] = str(exc)
        row = pending(service)
        edited = service.edit(ALICE, row["approval_id"], 1, row["version"], "Updated explanation.")
        outcomes["edited_new_revision"] = edited["revision"]
        try:
            service.decide(row["approval_id"], 1, row["version"], "old-tab", "approve", ALICE)
        except Refused as exc:
            outcomes["old_tab_approval"] = str(exc)
        row = approved(service)
        outcomes["approved_but_not_completed"] = {"approval_state": row["state"], **provider.counts()}
        result = service.execute(ALICE, row["approval_id"], 1, fault="lost_receipt")
        outcomes["external_success_receipt_lost"] = result["status"]
        service = ApprovalService(root / "approvals.sqlite", TicketSystem(root / "tickets.sqlite"))
        outcomes["resume_does_not_resend"] = service.execute(ALICE, row["approval_id"], 1)["status"]
        recovered = service.reconcile(ALICE, row["approval_id"], 1)
        outcomes["read_only_reconciliation"] = recovered["status"]
        outcomes["repeat_execution_reuses_receipt"] = service.execute(ALICE, row["approval_id"], 1) == recovered
        print(json.dumps({"outcomes": outcomes, **provider.counts()}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["demo"])
    parser.parse_args()
    demo()
