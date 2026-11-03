#!/usr/bin/env python3
"""Offline teaching model: durable admission, leased work and owned artifacts.

Python 3.11 standard library only. Principal and scheduler calls are trusted
inputs from an imagined authentication/service boundary, not authentication.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import tempfile
import time
from typing import Callable, Iterator
import uuid


class Rejected(ValueError):
    pass


class NotFound(Rejected):
    """Same outward error for an absent resource and a resource owned by others."""


class Conflict(Rejected):
    pass


class Busy(Rejected):
    pass


class StaleLease(Rejected):
    pass


@dataclass(frozen=True)
class Principal:
    tenant_id: str
    user_id: str


@dataclass(frozen=True)
class Lease:
    run_id: str
    tenant_id: str
    worker_id: str
    epoch: int
    expires_at: float


class Store:
    """One SQLite database on one host; independent connections per operation."""

    def __init__(self, database: Path, *, clock: Callable[[], float] = time.time,
                 max_active: int = 1, max_open_runs: int = 20):
        if max_active < 1 or max_open_runs < 1:
            raise ValueError("limits must be positive")
        self.database = Path(database)
        self.clock = clock
        self.max_active = max_active
        self.max_open_runs = max_open_runs
        with self.connection() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL,
                    user_id TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL, user_id TEXT NOT NULL,
                    session_id TEXT NOT NULL REFERENCES sessions(id),
                    request_key TEXT NOT NULL, fingerprint TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN
                        ('queued','running','cancel_requested','cancelled','succeeded')),
                    created_at REAL NOT NULL,
                    lease_epoch INTEGER NOT NULL DEFAULT 0,
                    lease_owner TEXT, lease_until REAL,
                    UNIQUE(tenant_id, request_key)
                );
                CREATE INDEX IF NOT EXISTS runs_schedule
                    ON runs(tenant_id, state, created_at);
                CREATE TABLE IF NOT EXISTS artifacts (
                    id TEXT PRIMARY KEY, run_id TEXT NOT NULL UNIQUE REFERENCES runs(id),
                    tenant_id TEXT NOT NULL, user_id TEXT NOT NULL,
                    name TEXT NOT NULL, sha256 TEXT NOT NULL, content BLOB NOT NULL
                );
            """)

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.database, timeout=5, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            yield db
        finally:
            db.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                yield db
                db.execute("COMMIT")
            except BaseException:
                db.execute("ROLLBACK")
                raise

    def create_session(self, principal: Principal) -> str:
        session_id = "ses_" + uuid.uuid4().hex
        with self.transaction() as db:
            db.execute("INSERT INTO sessions VALUES (?,?,?)",
                       (session_id, principal.tenant_id, principal.user_id))
        return session_id

    @staticmethod
    def _session(db: sqlite3.Connection, principal: Principal, session_id: str) -> None:
        found = db.execute("SELECT 1 FROM sessions WHERE id=? AND tenant_id=? AND user_id=?",
                           (session_id, principal.tenant_id, principal.user_id)).fetchone()
        if found is None:
            raise NotFound("session not found")

    @staticmethod
    def _run(db: sqlite3.Connection, principal: Principal, run_id: str) -> sqlite3.Row:
        row = db.execute("SELECT * FROM runs WHERE id=? AND tenant_id=? AND user_id=?",
                         (run_id, principal.tenant_id, principal.user_id)).fetchone()
        if row is None:
            raise NotFound("run not found")
        return row

    def submit(self, principal: Principal, session_id: str,
               request_key: str, payload: dict) -> tuple[str, bool]:
        """Return (run_id, created); an accepted run is durable before return."""
        if not isinstance(request_key, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", request_key):
            raise Rejected("invalid request key")
        if (not isinstance(payload, dict) or set(payload) != {"topic"}
                or not isinstance(payload["topic"], str)
                or not 1 <= len(payload["topic"].strip()) <= 120):
            raise Rejected("payload must contain only a nonempty topic, at most 120 characters")
        normalized = json.dumps({"topic": payload["topic"].strip()},
                                ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        fingerprint = hashlib.sha256((session_id + "\n" + normalized).encode()).hexdigest()
        with self.transaction() as db:
            self._session(db, principal, session_id)
            previous = db.execute("SELECT * FROM runs WHERE tenant_id=? AND request_key=?",
                                  (principal.tenant_id, request_key)).fetchone()
            if previous is not None:
                # A tenant-wide key is not a way to discover another user's run.
                if previous["user_id"] != principal.user_id:
                    raise Conflict("request key unavailable")
                if previous["fingerprint"] != fingerprint:
                    raise Conflict("same key bound to a different request")
                return previous["id"], False
            count = db.execute("""SELECT count(*) FROM runs WHERE tenant_id=?
                AND state IN ('queued','running','cancel_requested')""",
                               (principal.tenant_id,)).fetchone()[0]
            if count >= self.max_open_runs:
                raise Busy("tenant open-run limit reached")
            run_id = "run_" + uuid.uuid4().hex
            db.execute("""INSERT INTO runs
                (id,tenant_id,user_id,session_id,request_key,fingerprint,payload,state,created_at)
                VALUES (?,?,?,?,?,?,?,'queued',?)""",
                       (run_id, principal.tenant_id, principal.user_id, session_id,
                        request_key, fingerprint, normalized, self.clock()))
            return run_id, True

    def get_run(self, principal: Principal, run_id: str) -> dict:
        with self.connection() as db:
            row = db.execute("""SELECT r.*,a.id AS artifact_id FROM runs r
                LEFT JOIN artifacts a ON a.run_id=r.id
                WHERE r.id=? AND r.tenant_id=? AND r.user_id=?""",
                             (run_id, principal.tenant_id, principal.user_id)).fetchone()
            if row is None:
                raise NotFound("run not found")
            return dict(row)

    def claim(self, tenant_id: str, worker_id: str, *, lease_seconds: float = 30) -> Lease | None:
        """Trusted scheduler entry. Wakeup messages may call this repeatedly.

        The database is the source of runnable work; no queue message owns a run.
        """
        if not isinstance(worker_id, str) or not 1 <= len(worker_id) <= 80 or not 0 < lease_seconds <= 300:
            raise Rejected("invalid worker or lease duration")
        with self.transaction() as db:
            now = self.clock()  # Sample after acquiring the write transaction.
            # Expired cancelled work can never be dispatched again.
            db.execute("""UPDATE runs SET state='cancelled',lease_owner=NULL,lease_until=NULL
                WHERE tenant_id=? AND state='cancel_requested' AND lease_until<=?""",
                       (tenant_id, now))
            active = db.execute("""SELECT count(*) FROM runs WHERE tenant_id=?
                AND state IN ('running','cancel_requested') AND lease_until>?""",
                                (tenant_id, now)).fetchone()[0]
            if active >= self.max_active:
                return None
            row = db.execute("""SELECT * FROM runs WHERE tenant_id=? AND
                (state='queued' OR (state='running' AND lease_until<=?))
                ORDER BY created_at,id LIMIT 1""", (tenant_id, now)).fetchone()
            if row is None:
                return None
            epoch = row["lease_epoch"] + 1
            until = now + lease_seconds
            db.execute("""UPDATE runs SET state='running',lease_epoch=?,lease_owner=?,lease_until=?
                WHERE id=?""", (epoch, worker_id, until, row["id"]))
            return Lease(row["id"], tenant_id, worker_id, epoch, until)

    @staticmethod
    def _lease(db: sqlite3.Connection, lease: Lease, now: float,
               expected_state: str = "running") -> sqlite3.Row:
        row = db.execute("""SELECT * FROM runs WHERE id=? AND tenant_id=?
            AND lease_owner=? AND lease_epoch=? AND lease_until>? AND state=?""",
                         (lease.run_id, lease.tenant_id, lease.worker_id,
                          lease.epoch, now, expected_state)).fetchone()
        if row is None:
            raise StaleLease("lease expired, superseded, cancelled or already finished")
        return row

    def heartbeat(self, lease: Lease, *, lease_seconds: float = 30) -> Lease:
        if not 0 < lease_seconds <= 300:
            raise Rejected("invalid lease duration")
        with self.transaction() as db:
            now = self.clock()
            self._lease(db, lease, now)
            until = now + lease_seconds
            db.execute("UPDATE runs SET lease_until=? WHERE id=?", (until, lease.run_id))
            return Lease(lease.run_id, lease.tenant_id, lease.worker_id, lease.epoch, until)

    def request_cancel(self, principal: Principal, run_id: str) -> str:
        with self.transaction() as db:
            row = self._run(db, principal, run_id)
            if row["state"] == "queued":
                state = "cancelled"
            elif row["state"] in {"running", "cancel_requested"}:
                state = "cancel_requested"
            elif row["state"] == "succeeded":
                return "too_late"
            else:
                return "cancelled"
            db.execute("UPDATE runs SET state=? WHERE id=?", (state, run_id))
            return state

    def acknowledge_cancel(self, lease: Lease) -> None:
        with self.transaction() as db:
            self._lease(db, lease, self.clock(), "cancel_requested")
            db.execute("""UPDATE runs SET state='cancelled',lease_owner=NULL,lease_until=NULL
                WHERE id=?""", (lease.run_id,))

    def complete(self, lease: Lease, content: bytes) -> str:
        """Atomically commit a small immutable artifact and the terminal run state."""
        if not isinstance(content, bytes) or not 1 <= len(content) <= 65536:
            raise Rejected("artifact must be 1..65536 bytes")
        artifact_id = "art_" + uuid.uuid4().hex
        digest = hashlib.sha256(content).hexdigest()
        with self.transaction() as db:
            row = self._lease(db, lease, self.clock())
            db.execute("INSERT INTO artifacts VALUES (?,?,?,?,?,?,?)",
                       (artifact_id, lease.run_id, row["tenant_id"], row["user_id"],
                        "report.md", digest, content))
            db.execute("""UPDATE runs SET state='succeeded',lease_owner=NULL,lease_until=NULL
                WHERE id=?""", (lease.run_id,))
            return artifact_id

    def download(self, principal: Principal, artifact_id: str) -> bytes:
        with self.connection() as db:
            row = db.execute("""SELECT content,sha256 FROM artifacts
                WHERE id=? AND tenant_id=? AND user_id=?""",
                             (artifact_id, principal.tenant_id, principal.user_id)).fetchone()
            if row is None:
                raise NotFound("artifact not found")
            content = bytes(row["content"])
            if hashlib.sha256(content).hexdigest() != row["sha256"]:
                raise Rejected("artifact checksum mismatch")
            return content


class ManualClock:
    def __init__(self, value: float = 1000):
        self.value = value

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError("clock cannot move backwards in this teaching scenario")
        self.value += seconds


def demo() -> None:
    with tempfile.TemporaryDirectory(prefix="ch25-backend-") as root:
        clock = ManualClock()
        store = Store(Path(root) / "runs.sqlite", clock=clock)
        alice, other_tenant = Principal("tenant-a", "alice"), Principal("tenant-b", "alice")
        session = store.create_session(alice)
        run_id, created = store.submit(alice, session, "report-001", {"topic": "VPN 故障周报"})
        repeated, created_again = store.submit(alice, session, "report-001", {"topic": "VPN 故障周报"})
        print(f"1 持久接收：created={created}，重复请求复用任务={repeated == run_id and not created_again}")
        old = store.claim("tenant-a", "worker-old", lease_seconds=10)
        assert old is not None
        print(f"2 重复唤醒：无额外领取={store.claim('tenant-a', 'worker-duplicate') is None}")
        clock.advance(11)
        recovered = Store(Path(root) / "runs.sqlite", clock=clock)
        current = recovered.claim("tenant-a", "worker-new")
        assert current is not None
        try:
            store.complete(old, b"stale output")
        except StaleLease:
            print(f"3 租约回收：epoch={old.epoch}→{current.epoch}，旧 Worker 回写被拒绝")
        with tempfile.TemporaryDirectory(prefix="workspace-", dir=root) as workspace:
            draft = Path(workspace) / "draft.md"
            draft.write_text("# VPN 故障周报\n\n本报告由离线实验生成。\n", encoding="utf-8")
            artifact = recovered.complete(current, draft.read_bytes())
        content = recovered.download(alice, artifact)
        print(f"4 工作区销毁：exists={Path(workspace).exists()}，正式产物仍可下载={bool(content)}")
        try:
            recovered.download(other_tenant, artifact)
        except NotFound:
            print("5 跨租户下载：拒绝")
        cancel_run, _ = recovered.submit(alice, session, "report-002", {"topic": "取消示例"})
        lease = recovered.claim("tenant-a", "worker-cancel")
        assert lease is not None
        print(f"6 运行中取消：{recovered.request_cancel(alice, cancel_run)}")
        try:
            recovered.complete(lease, b"late result")
        except StaleLease:
            print("  迟到产物提交：拒绝")
        recovered.acknowledge_cancel(lease)
        print(f"  Worker 确认后：{recovered.get_run(alice, cancel_run)['state']}")


if __name__ == "__main__":
    demo()
