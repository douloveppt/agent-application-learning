"""Bounded long-term-memory lab using SQLite; no model, network or real auth."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import re
import sqlite3
from typing import Any, Literal
import uuid

IDENTIFIER = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,63}\Z")
MAX_TEXT_BYTES = 4096
Status = Literal["active", "deleted"]

class MemoryLabError(ValueError):
    """Stable errors deliberately omit identities, values and database details."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)

@dataclass(frozen=True)
class Scope:
    """Trusted application input in this lab; constructing it is not authentication."""

    tenant_id: str
    actor_id: str
    now: datetime

@dataclass(frozen=True)
class ProjectionEntry:
    """One synthetic search record, not an embedding or vector-index result."""

    memory_id: str
    tenant_id: str
    owner_id: str
    slot: str
    value: str | None
    version: int
    status: Status
    expires_at: datetime | None

def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)

def _identifier(value: Any, code: str) -> None:
    if type(value) is not str or not IDENTIFIER.fullmatch(value):
        raise MemoryLabError(code)

def _text(value: Any, code: str, max_bytes: int = MAX_TEXT_BYTES) -> None:
    if type(value) is not str or not value.strip():
        raise MemoryLabError(code)
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError:
        raise MemoryLabError("invalid_unicode") from None
    if size > max_bytes:
        raise MemoryLabError(code)

def _aware(value: Any, code: str) -> None:
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        raise MemoryLabError(code)

def _utc_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")

def _parse_time(value: str | None) -> datetime | None:
    return None if value is None else datetime.fromisoformat(value)

def _expired(expires_at: datetime, now: datetime) -> bool:
    return expires_at.astimezone(timezone.utc) <= now.astimezone(timezone.utc)


def _opaque_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex}"

class MemoryStore:
    """Authoritative preference rows, idempotency receipts and an atomic outbox."""

    def __init__(self, connection: sqlite3.Connection):
        if type(connection) is not sqlite3.Connection or connection.in_transaction:
            raise MemoryLabError("invalid_connection")
        self.db = connection
        self.db.isolation_level = None
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS memories (
              memory_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, owner_id TEXT NOT NULL,
              slot TEXT NOT NULL, value TEXT, status TEXT NOT NULL CHECK(status IN ('active','deleted')),
              version INTEGER NOT NULL CHECK(version > 0), source_ref TEXT NOT NULL,
              expires_at TEXT, updated_at TEXT NOT NULL, UNIQUE(tenant_id,owner_id,slot),
              CHECK((status='active' AND value IS NOT NULL) OR (status='deleted' AND value IS NULL)));
            CREATE TABLE IF NOT EXISTS idempotency_receipts (
              tenant_id TEXT NOT NULL, owner_id TEXT NOT NULL, event_key TEXT NOT NULL,
              payload_sha256 TEXT NOT NULL, result_json TEXT NOT NULL, created_at TEXT NOT NULL,
              PRIMARY KEY(tenant_id,owner_id,event_key));
            CREATE TABLE IF NOT EXISTS outbox (
              event_id TEXT PRIMARY KEY, memory_id TEXT NOT NULL REFERENCES memories(memory_id),
              version INTEGER NOT NULL, status TEXT NOT NULL CHECK(status IN ('active','deleted')),
              created_at TEXT NOT NULL, published_at TEXT, UNIQUE(memory_id,version));
        """)

    @staticmethod
    def _scope(scope: Any) -> None:
        if type(scope) is not Scope:
            raise MemoryLabError("invalid_scope")
        _identifier(scope.tenant_id, "invalid_scope")
        _identifier(scope.actor_id, "invalid_scope")
        _aware(scope.now, "invalid_scope_time")

    @staticmethod
    def _write_inputs(scope: Scope, event_key: Any, slot: Any, expected_version: Any,
                      source_ref: Any, confirmed: Any) -> None:
        MemoryStore._scope(scope)
        _identifier(event_key, "invalid_event_key")
        _identifier(slot, "invalid_slot")
        _text(source_ref, "invalid_source_ref", 256)
        if type(expected_version) is not int or expected_version < 0:
            raise MemoryLabError("invalid_expected_version")
        if confirmed is not True:
            raise MemoryLabError("unconfirmed_memory")

    def _transact(self, operation) -> dict[str, Any]:
        if self.db.in_transaction:
            raise MemoryLabError("invalid_transaction_state")
        try:
            self.db.execute("BEGIN IMMEDIATE")
            result = operation()
            self.db.execute("COMMIT")
            return result
        except MemoryLabError:
            if self.db.in_transaction:
                self.db.execute("ROLLBACK")
            raise
        except (sqlite3.DatabaseError, json.JSONDecodeError):
            if self.db.in_transaction:
                self.db.execute("ROLLBACK")
            raise MemoryLabError("storage_failure") from None

    def _replay(self, scope: Scope, event_key: str, payload_hash: str) -> dict[str, Any] | None:
        row = self.db.execute(
            "SELECT payload_sha256,result_json FROM idempotency_receipts "
            "WHERE tenant_id=? AND owner_id=? AND event_key=?",
            (scope.tenant_id, scope.actor_id, event_key),
        ).fetchone()
        if row is None:
            return None
        if row["payload_sha256"] != payload_hash:
            raise MemoryLabError("idempotency_conflict")
        return json.loads(row["result_json"])

    def _finish(self, scope: Scope, event_key: str, payload_hash: str, memory_id: str,
                version: int, status: Status) -> dict[str, Any]:
        event_id = _opaque_id("out")
        receipt = {"memory_id": memory_id, "version": version, "status": status,
                   "outbox_event_id": event_id}
        now = _utc_text(scope.now)
        self.db.execute(
            "INSERT INTO outbox(event_id,memory_id,version,status,created_at) VALUES(?,?,?,?,?)",
            (event_id, memory_id, version, status, now),
        )
        self.db.execute(
            "INSERT INTO idempotency_receipts VALUES(?,?,?,?,?,?)",
            (scope.tenant_id, scope.actor_id, event_key, payload_hash, canonical_json(receipt), now),
        )
        return receipt

    def _mutate(self, scope: Scope, *, action: Literal["put", "delete"], event_key: str,
                slot: str, value: str | None, expected_version: int, source_ref: str,
                confirmed: bool, expires_at: datetime | None) -> dict[str, Any]:
        self._write_inputs(scope, event_key, slot, expected_version, source_ref, confirmed)
        if action == "put":
            _text(value, "invalid_value")
            if expires_at is not None:
                _aware(expires_at, "invalid_expiry")
        elif expected_version == 0:
            raise MemoryLabError("invalid_expected_version")
        payload = canonical_json({
            "action": action, "slot": slot, "value": value,
            "expected_version": expected_version, "source_ref": source_ref,
            "expires_at": None if expires_at is None else _utc_text(expires_at),
        })
        payload_hash = hashlib.sha256(payload.encode()).hexdigest()

        def operation() -> dict[str, Any]:
            replay = self._replay(scope, event_key, payload_hash)
            if replay is not None:
                return replay
            if action == "put" and expires_at is not None and _expired(expires_at, scope.now):
                raise MemoryLabError("invalid_expiry")
            row = self.db.execute(
                "SELECT memory_id,version,status FROM memories "
                "WHERE tenant_id=? AND owner_id=? AND slot=?",
                (scope.tenant_id, scope.actor_id, slot),
            ).fetchone()
            now = _utc_text(scope.now)
            if row is not None and row["status"] != "active":
                raise MemoryLabError("version_conflict")
            if expected_version == 0:
                if row is not None or action != "put":
                    raise MemoryLabError("version_conflict")
                memory_id = _opaque_id("mem")
                version = 1
                self.db.execute(
                    "INSERT INTO memories VALUES(?,?,?,?,?,'active',?,?,?,?)",
                    (memory_id, scope.tenant_id, scope.actor_id, slot, value, version,
                     source_ref, _utc_text(expires_at) if expires_at else None, now),
                )
                status: Status = "active"
            else:
                if row is None or row["version"] != expected_version:
                    raise MemoryLabError("version_conflict")
                if action == "delete" and row["status"] != "active":
                    raise MemoryLabError("version_conflict")
                memory_id, version = row["memory_id"], expected_version + 1
                status = "active" if action == "put" else "deleted"
                expiry = _utc_text(expires_at) if expires_at else None
                changed = self.db.execute(
                    "UPDATE memories SET value=?,status=?,version=?,source_ref=?,expires_at=?,updated_at=? "
                    "WHERE memory_id=? AND tenant_id=? AND owner_id=? AND version=?",
                    (value, status, version, source_ref, expiry, now, memory_id,
                     scope.tenant_id, scope.actor_id, expected_version),
                ).rowcount
                if changed != 1:
                    raise MemoryLabError("version_conflict")
            return self._finish(scope, event_key, payload_hash, memory_id, version, status)

        return self._transact(operation)

    def put_preference(self, scope: Scope, *, event_key: str, slot: str, value: str,
                       expected_version: int, source_ref: str, confirmed: bool,
                       expires_at: datetime | None = None) -> dict[str, Any]:
        """Create or correct one confirmed preference slot; deleted slots stay deleted."""
        return self._mutate(scope, action="put", event_key=event_key, slot=slot, value=value,
                            expected_version=expected_version, source_ref=source_ref,
                            confirmed=confirmed, expires_at=expires_at)

    def delete_preference(self, scope: Scope, *, event_key: str, slot: str,
                          expected_version: int, source_ref: str,
                          confirmed: bool) -> dict[str, Any]:
        """Write a versioned tombstone; this lab does not claim physical erasure."""
        return self._mutate(scope, action="delete", event_key=event_key, slot=slot, value=None,
                            expected_version=expected_version, source_ref=source_ref,
                            confirmed=confirmed, expires_at=None)

    def get_preference(self, scope: Scope, slot: str) -> dict[str, Any] | None:
        self._scope(scope)
        _identifier(slot, "invalid_slot")
        row = self.db.execute(
            "SELECT memory_id,slot,value,version,expires_at FROM memories "
            "WHERE tenant_id=? AND owner_id=? AND slot=? AND status='active'",
            (scope.tenant_id, scope.actor_id, slot),
        ).fetchone()
        if row is None:
            return None
        expiry = _parse_time(row["expires_at"])
        if expiry is not None and _expired(expiry, scope.now):
            return None
        return {key: row[key] for key in ("memory_id", "slot", "value", "version")}

    def _verify_projection(self, scope: Scope, candidate: ProjectionEntry) -> dict[str, Any] | None:
        row = self.db.execute(
            "SELECT memory_id,slot,value,version,status,expires_at FROM memories "
            "WHERE memory_id=? AND tenant_id=? AND owner_id=?",
            (candidate.memory_id, scope.tenant_id, scope.actor_id),
        ).fetchone()
        if row is None or row["status"] != "active" or candidate.status != "active":
            return None
        expiry = _parse_time(row["expires_at"])
        if expiry is not None and _expired(expiry, scope.now):
            return None
        if (row["version"], row["slot"], row["value"]) != (
            candidate.version, candidate.slot, candidate.value
        ):
            return None
        return {key: row[key] for key in ("memory_id", "slot", "value", "version")}

    def project_outbox_event(self, event_id: str, projection: "MemoryProjection",
                             delivered_at: datetime) -> dict[str, Any]:
        """Deliver at least once; projection versions make duplicate delivery harmless."""
        _identifier(event_id, "invalid_event_id")
        _aware(delivered_at, "invalid_delivery_time")
        row = self.db.execute("""
            SELECT o.memory_id,o.version event_version,o.status event_status,
              m.tenant_id,m.owner_id,m.slot,m.value,m.version current_version,
              m.status current_status,m.expires_at
            FROM outbox o JOIN memories m ON m.memory_id=o.memory_id WHERE o.event_id=?
        """, (event_id,)).fetchone()
        if row is None:
            raise MemoryLabError("outbox_event_not_found")
        if (row["event_version"], row["event_status"]) != (
            row["current_version"], row["current_status"]
        ):
            applied, reason = False, "superseded"
        else:
            entry = ProjectionEntry(
                row["memory_id"], row["tenant_id"], row["owner_id"], row["slot"],
                row["value"], row["current_version"], row["current_status"],
                _parse_time(row["expires_at"]),
            )
            applied, reason = projection.apply(entry), "current"
        self.db.execute("UPDATE outbox SET published_at=? WHERE event_id=?",
                        (_utc_text(delivered_at), event_id))
        return {"applied": applied, "reason": reason}

    def counts(self) -> dict[str, int]:
        def count(table: str, where: str = "") -> int:
            return self.db.execute(f"SELECT COUNT(*) FROM {table} {where}").fetchone()[0]

        return {"memory_rows": count("memories"),
                "idempotency_rows": count("idempotency_receipts"),
                "outbox_rows": count("outbox"),
                "tombstone_rows": count("memories", "WHERE status='deleted'")}


class MemoryProjection:
    """Tiny in-memory projection with version fences and authoritative readback."""

    def __init__(self):
        self._active: dict[str, ProjectionEntry] = {}
        self._fences: dict[str, int] = {}

    def apply(self, entry: ProjectionEntry) -> bool:
        if type(entry) is not ProjectionEntry or type(entry.version) is not int or entry.version < 1:
            raise MemoryLabError("invalid_projection_entry")
        if entry.status not in ("active", "deleted"):
            raise MemoryLabError("invalid_projection_entry")
        if entry.status == "active":
            _text(entry.value, "invalid_projection_entry")
        elif entry.value is not None:
            raise MemoryLabError("invalid_projection_entry")
        if entry.expires_at is not None:
            _aware(entry.expires_at, "invalid_projection_entry")
        previous = self._fences.get(entry.memory_id, 0)
        if entry.version < previous:
            return False
        if entry.version == previous:
            if self._active.get(entry.memory_id) == entry or (
                entry.status == "deleted" and entry.memory_id not in self._active
            ):
                return False
            raise MemoryLabError("projection_version_conflict")
        self._fences[entry.memory_id] = entry.version
        if entry.status == "deleted":
            self._active.pop(entry.memory_id, None)
        elif entry.status == "active" and entry.value is not None:
            self._active[entry.memory_id] = entry
        else:
            raise MemoryLabError("invalid_projection_entry")
        return True

    def retrieve(self, store: MemoryStore, scope: Scope, slot: str) -> dict[str, Any]:
        store._scope(scope)
        _identifier(slot, "invalid_slot")
        items, rejected = [], 0
        for candidate in sorted(self._active.values(), key=lambda item: item.memory_id):
            if candidate.slot != slot:
                continue
            verified = store._verify_projection(scope, candidate)
            if verified is None:
                rejected += 1
            else:
                items.append(verified)
        return {"items": items, "rejected_count": rejected}

    def fence_version(self, memory_id: str) -> int:
        return self._fences.get(memory_id, 0)


def run_demo() -> dict[str, Any]:
    connection = sqlite3.connect(":memory:")
    store, projection = MemoryStore(connection), MemoryProjection()
    now = datetime(2026, 11, 2, 9, tzinfo=timezone(timedelta(hours=8)))
    alice, bob = Scope("demo-company", "demo-alice", now), Scope("demo-company", "demo-bob", now)
    expiry = datetime(2026, 12, 1, tzinfo=timezone(timedelta(hours=8)))
    first_value = "默认简短清单，需要时展开。"
    corrected_value = "默认详细解释每一步。"

    def put(who: Scope, key: str, value: str, version: int, source: str):
        return store.put_preference(
            who, event_key=key, slot="response_style", value=value,
            expected_version=version, source_ref=source, confirmed=True, expires_at=expiry,
        )

    first = put(alice, "preference-001", first_value, 0, "turn-012")
    store.project_outbox_event(first["outbox_event_id"], projection, now)
    replay = put(alice, "preference-001", first_value, 0, "turn-012")
    delayed_v1 = ProjectionEntry(first["memory_id"], alice.tenant_id, alice.actor_id,
                                 "response_style", first_value, 1, "active", expiry)
    corrected = put(alice, "preference-002", corrected_value, 1, "turn-018")
    try:
        put(alice, "preference-stale", "过时写入", 1, "turn-013")
    except MemoryLabError as error:
        stale_error = error.code
    else:  # pragma: no cover
        stale_error = "missing_error"
    other = put(bob, "preference-101", "只给简短答案。", 0, "turn-004")
    store.project_outbox_event(other["outbox_event_id"], projection, now)
    store.project_outbox_event(corrected["outbox_event_id"], projection, now)
    isolated = projection.retrieve(store, alice, "response_style")
    at_expiry = projection.retrieve(store, Scope(alice.tenant_id, alice.actor_id, expiry),
                                    "response_style")
    deleted = store.delete_preference(
        alice, event_key="preference-003", slot="response_style", expected_version=2,
        source_ref="turn-025", confirmed=True,
    )
    before_delete_event = projection.retrieve(store, alice, "response_style")
    store.project_outbox_event(deleted["outbox_event_id"], projection, now)
    late_applied = projection.apply(delayed_v1)
    after_delete = projection.retrieve(store, alice, "response_style")
    columns = [row[1] for row in connection.execute("PRAGMA table_info(outbox)")]
    outbox_dump = canonical_json([dict(row) for row in connection.execute("SELECT * FROM outbox")])
    result = {
        "fixture": {"clock": now.isoformat(), "database": "sqlite_memory",
                    "model_called": False, "network_called": False,
                    "real_auth_performed": False, "projection_kind": "in_memory_exact_slot_demo"},
        "write": {"created_version": first["version"], "replay_same_receipt": replay == first,
                  "corrected_version": corrected["version"], "stale_write_error": stale_error},
        "isolation_and_expiry": {
            "visible_values": [item["value"] for item in isolated["items"]],
            "rejected_candidate_count": isolated["rejected_count"],
            "visible_at_expiry_boundary": bool(at_expiry["items"]),
        },
        "delete": {"deleted_version": deleted["version"],
                   "visible_before_delete_event": bool(before_delete_event["items"]),
                   "visible_after_delete": bool(after_delete["items"]),
                   "late_v1_projection_applied": late_applied,
                   "delete_fence_version": projection.fence_version(first["memory_id"])},
        "persistence": {**store.counts(), "outbox_columns": columns,
                        "outbox_contains_preference_text": any(
                            value in outbox_dump for value in (first_value, corrected_value))},
        "limits": ["no_free_text_extraction", "no_real_authorization", "no_vector_search",
                   "no_physical_erasure_claim", "no_distributed_concurrency_claim"],
    }
    assert stale_error == "version_conflict" and replay == first
    assert [item["value"] for item in isolated["items"]] == [corrected_value]
    assert not before_delete_event["items"] and not after_delete["items"] and not late_applied
    return result


def main() -> None:
    print(json.dumps(run_demo(), ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
