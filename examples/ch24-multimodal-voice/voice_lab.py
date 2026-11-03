"""Chapter 24: offline control-plane simulation; no real media or external writes."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field


@dataclass(frozen=True)
class StreamKey:
    session_id: str
    connection_epoch: int
    turn_id: int
    response_id: str


@dataclass(frozen=True)
class Frame:
    key: StreamKey
    seq: int
    duration_ms: int


@dataclass
class Response:
    key: StreamKey
    queue: deque[Frame] = field(default_factory=deque)
    durations: dict[int, int] = field(default_factory=dict)
    received_ms: int = 0
    queued_ms: int = 0
    submitted_ms: int = 0
    played_ms: int = 0
    generation_done: bool = False
    stop_reason: str | None = None

    @property
    def playback_done(self) -> bool:
        return (
            self.stop_reason is None
            and self.generation_done
            and self.queued_ms == 0
            and self.played_ms == self.received_ms
        )


class Player:
    """One authenticated session and one current audio response.

    bind_response is called by the trusted session controller, never directly by
    a model or by arbitrary packet metadata. An audio adapter supplies frames.
    """

    def __init__(self, session_id: str, max_buffer_ms: int = 120):
        if not session_id or type(max_buffer_ms) is not int or max_buffer_ms < 1:
            raise ValueError("invalid session or buffer budget")
        self.session_id = session_id
        self.connection_epoch = 1
        self.turn_id = 1
        self.max_buffer_ms = max_buffer_ms
        self.max_response_ms = 30_000
        self.response: Response | None = None
        self._used_response_ids: set[str] = set()

    def key(self, response_id: str) -> StreamKey:
        """Convenience for the trusted controller in this single-threaded lab."""
        return StreamKey(
            self.session_id, self.connection_epoch, self.turn_id, response_id
        )

    def bind_response(self, key: StreamKey) -> None:
        if (
            key.session_id != self.session_id
            or key.connection_epoch != self.connection_epoch
            or key.turn_id != self.turn_id
            or not key.response_id
            or key.response_id in self._used_response_ids
            or len(self._used_response_ids) >= 16
        ):
            raise ValueError("stale, reused or excessive response binding")
        self.interrupt("replaced")
        self._used_response_ids.add(key.response_id)
        self.response = Response(key)

    def _current(self, key: StreamKey) -> Response | None:
        response = self.response
        if response is None or response.key != key or response.stop_reason:
            return None
        return response

    def interrupt(self, reason: str = "interrupted") -> None:
        """Synchronous local fence and flush, before any remote cancellation."""
        if self.response is not None:
            self.response.stop_reason = self.response.stop_reason or reason
            self.response.queue.clear()
            self.response.queued_ms = 0

    def new_turn(self) -> None:
        self.interrupt("new_turn")
        self.turn_id += 1
        self._used_response_ids.clear()
        self.response = None

    def reconnect(self) -> None:
        self.interrupt("reconnected")
        self.connection_epoch += 1
        self._used_response_ids.clear()
        self.response = None

    def offer(self, frame: Frame) -> str:
        response = self._current(frame.key)
        if response is None:
            return "stale_ignored"
        if (
            type(frame.seq) is not int
            or frame.seq < 1
            or type(frame.duration_ms) is not int
            or not 1 <= frame.duration_ms <= 200
        ):
            self.interrupt("invalid_frame")
            return "invalid_frame"
        if frame.seq in response.durations:
            if response.durations[frame.seq] == frame.duration_ms:
                return "duplicate_ignored"
            self.interrupt("conflicting_duplicate")
            return "conflicting_duplicate"
        if frame.seq != len(response.durations) + 1:
            self.interrupt("sequence_gap")
            return "sequence_gap"
        if response.generation_done:
            self.interrupt("audio_after_generation_done")
            return "audio_after_generation_done"
        if response.received_ms + frame.duration_ms > self.max_response_ms:
            self.interrupt("response_budget_exceeded")
            return "response_budget_exceeded"
        if response.queued_ms + frame.duration_ms > self.max_buffer_ms:
            self.interrupt("buffer_overflow")
            return "buffer_overflow"
        response.queue.append(frame)
        response.durations[frame.seq] = frame.duration_ms
        response.received_ms += frame.duration_ms
        response.queued_ms += frame.duration_ms
        return "accepted"

    def take_next(self, key: StreamKey) -> Frame | None:
        """Hand one frame to the simulated audio sink, not a playback receipt."""
        response = self._current(key)
        if response is None or not response.queue:
            return None
        frame = response.queue.popleft()
        response.queued_ms -= frame.duration_ms
        response.submitted_ms += frame.duration_ms
        return frame

    def ack_played(self, key: StreamKey, offset_ms: int) -> str:
        response = self._current(key)
        if response is None:
            return "stale_ignored"
        if (
            type(offset_ms) is not int
            or not response.played_ms <= offset_ms <= response.submitted_ms
        ):
            return "invalid_playback_ack"
        response.played_ms = offset_ms
        return "playback_completed" if response.playback_done else "acknowledged"

    def generation_finished(self, key: StreamKey, last_seq: int) -> str:
        response = self._current(key)
        if response is None:
            return "stale_ignored"
        if type(last_seq) is not int or last_seq != len(response.durations):
            self.interrupt("incomplete_generation")
            return "incomplete_generation"
        response.generation_done = True
        return "playback_completed" if response.playback_done else "generation_done"


@dataclass
class Operation:
    payload: str
    status: str = "prepared"
    dispatch_count: int = 0
    query_count: int = 0
    cancel_status: str = "none"
    receipt_id: str | None = None


class OperationLedger:
    """Separate business state. Memory only; dispatch simulates an external write."""

    def __init__(self):
        self.operations: dict[str, Operation] = {}

    def prepare(self, operation_id: str, approved_payload: str) -> None:
        if not operation_id or not approved_payload:
            raise ValueError("empty operation")
        existing = self.operations.get(operation_id)
        if existing is not None and existing.payload != approved_payload:
            raise ValueError("operation id is bound to another approved payload")
        if existing is None:
            self.operations[operation_id] = Operation(approved_payload)

    def dispatch(self, operation_id: str) -> str:
        operation = self.operations[operation_id]
        if operation.status != "prepared":
            return "not_dispatched_again"
        operation.dispatch_count += 1
        operation.status = "dispatched_unknown"
        return "dispatched_unknown"

    def request_stop(self, operation_id: str) -> str:
        operation = self.operations[operation_id]
        if operation.status == "prepared":
            operation.status, operation.cancel_status = "cancelled", "confirmed"
        elif operation.status == "confirmed":
            operation.cancel_status = "too_late"
        elif operation.status != "cancelled":
            operation.status, operation.cancel_status = "reconciling", "requested"
        return operation.status

    def query_receipt(self, operation_id: str, receipt_id: str | None) -> str:
        operation = self.operations[operation_id]
        if operation.status not in {"dispatched_unknown", "reconciling", "confirmed"}:
            raise ValueError("no dispatched operation to reconcile")
        if operation.receipt_id is not None:
            if receipt_id is not None and operation.receipt_id != receipt_id:
                raise ValueError("conflicting receipt")
            return "confirmed"
        operation.query_count += 1
        operation.status = "reconciling"
        if receipt_id is not None:
            if not receipt_id:
                raise ValueError("empty receipt")
            operation.receipt_id = receipt_id
            operation.status = "confirmed"
            if operation.cancel_status == "requested":
                operation.cancel_status = "too_late"
        return operation.status


def main() -> None:
    player = Player("session-A")
    key = player.key("response-1")
    player.bind_response(key)
    player.offer(Frame(key, 1, 40))
    print("1 生成结束：", player.generation_finished(key, 1), sep="")
    player.take_next(key)
    print("  播放确认：", player.ack_played(key, 40), sep="")

    player.new_turn()
    key = player.key("response-2")
    player.bind_response(key)
    player.offer(Frame(key, 1, 40))
    player.interrupt()
    print("2 用户打断：queued_ms=", player.response.queued_ms,
          "，迟到音频=", player.offer(Frame(key, 2, 40)), sep="")

    player.new_turn()
    key = player.key("response-3")
    player.bind_response(key)
    player.reconnect()
    print("3 连接重建：epoch=", player.connection_epoch,
          "，旧连接音频=", player.offer(Frame(key, 1, 40)), sep="")

    key = player.key("response-4")
    player.bind_response(key)
    player.offer(Frame(key, 1, 100))
    result = player.offer(Frame(key, 2, 40))
    print("4 队列溢出：", result, "，queued_ms=", player.response.queued_ms, sep="")

    ledger = OperationLedger()
    ledger.prepare("op-42", "已批准：为打印机 P-8 创建报障工单")
    print("5 写入结果：", ledger.dispatch("op-42"), sep="")
    print("  打断后：", ledger.request_stop("op-42"), sep="")
    print("  重连重试：", ledger.dispatch("op-42"), sep="")
    print("  只读核实：", ledger.query_receipt("op-42", "ticket-8042"), sep="")
    operation = ledger.operations["op-42"]
    print("  dispatch_count=", operation.dispatch_count,
          "，receipt_id=", operation.receipt_id,
          "，cancel_status=", operation.cancel_status, sep="")


if __name__ == "__main__":
    main()
