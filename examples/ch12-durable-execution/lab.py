"""Single-worker durable execution lab. Python 3.11+, SQLite, no network.

runtime.db and provider.db deliberately have independent transactions. Crash
injection uses os._exit after committed boundaries, never exception rollback.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile

CRASH_EXIT = 73


@contextmanager
def database(path):
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA synchronous = FULL")
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def encode(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def payload_hash(payload):
    return hashlib.sha256(encode(payload).encode()).hexdigest()


def initialize(runtime_db, provider_db):
    with database(runtime_db) as db:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS runs (
                run_id TEXT PRIMARY KEY, status TEXT NOT NULL,
                version INTEGER NOT NULL, payload TEXT NOT NULL,
                payload_hash TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS actions (
                action_id TEXT PRIMARY KEY, run_id TEXT NOT NULL UNIQUE,
                operation_key TEXT NOT NULL UNIQUE, payload_hash TEXT NOT NULL,
                status TEXT NOT NULL, receipt TEXT);
            CREATE TABLE IF NOT EXISTS inbox (
                event_id TEXT PRIMARY KEY, run_id TEXT NOT NULL,
                payload_hash TEXT NOT NULL, result TEXT NOT NULL);
        """)
    with database(provider_db) as db:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS tickets (
                operation_key TEXT PRIMARY KEY, payload_hash TEXT NOT NULL,
                receipt TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS create_calls (operation_key TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS lookup_calls (operation_key TEXT NOT NULL);
        """)


def create_run(runtime_db, run_id, payload):
    digest = payload_hash(payload)
    with database(runtime_db) as db:
        db.execute("INSERT INTO runs VALUES (?, 'WAITING_APPROVAL', 0, ?, ?)",
                   (run_id, encode(payload), digest))
    return digest


def revise_payload(runtime_db, run_id, expected_version, payload):
    """A small CAS example: only an unapproved payload may be edited."""
    with database(runtime_db) as db:
        result = db.execute("""UPDATE runs SET payload=?, payload_hash=?,
            version=version+1 WHERE run_id=? AND version=?
            AND status='WAITING_APPROVAL'""",
            (encode(payload), payload_hash(payload), run_id, expected_version))
        return result.rowcount == 1


def approve(runtime_db, run_id, event_id, approved_hash):
    """Trusted approval input; identity/permission checks are outside this lab."""
    with database(runtime_db) as db:
        db.execute("BEGIN IMMEDIATE")
        seen = db.execute("SELECT * FROM inbox WHERE event_id=?", (event_id,)).fetchone()
        if seen:
            if (seen["run_id"], seen["payload_hash"]) != (run_id, approved_hash):
                raise ValueError("event_id reused with different content")
            return "DUPLICATE"
        run = db.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        if run is None:
            raise ValueError("unknown run")
        if approved_hash != run["payload_hash"]:
            result = "STALE_APPROVAL"
        elif run["status"] != "WAITING_APPROVAL":
            result = "ALREADY_APPROVED"
        else:
            # The operation identity binds this run to the exact approved payload.
            key = "op_" + hashlib.sha256((run_id + ":" + approved_hash).encode()).hexdigest()
            db.execute("INSERT INTO actions VALUES (?, ?, ?, ?, 'PREPARED', NULL)",
                       (run_id + "/submit", run_id, key, approved_hash))
            db.execute("UPDATE runs SET status='READY', version=version+1 WHERE run_id=?",
                       (run_id,))
            result = "ACCEPTED"
        # Inbox receipt and approval state change commit in the same local transaction.
        db.execute("INSERT INTO inbox VALUES (?, ?, ?, ?)",
                   (event_id, run_id, approved_hash, result))
        return result


def snapshot(runtime_db, run_id):
    with database(runtime_db) as db:
        row = db.execute("""SELECT r.run_id, r.status AS run_status, r.version,
            r.payload_hash, a.action_id, a.operation_key,
            a.status AS action_status, a.receipt FROM runs r
            LEFT JOIN actions a USING(run_id) WHERE r.run_id=?""", (run_id,)).fetchone()
        if row is None:
            raise ValueError("unknown run")
        return dict(row)


def provider_create(provider_db, key, digest):
    # Count actual create invocations separately. A repeated create is observable
    # even if the provider's UNIQUE constraint subsequently rejects the request.
    with database(provider_db) as db:
        db.execute("INSERT INTO create_calls VALUES (?)", (key,))
    receipt = encode({"ticket_id": "ticket_" + key[-12:], "operation_key": key})
    with database(provider_db) as db:
        db.execute("INSERT INTO tickets VALUES (?, ?, ?)", (key, digest, receipt))
    return receipt


def provider_lookup(provider_db, key):
    with database(provider_db) as db:
        db.execute("INSERT INTO lookup_calls VALUES (?)", (key,))
        row = db.execute("SELECT * FROM tickets WHERE operation_key=?", (key,)).fetchone()
        return dict(row) if row else None


def provider_stats(provider_db):
    with database(provider_db) as db:
        return {name: db.execute("SELECT COUNT(*) FROM " + name).fetchone()[0]
                for name in ("tickets", "create_calls", "lookup_calls")}


def finish(runtime_db, run_id, receipt):
    # Persist the receipt and completion together in one runtime transaction.
    with database(runtime_db) as db:
        db.execute("UPDATE actions SET status='SUCCEEDED', receipt=? WHERE run_id=?",
                   (receipt, run_id))
        db.execute("UPDATE runs SET status='COMPLETED', version=version+1 WHERE run_id=?",
                   (run_id,))


def crash_if(selected, current):
    if selected == current:
        os._exit(CRASH_EXIT)


def advance(runtime_db, provider_db, run_id, crash_at=None):
    """Resume this run with a trusted single worker. Never regenerate action IDs."""
    state = snapshot(runtime_db, run_id)
    if state["run_status"] == "WAITING_APPROVAL":
        crash_if(crash_at, "waiting")
        return "WAITING_APPROVAL"
    if state["action_status"] == "SUCCEEDED":
        return "COMPLETED"  # Reuse a confirmed receipt; do not call create again.
    if state["action_status"] == "PREPARED":
        crash_if(crash_at, "prepared")  # Durable intent exists, dispatch has not begun.
        with database(runtime_db) as db:
            db.execute("UPDATE actions SET status='DISPATCHING' WHERE run_id=?", (run_id,))
            db.execute("UPDATE runs SET status='RUNNING', version=version+1 WHERE run_id=?",
                       (run_id,))
        crash_if(crash_at, "dispatching")
        try:
            receipt = provider_create(provider_db, state["operation_key"], state["payload_hash"])
        except Exception:
            # An error after dispatch cannot prove that no external write occurred.
            pass
        else:
            crash_if(crash_at, "after_provider")
            finish(runtime_db, run_id, receipt)
            return "COMPLETED"
    # DISPATCHING after restart is ambiguous, even if this particular injected
    # crash happened before the provider call. UNKNOWN must be persisted first.
    with database(runtime_db) as db:
        db.execute("UPDATE actions SET status='UNKNOWN' WHERE run_id=?", (run_id,))
    found = provider_lookup(provider_db, state["operation_key"])
    if found and found["payload_hash"] == state["payload_hash"]:
        finish(runtime_db, run_id, found["receipt"])
        return "COMPLETED"
    # This fake query does not promise authoritative negative results. Absence
    # (or a mismatched payload) therefore never authorizes another create.
    with database(runtime_db) as db:
        db.execute("UPDATE runs SET status='HUMAN_REQUIRED', version=version+1 WHERE run_id=?",
                   (run_id,))
    return "HUMAN_REQUIRED"


def launch_worker(runtime_db, provider_db, run_id, crash_at):
    return subprocess.run([sys.executable, str(Path(__file__).resolve()), "worker",
                           "--runtime", str(runtime_db), "--provider", str(provider_db),
                           "--run", run_id, "--crash", crash_at], check=False,
                          capture_output=True, text=True)


def demo():
    for crash_at in ("waiting", "prepared", "after_provider", "dispatching"):
        with tempfile.TemporaryDirectory(prefix="durable-lab-") as directory:
            runtime_db = Path(directory) / "runtime.db"
            provider_db = Path(directory) / "provider.db"
            initialize(runtime_db, provider_db)
            run_id = "demo-" + crash_at
            digest = create_run(runtime_db, run_id, {"title": "申请开通测试环境权限"})
            if crash_at != "waiting":
                approve(runtime_db, run_id, "approval-1", digest)
            child = launch_worker(runtime_db, provider_db, run_id, crash_at)
            if child.returncode != CRASH_EXIT:
                raise RuntimeError("worker did not reach the requested crash boundary")
            before = snapshot(runtime_db, run_id)
            if crash_at == "waiting":
                approve(runtime_db, run_id, "approval-1", digest)
            result = advance(runtime_db, provider_db, run_id)
            print(encode({"crash": crash_at, "exit": child.returncode,
                          "persisted": before["action_status"] or before["run_status"],
                          "resumed": result, **provider_stats(provider_db)}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", nargs="?", choices=("worker",), help="internal crash worker")
    parser.add_argument("--runtime", type=Path)
    parser.add_argument("--provider", type=Path)
    parser.add_argument("--run")
    parser.add_argument("--crash", choices=("waiting", "prepared", "dispatching", "after_provider"))
    args = parser.parse_args()
    if args.mode == "worker":
        if not all((args.runtime, args.provider, args.run, args.crash)):
            parser.error("worker requires --runtime, --provider, --run and --crash")
        advance(args.runtime, args.provider, args.run, args.crash)
    else:
        demo()


if __name__ == "__main__":
    main()
