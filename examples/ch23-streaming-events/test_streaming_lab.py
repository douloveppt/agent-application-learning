"""Contract tests for the chapter's recovery and cancellation examples."""

from dataclasses import replace
import unittest

from streaming_lab import (
    CursorExpired, EventLog, FINGERPRINT_WINDOW, Projection, ProtocolError,
    apply_all, sample_log,
)


class StreamingContracts(unittest.TestCase):
    def test_duplicate_delta_does_not_append_twice(self):
        log = sample_log()
        client = Projection(log.run_id)
        apply_all(client, log.events[:5])
        self.assertEqual(client.apply(log.events[4]), "duplicate")
        self.assertEqual(client.view.text, "已找到 ")
        self.assertEqual(client.view.steps, {"search-policy": "completed"})

    def test_gap_preserves_cursor_then_replay_fills_missing_prefix(self):
        log = sample_log()
        client = Projection(log.run_id)
        apply_all(client, log.events[:3])
        self.assertEqual(client.apply(log.events[5]), "needs_replay")
        self.assertEqual(client.view.last_seq, 3)
        self.assertEqual(client.view.text, "")
        self.assertEqual(client.view.steps["search-policy"], "running")
        apply_all(client, log.replay(client.view.last_seq))
        self.assertEqual(client.view.status, "succeeded")
        self.assertIsNone(client.sync_required)
        self.assertEqual(log.last_seq, 8)  # Reconnect never executed the task again.

    def test_generated_success_text_does_not_complete_the_run(self):
        log = EventLog("run-text-is-not-proof")
        log.append("run.created")
        log.append("run.started")
        log.append("text.delta", delta="工单提交成功")
        client = Projection(log.run_id)
        apply_all(client, log.events)
        self.assertEqual(client.view.status, "running")
        self.assertIsNone(client.view.receipt_id)

    def test_expired_cursor_uses_snapshot_and_exact_tail(self):
        log = sample_log()
        authority = Projection(log.run_id)
        apply_all(authority, log.events[:6])
        snapshot = authority.snapshot()
        expected = Projection(log.run_id)
        apply_all(expected, log.events)
        log.prune_through(6)
        with self.assertRaises(CursorExpired):
            log.replay(2)
        recovered = Projection(log.run_id)
        recovered.install_snapshot(snapshot)
        apply_all(recovered, log.replay(snapshot["last_seq"]))
        self.assertEqual(recovered.view, expected.view)

    def test_incompatible_schema_blocks_progress_until_compatible_snapshot(self):
        log = sample_log()
        client = Projection(log.run_id)
        self.assertEqual(client.apply(replace(log.events[0], schema_version=2)), "needs_snapshot")
        self.assertEqual(client.view.last_seq, 0)
        self.assertEqual(client.apply(log.events[0]), "needs_snapshot")
        authority = Projection(log.run_id)
        apply_all(authority, log.events[:2])
        snapshot = authority.snapshot()
        with self.assertRaises(ProtocolError):
            client.install_snapshot({**snapshot, "schema_version": 2})
        client.install_snapshot(snapshot)
        apply_all(client, log.replay(client.view.last_seq))
        self.assertEqual(client.view.status, "succeeded")

    def test_wrong_run_and_conflicting_duplicate_do_not_advance(self):
        log = sample_log()
        wrong_run = Projection("run-other")
        self.assertEqual(wrong_run.apply(log.events[0]), "needs_snapshot")
        self.assertEqual(wrong_run.view.last_seq, 0)
        client = Projection(log.run_id)
        client.apply(log.events[0])
        conflicting = replace(log.events[0], event_id="different-event")
        self.assertEqual(client.apply(conflicting), "needs_snapshot")
        self.assertEqual(client.view.last_seq, 1)

    def test_late_text_cannot_reopen_or_mutate_terminal_result(self):
        log = sample_log()
        client = Projection(log.run_id)
        apply_all(client, log.events)
        text = client.view.text
        late = log.append("text.delta", delta="迟到内容")
        self.assertEqual(client.apply(late), "late_ignored")
        self.assertEqual(client.view.status, "succeeded")
        self.assertEqual(client.view.text, text)
        self.assertEqual(client.view.last_seq, late.seq)
        self.assertIn("终态后忽略", client.diagnostics[-1])

    def test_malformed_terminal_payload_never_advances_cursor(self):
        malformed = [
            ("text.delta", {}),
            ("text.delta", {"delta": 12}),
            ("text.snapshot", {"text": None}),
            ("step.started", {"step_id": False}),
            ("step.completed", {"step_id": "search-policy"}),
        ]
        for event_type, payload in malformed:
            with self.subTest(event_type=event_type, payload=payload):
                log = sample_log()
                client = Projection(log.run_id)
                apply_all(client, log.events)
                before = client.snapshot()
                bad = log.append(event_type, **payload)
                self.assertEqual(client.apply(bad), "needs_snapshot")
                self.assertEqual(client.snapshot(), before)

    def test_same_id_duplicate_with_changed_payload_is_a_conflict(self):
        log = sample_log()
        client = Projection(log.run_id)
        apply_all(client, log.events[:5])
        changed = replace(log.events[4], payload={"delta": "伪造的新内容"})
        self.assertEqual(client.apply(changed), "needs_snapshot")
        self.assertEqual(client.view.last_seq, 5)
        self.assertEqual(client.view.text, "已找到 ")

    def test_non_json_values_are_rejected_without_advancing(self):
        cycle = []
        cycle.append(cycle)
        invalid_values = [float("nan"), float("inf"), {"x"}, (1, 2), {1: "key"}, cycle]
        for value in invalid_values:
            with self.subTest(value_type=type(value).__name__):
                log = sample_log()
                client = Projection(log.run_id)
                event = replace(log.events[0], payload={"extra": value})
                self.assertEqual(client.apply(event), "needs_snapshot")
                self.assertEqual(client.view.last_seq, 0)

    def test_fingerprint_window_is_bounded_and_canonical_key_order_is_stable(self):
        log = EventLog("run-bounded-dedup")
        log.append("run.created", first="a", second="b")
        log.append("run.started")
        client = Projection(log.run_id)
        apply_all(client, log.events)
        reordered = replace(log.events[0], payload={"second": "b", "first": "a"})
        self.assertEqual(client.apply(reordered), "duplicate")
        for _ in range(FINGERPRINT_WINDOW + 3):
            client.apply(log.append("text.delta", delta="x"))
        self.assertEqual(len(client._seen_fingerprints), FINGERPRINT_WINDOW)
        self.assertNotIn(1, client._seen_fingerprints)
        # Outside the window, consistency relies on the immutable server log.
        self.assertEqual(client.apply(log.events[0]), "duplicate")

    def test_cancel_unknown_write_requires_reconciliation(self):
        log = EventLog("run-write")
        log.append("run.created")
        log.append("run.started")
        log.append("cancel.requested")
        log.append("run.status_changed", status="reconciling", operation_id="submit-ticket")
        client = Projection(log.run_id)
        apply_all(client, log.events)
        self.assertEqual(client.view.cancel_status, "requested")
        self.assertEqual(client.view.status, "reconciling")
        invalid = replace(log.events[-1], seq=5, event_id="evt-invalid", type="run.completed", payload={"status": "cancelled"})
        self.assertEqual(client.apply(invalid), "needs_snapshot")
        self.assertEqual(client.view.last_seq, 4)
        authority = Projection(log.run_id)
        apply_all(authority, log.events)
        client.install_snapshot(authority.snapshot())
        log.append("operation.reconciled", receipt_id="ticket-8042")
        log.append("run.completed", status="succeeded")
        apply_all(client, log.replay(client.view.last_seq))
        self.assertEqual(client.view.receipt_id, "ticket-8042")
        self.assertEqual(client.view.status, "succeeded")
        self.assertEqual(client.view.cancel_status, "too_late")

    def test_cancel_is_confirmed_when_no_unknown_operation_remains(self):
        log = EventLog("run-safe-cancel")
        for event_type in ["run.created", "run.started", "cancel.requested"]:
            log.append(event_type)
        log.append("run.completed", status="cancelled")
        client = Projection(log.run_id)
        apply_all(client, log.events)
        self.assertEqual(client.view.status, "cancelled")
        self.assertEqual(client.view.cancel_status, "confirmed")

    def test_snapshot_must_match_identity_and_cannot_move_cursor_backwards(self):
        log = sample_log()
        client = Projection(log.run_id)
        apply_all(client, log.events[:3])
        snapshot = client.snapshot()
        with self.assertRaises(ProtocolError):
            client.install_snapshot({**snapshot, "last_seq": 2})
        with self.assertRaises(ProtocolError):
            client.install_snapshot({**snapshot, "run_id": "another-run"})
        with self.assertRaises(ProtocolError):
            client.install_snapshot({key: value for key, value in snapshot.items() if key != "text"})
        self.assertEqual(client.view.last_seq, 3)


if __name__ == "__main__":
    unittest.main()
