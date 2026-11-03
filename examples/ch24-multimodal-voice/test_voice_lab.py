import unittest

from voice_lab import Frame, OperationLedger, Player, StreamKey


class PlaybackTests(unittest.TestCase):
    def setUp(self):
        self.player = Player("session-A", max_buffer_ms=100)
        self.key = self.player.key("r-1")
        self.player.bind_response(self.key)

    def test_server_bound_identity_rejects_other_sessions_and_responses(self):
        for key in (
            StreamKey("session-B", 1, 1, "r-1"),
            self.player.key("r-2"),
        ):
            self.assertEqual(self.player.offer(Frame(key, 1, 20)), "stale_ignored")
        with self.assertRaises(ValueError):
            self.player.bind_response(StreamKey("session-B", 1, 1, "r-1"))
        self.assertEqual(self.player.response.received_ms, 0)

    def test_new_response_same_turn_fences_previous_response(self):
        self.player.offer(Frame(self.key, 1, 20))
        new_key = self.player.key("r-2")
        self.player.bind_response(new_key)
        self.assertEqual(self.player.offer(Frame(self.key, 2, 20)), "stale_ignored")
        self.assertEqual(self.player.offer(Frame(new_key, 1, 20)), "accepted")
        self.assertEqual(self.player.response.received_ms, 20)
        with self.assertRaises(ValueError):
            self.player.bind_response(self.key)

    def test_interrupt_flushes_queue_and_fences_delayed_callbacks(self):
        self.player.offer(Frame(self.key, 1, 20))
        self.player.take_next(self.key)
        self.player.offer(Frame(self.key, 2, 20))
        self.player.interrupt()
        self.assertEqual(self.player.response.queued_ms, 0)
        self.assertIsNone(self.player.take_next(self.key))
        self.assertEqual(self.player.offer(Frame(self.key, 3, 20)), "stale_ignored")
        self.assertEqual(self.player.ack_played(self.key, 20), "stale_ignored")
        self.assertEqual(self.player.generation_finished(self.key, 2), "stale_ignored")

    def test_new_turn_rejects_old_packets_and_stale_binding(self):
        self.player.new_turn()
        self.assertEqual(self.player.turn_id, 2)
        with self.assertRaises(ValueError):
            self.player.bind_response(self.key)
        self.player.bind_response(self.player.key("r-1"))
        self.assertEqual(self.player.offer(Frame(self.key, 1, 20)), "stale_ignored")

    def test_reconnect_rejects_old_epoch_even_if_response_id_reused(self):
        self.player.reconnect()
        with self.assertRaises(ValueError):
            self.player.bind_response(self.key)
        key = self.player.key("r-1")
        self.player.bind_response(key)
        self.assertEqual(self.player.offer(Frame(self.key, 1, 20)), "stale_ignored")
        self.assertEqual(self.player.offer(Frame(key, 1, 20)), "accepted")

    def test_duplicate_frames_do_not_consume_budget_twice(self):
        frame = Frame(self.key, 1, 40)
        self.assertEqual(self.player.offer(frame), "accepted")
        self.assertEqual(self.player.offer(frame), "duplicate_ignored")
        self.assertEqual(self.player.response.queued_ms, 40)
        self.assertEqual(self.player.offer(Frame(self.key, 1, 30)), "conflicting_duplicate")
        self.assertEqual(self.player.response.queued_ms, 0)

    def test_gap_stops_response_instead_of_joining_missing_audio(self):
        self.player.offer(Frame(self.key, 1, 20))
        self.assertEqual(self.player.offer(Frame(self.key, 3, 20)), "sequence_gap")
        self.assertEqual(self.player.response.queued_ms, 0)
        self.assertEqual(self.player.offer(Frame(self.key, 2, 20)), "stale_ignored")

    def test_overflow_clears_entire_response_and_blocks_future_audio(self):
        self.player.offer(Frame(self.key, 1, 80))
        self.assertEqual(self.player.offer(Frame(self.key, 2, 40)), "buffer_overflow")
        self.assertEqual(self.player.response.queued_ms, 0)
        self.assertIsNone(self.player.take_next(self.key))
        self.assertEqual(self.player.offer(Frame(self.key, 3, 20)), "stale_ignored")

    def test_generation_done_is_not_playback_done(self):
        self.player.offer(Frame(self.key, 1, 40))
        self.assertEqual(self.player.generation_finished(self.key, 1), "generation_done")
        self.assertFalse(self.player.response.playback_done)
        self.player.take_next(self.key)
        self.assertFalse(self.player.response.playback_done)
        self.assertEqual(self.player.ack_played(self.key, 40), "playback_completed")

    def test_playback_ack_is_monotonic_and_bounded_by_audio_sink_submission(self):
        self.player.offer(Frame(self.key, 1, 40))
        self.assertEqual(self.player.ack_played(self.key, 1), "invalid_playback_ack")
        self.player.take_next(self.key)
        self.assertEqual(self.player.ack_played(self.key, 41), "invalid_playback_ack")
        self.assertEqual(self.player.ack_played(self.key, 20), "acknowledged")
        self.assertEqual(self.player.ack_played(self.key, 19), "invalid_playback_ack")
        self.assertEqual(self.player.ack_played(self.key, 20), "acknowledged")
        self.assertEqual(self.player.response.played_ms, 20)

    def test_generation_finish_with_missing_audio_fails_closed(self):
        self.player.offer(Frame(self.key, 1, 20))
        self.assertEqual(self.player.generation_finished(self.key, 2), "incomplete_generation")
        self.assertFalse(self.player.response.playback_done)

    def test_new_audio_after_generation_finish_is_protocol_error(self):
        self.player.offer(Frame(self.key, 1, 20))
        self.player.generation_finished(self.key, 1)
        self.assertEqual(self.player.offer(Frame(self.key, 2, 20)), "audio_after_generation_done")

    def test_response_duration_budget_is_bounded_even_when_consumer_keeps_up(self):
        self.player.max_response_ms = 50
        self.player.offer(Frame(self.key, 1, 40))
        self.player.take_next(self.key)
        self.assertEqual(self.player.offer(Frame(self.key, 2, 20)), "response_budget_exceeded")


class OperationTests(unittest.TestCase):
    def setUp(self):
        self.ledger = OperationLedger()
        self.ledger.prepare("op-42", "approved body version 3")

    def test_interrupt_and_reconnect_never_replay_unknown_write(self):
        self.assertEqual(self.ledger.dispatch("op-42"), "dispatched_unknown")
        self.assertEqual(self.ledger.request_stop("op-42"), "reconciling")
        self.assertEqual(self.ledger.dispatch("op-42"), "not_dispatched_again")
        self.assertEqual(self.ledger.query_receipt("op-42", None), "reconciling")
        self.assertEqual(self.ledger.query_receipt("op-42", "ticket-8042"), "confirmed")
        operation = self.ledger.operations["op-42"]
        self.assertEqual(operation.dispatch_count, 1)
        self.assertEqual(operation.query_count, 2)
        self.assertEqual(operation.cancel_status, "too_late")

    def test_stop_never_erases_confirmed_receipt(self):
        self.ledger.dispatch("op-42")
        self.ledger.query_receipt("op-42", "ticket-8042")
        self.assertEqual(self.ledger.request_stop("op-42"), "confirmed")
        self.assertEqual(self.ledger.query_receipt("op-42", None), "confirmed")
        self.assertEqual(self.ledger.operations["op-42"].receipt_id, "ticket-8042")
        self.assertEqual(self.ledger.operations["op-42"].cancel_status, "too_late")

    def test_stopping_prepared_operation_prevents_dispatch(self):
        self.assertEqual(self.ledger.request_stop("op-42"), "cancelled")
        self.assertEqual(self.ledger.dispatch("op-42"), "not_dispatched_again")
        self.assertEqual(self.ledger.operations["op-42"].dispatch_count, 0)

    def test_operation_id_binds_payload_and_receipt(self):
        self.ledger.prepare("op-42", "approved body version 3")
        with self.assertRaises(ValueError):
            self.ledger.prepare("op-42", "changed body version 4")
        self.ledger.dispatch("op-42")
        self.ledger.query_receipt("op-42", "ticket-8042")
        with self.assertRaises(ValueError):
            self.ledger.query_receipt("op-42", "ticket-other")


if __name__ == "__main__":
    unittest.main()
