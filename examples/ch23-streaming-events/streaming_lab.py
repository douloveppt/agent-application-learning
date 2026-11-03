#!/usr/bin/env python3
"""Offline teaching model for an ordered event stream and a client projection.

No HTTP server, database, real business writes, or third-party dependencies.
"""

from __future__ import annotations

import argparse
from collections import OrderedDict
from copy import deepcopy
from dataclasses import asdict, dataclass, field
import hashlib
import json
import math


SCHEMA_VERSION = 1
FINGERPRINT_WINDOW = 128
TERMINAL = {"succeeded", "partially_succeeded", "failed", "cancelled"}
STATUSES = {"idle", "queued", "running", "waiting_approval", "reconciling"} | TERMINAL
KNOWN_TYPES = {
    "run.created", "run.started", "step.started", "step.completed",
    "text.delta", "text.snapshot", "run.status_changed", "cancel.requested",
    "operation.reconciled", "run.completed",
}


class ProtocolError(ValueError):
    """The stream cannot safely update this projection."""


class CursorExpired(ValueError):
    """The required prefix is no longer retained; obtain a snapshot."""


@dataclass(frozen=True)
class Event:
    run_id: str
    seq: int
    event_id: str
    type: str
    schema_version: int
    payload: dict


@dataclass
class View:
    run_id: str
    last_seq: int = 0
    status: str = "idle"
    text: str = ""
    steps: dict[str, str] = field(default_factory=dict)
    cancel_status: str = "none"
    operation_outcome: str = "none"
    receipt_id: str | None = None


class EventLog:
    """An in-memory stand-in for one authorized, ordered public event stream."""

    def __init__(self, run_id: str):
        self.run_id = run_id
        self.last_seq = 0
        self.retained_after = 0
        self.events: list[Event] = []

    def append(self, event_type: str, **payload) -> Event:
        self.last_seq += 1
        event = Event(self.run_id, self.last_seq, f"evt-{self.last_seq:04d}",
                      event_type, SCHEMA_VERSION, deepcopy(payload))
        self.events.append(event)
        return event

    def replay(self, after: int) -> list[Event]:
        if after < self.retained_after:
            raise CursorExpired(f"cursor {after} 已过期，需要 snapshot")
        if after > self.last_seq or after < 0:
            raise ProtocolError("cursor 不在该流的有效范围内")
        return [event for event in self.events if event.seq > after]

    def prune_through(self, seq: int) -> None:
        if not self.retained_after <= seq <= self.last_seq:
            raise ProtocolError("无效的保留水位")
        self.events = [event for event in self.events if event.seq > seq]
        self.retained_after = seq


def required_text(payload: dict, key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise ProtocolError(f"缺少有效字段 {key}")
    return value


def validate_json_value(value, ancestors: set[int] | None = None, depth: int = 0) -> None:
    """Reject Python-only values, cycles, non-finite numbers and excessive depth."""
    if depth > 32:
        raise ProtocolError("JSON 嵌套超过实验上限 32 层")
    if value is None or type(value) in {bool, int}:
        return
    if type(value) is str:
        try:
            value.encode("utf-8")
        except UnicodeError as error:
            raise ProtocolError("JSON 字符串不是有效 Unicode") from error
        return
    if type(value) is float:
        if not math.isfinite(value):
            raise ProtocolError("JSON 不接受 NaN 或 Infinity")
        return
    if type(value) not in {dict, list}:
        raise ProtocolError("事件包含非法 JSON 字段值")
    ancestors = set() if ancestors is None else ancestors
    identity = id(value)
    if identity in ancestors:
        raise ProtocolError("JSON 不允许循环引用")
    ancestors.add(identity)
    try:
        if type(value) is dict:
            for key, item in value.items():
                if type(key) is not str:
                    raise ProtocolError("JSON 对象的键必须是字符串")
                validate_json_value(key, ancestors, depth + 1)
                validate_json_value(item, ancestors, depth + 1)
        else:
            for item in value:
                validate_json_value(item, ancestors, depth + 1)
    finally:
        ancestors.remove(identity)


def event_fingerprint(event: Event) -> str:
    """Validate structure without state changes, then fingerprint all fields."""
    data = {
        "run_id": event.run_id, "seq": event.seq, "event_id": event.event_id,
        "type": event.type, "schema_version": event.schema_version, "payload": event.payload,
    }
    validate_json_value(data)
    payload = event.payload
    if event.type in {"step.started", "step.completed"}:
        required_text(payload, "step_id")
        if event.type == "step.completed":
            required_text(payload, "evidence_id")
    elif event.type == "text.delta":
        required_text(payload, "delta")
    elif event.type == "text.snapshot":
        if not isinstance(payload.get("text"), str):
            raise ProtocolError("text.snapshot 必须包含完整 text")
    elif event.type == "run.status_changed":
        status = payload.get("status")
        if status in ("running", "waiting_approval"):
            required_text(payload, "action_id")
        elif status == "reconciling":
            required_text(payload, "operation_id")
        else:
            raise ProtocolError("中间状态字段无效")
    elif event.type == "operation.reconciled":
        required_text(payload, "receipt_id")
    elif event.type == "run.completed":
        status = payload.get("status")
        if not isinstance(status, str) or status not in TERMINAL:
            raise ProtocolError("终态字段无效")
        if status == "failed":
            required_text(payload, "error_code")
    try:
        canonical = json.dumps(data, sort_keys=True, separators=(",", ":"),
                               ensure_ascii=False, allow_nan=False)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    except (TypeError, ValueError, UnicodeError, RecursionError) as error:
        raise ProtocolError("事件无法编码为规范 JSON") from error


class Projection:
    """A reducer: business state comes only from accepted events/snapshots."""

    def __init__(self, run_id: str):
        self.view = View(run_id)
        self.sync_required: str | None = None
        self.reason: str | None = None
        self._replay_until = 0
        self._seen_fingerprints: OrderedDict[int, str] = OrderedDict()
        self.diagnostics: list[str] = []

    def _needs_snapshot(self, reason: str) -> str:
        self.sync_required = "snapshot"
        self.reason = reason
        self.diagnostics.append(reason)
        return "needs_snapshot"

    def apply(self, event: Event) -> str:
        if self.sync_required == "snapshot":
            return "needs_snapshot"
        if event.run_id != self.view.run_id:
            return self._needs_snapshot("run_id 不匹配")
        if type(event.schema_version) is not int or event.schema_version != SCHEMA_VERSION:
            return self._needs_snapshot("schema_version 不兼容")
        if not isinstance(event.seq, int) or isinstance(event.seq, bool) or event.seq < 1:
            return self._needs_snapshot("seq 无效")
        if not isinstance(event.event_id, str) or not event.event_id:
            return self._needs_snapshot("event_id 无效")
        if not isinstance(event.type, str) or event.type not in KNOWN_TYPES or not isinstance(event.payload, dict):
            return self._needs_snapshot("事件类型或 payload 不兼容")
        try:
            fingerprint = event_fingerprint(event)
        except ProtocolError as error:
            return self._needs_snapshot(str(error))
        if event.seq <= self.view.last_seq:
            known = self._seen_fingerprints.get(event.seq)
            if known is not None and known != fingerprint:
                return self._needs_snapshot("同一 seq 对应冲突的事件内容")
            return "duplicate"
        if event.seq != self.view.last_seq + 1:
            self.sync_required = "replay"
            self._replay_until = max(self._replay_until, event.seq)
            self.reason = f"期望 {self.view.last_seq + 1}，收到 {event.seq}"
            return "needs_replay"

        candidate = deepcopy(self.view)
        try:
            if candidate.status in TERMINAL:
                if event.type not in {"text.delta", "text.snapshot", "step.started", "step.completed"}:
                    raise ProtocolError("终态后出现新的状态变化")
                self.diagnostics.append(f"终态后忽略 seq={event.seq} 的 {event.type}")
                result = "late_ignored"
            else:
                self._reduce(candidate, event)
                result = "applied"
        except ProtocolError as error:
            return self._needs_snapshot(str(error))

        candidate.last_seq = event.seq
        self.view = candidate
        self._seen_fingerprints[event.seq] = fingerprint
        if len(self._seen_fingerprints) > FINGERPRINT_WINDOW:
            self._seen_fingerprints.popitem(last=False)
        if self.view.last_seq >= self._replay_until:
            self.sync_required = None
            self.reason = None
        return result

    @staticmethod
    def _reduce(view: View, event: Event) -> None:
        payload = event.payload
        if event.type == "run.created":
            if view.status != "idle":
                raise ProtocolError("run.created 只能创建一次")
            view.status = "queued"
        elif event.type == "run.started":
            if view.status != "queued":
                raise ProtocolError("只有 queued 可以开始执行")
            view.status = "running"
        elif view.status == "idle":
            raise ProtocolError("流必须从 run.created 开始")
        elif event.type == "step.started":
            step_id = required_text(payload, "step_id")
            if view.status != "running" or step_id in view.steps:
                raise ProtocolError("步骤不能重复开始或在非 running 状态开始")
            view.steps[step_id] = "running"
        elif event.type == "step.completed":
            step_id = required_text(payload, "step_id")
            required_text(payload, "evidence_id")
            if view.steps.get(step_id) != "running":
                raise ProtocolError("完成事件必须对应已开始的步骤")
            view.steps[step_id] = "completed"
        elif event.type == "text.delta":
            view.text += required_text(payload, "delta")
        elif event.type == "text.snapshot":
            value = payload.get("text")
            if not isinstance(value, str):
                raise ProtocolError("text.snapshot 必须包含完整 text")
            view.text = value
        elif event.type == "run.status_changed":
            target = payload.get("status")
            if target == "waiting_approval" and view.status == "running":
                required_text(payload, "action_id")
                view.status = target
            elif target == "running" and view.status == "waiting_approval":
                required_text(payload, "action_id")
                view.status = target
            elif target == "reconciling" and view.status == "running":
                required_text(payload, "operation_id")
                view.operation_outcome = "unknown"
                view.status = target
            else:
                raise ProtocolError("不合法的中间状态变化")
        elif event.type == "cancel.requested":
            view.cancel_status = "requested"
        elif event.type == "operation.reconciled":
            if view.status != "reconciling":
                raise ProtocolError("没有待核对的操作")
            view.receipt_id = required_text(payload, "receipt_id")
            view.operation_outcome = "known"
        elif event.type == "run.completed":
            status = payload.get("status")
            if not isinstance(status, str) or status not in TERMINAL or view.operation_outcome == "unknown":
                raise ProtocolError("缺少确定的终态结果")
            if view.status == "reconciling" and not view.receipt_id:
                raise ProtocolError("核对完成需要回执")
            if status == "cancelled":
                if view.cancel_status != "requested" or view.receipt_id:
                    raise ProtocolError("取消尚未确认，或已确认提交")
                view.cancel_status = "confirmed"
            elif view.cancel_status == "requested":
                view.cancel_status = "too_late"
            if status == "failed":
                required_text(payload, "error_code")
            view.status = status

    def snapshot(self) -> dict:
        """Capture this projection and its exact watermark together."""
        return {"schema_version": SCHEMA_VERSION, **deepcopy(asdict(self.view))}

    def install_snapshot(self, snapshot: dict) -> None:
        if not isinstance(snapshot, dict):
            raise ProtocolError("快照必须是对象")
        if type(snapshot.get("schema_version")) is not int or snapshot.get("schema_version") != SCHEMA_VERSION:
            raise ProtocolError("客户端无法理解此快照，需兼容版本或升级")
        if snapshot.get("run_id") != self.view.run_id:
            raise ProtocolError("快照的 run_id 不匹配")
        last_seq = snapshot.get("last_seq")
        if not isinstance(last_seq, int) or isinstance(last_seq, bool) or last_seq < self.view.last_seq:
            raise ProtocolError("快照水位不能倒退")
        try:
            data = {key: deepcopy(snapshot[key]) for key in View.__dataclass_fields__}
        except KeyError as error:
            raise ProtocolError(f"快照缺少字段 {error.args[0]}") from error
        candidate = View(**data)
        if not isinstance(candidate.status, str) or candidate.status not in STATUSES or not isinstance(candidate.text, str):
            raise ProtocolError("快照状态或文本无效")
        if not isinstance(candidate.steps, dict) or any(
            not isinstance(key, str) or not isinstance(value, str) or value not in {"running", "completed"}
            for key, value in candidate.steps.items()
        ):
            raise ProtocolError("快照步骤无效")
        if not isinstance(candidate.cancel_status, str) or candidate.cancel_status not in {"none", "requested", "confirmed", "too_late"}:
            raise ProtocolError("快照取消状态无效")
        if not isinstance(candidate.operation_outcome, str) or candidate.operation_outcome not in {"none", "unknown", "known"}:
            raise ProtocolError("快照操作结果无效")
        if candidate.receipt_id is not None and not isinstance(candidate.receipt_id, str):
            raise ProtocolError("快照回执无效")
        if candidate.status in TERMINAL and candidate.operation_outcome == "unknown":
            raise ProtocolError("终态不能掩盖未知写入结果")
        self.view = candidate
        self.sync_required = None
        self.reason = None
        self._replay_until = 0
        self._seen_fingerprints.clear()


def sample_log() -> EventLog:
    log = EventLog("run-vpn-001")
    log.append("run.created")
    log.append("run.started")
    log.append("step.started", step_id="search-policy")
    log.append("step.completed", step_id="search-policy", evidence_id="doc-vpn-v3")
    log.append("text.delta", delta="已找到 ")
    log.append("text.delta", delta="VPN 排查指引。")
    log.append("text.snapshot", text="已找到 VPN 排查指引。")
    log.append("run.completed", status="succeeded")
    return log


def apply_all(client: Projection, events: list[Event]) -> None:
    for event in events:
        result = client.apply(event)
        if result not in {"applied", "duplicate", "late_ignored"}:
            raise ProtocolError(f"应用 seq={event.seq} 失败：{result}: {client.reason}")


def demo() -> None:
    log = sample_log()
    normal = Projection(log.run_id)
    apply_all(normal, log.replay(0))
    print(f"1 正常：{normal.view.status}，last_seq={normal.view.last_seq}，文本={normal.view.text}")

    duplicate = Projection(log.run_id)
    apply_all(duplicate, log.events[:5])
    result = duplicate.apply(log.events[4])
    print(f"2 重复：seq=5 -> {result}，文本={duplicate.view.text!r}")

    reconnect = Projection(log.run_id)
    apply_all(reconnect, log.events[:3])
    result = reconnect.apply(log.events[5])
    cursor = reconnect.view.last_seq
    apply_all(reconnect, log.replay(cursor))
    print(f"3 断线：收到 seq=6 -> {result}，从 cursor={cursor} 回放；最终 {reconnect.view.status}")

    authority = Projection(log.run_id)
    apply_all(authority, log.events[:6])
    snapshot = authority.snapshot()  # Snapshot state and H=6 belong together.
    log.prune_through(6)
    try:
        log.replay(2)
    except CursorExpired:
        recovered = Projection(log.run_id)
        recovered.install_snapshot(snapshot)
        apply_all(recovered, log.replay(snapshot["last_seq"]))
        print(f"4 过期：cursor=2 -> snapshot H={snapshot['last_seq']} + events>H；文本={recovered.view.text}")

    late = log.append("text.delta", delta="迟到的内容")
    before = normal.view.text
    result = normal.apply(late)
    assert normal.view.text == before
    print(f"5 终态：迟到文本 -> {result}，状态仍是 {normal.view.status}")

    race = EventLog("run-write-001")
    race.append("run.created")
    race.append("run.started")
    race.append("cancel.requested")
    race.append("run.status_changed", status="reconciling", operation_id="submit-ticket-001")
    client = Projection(race.run_id)
    apply_all(client, race.replay(0))
    print(f"6 取消竞态：cancel_status={client.view.cancel_status}，状态={client.view.status}；只查询回执，不重发写入")
    race.append("operation.reconciled", receipt_id="ticket-8042")
    race.append("run.completed", status="succeeded")
    apply_all(client, race.replay(client.view.last_seq))
    print(f"  核对后：{client.view.status}，receipt_id={client.view.receipt_id}，cancel_status={client.view.cancel_status}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--events", action="store_true", help="输出示例事件 JSON，便于对照字段")
    args = parser.parse_args()
    if args.events:
        for event in sample_log().events:
            print(json.dumps(asdict(event), ensure_ascii=False))
    else:
        demo()


if __name__ == "__main__":
    main()
