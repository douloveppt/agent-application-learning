import sqlite3
import unittest

from lab import KnowledgeStore, PipelineError, Scope


class IngestionTests(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(':memory:')
        self.store = KnowledgeStore(self.connection)
        self.scope = Scope('company-a')
        self.blocks = [{'text': 'VPN 客户端更新后重试。', 'page': 2, 'section': '排查'}]
        self.store.receive(self.scope, 'vpn', 1, 'raw-v1', 'kb://vpn')

    def tearDown(self):
        self.connection.close()

    def build(self, pipeline='p1'):
        return self.store.build(self.scope, 'vpn', 1, pipeline, self.blocks)

    def test_ready_candidate_is_not_visible_until_published(self):
        generation = self.build()
        self.assertEqual([], self.store.search(self.scope, 'VPN'))
        self.store.publish(self.scope, generation, None)
        result = self.store.search(self.scope, 'VPN')[0]
        self.assertEqual(('vpn', 'kb://vpn', 1, 2, '排查'),
                         tuple(result[k] for k in ('doc_id', 'source_uri', 'source_revision', 'page', 'section')))

    def test_receive_replay_and_conflicting_payload(self):
        self.assertEqual('replayed', self.store.receive(self.scope, 'vpn', 1, 'raw-v1', 'kb://vpn'))
        with self.assertRaisesRegex(PipelineError, 'revision_payload_conflict'):
            self.store.receive(self.scope, 'vpn', 1, 'other', 'kb://vpn')

    def test_new_revision_immediately_hides_old_publication(self):
        generation = self.build()
        self.store.publish(self.scope, generation, None)
        self.store.receive(self.scope, 'vpn', 2, 'raw-v2', 'kb://vpn')
        self.assertEqual([], self.store.search(self.scope, 'VPN'))
        with self.assertRaisesRegex(PipelineError, 'stale_revision'):
            self.store.publish(self.scope, generation, None)

    def test_stale_source_event_and_build_are_rejected(self):
        self.store.receive(self.scope, 'vpn', 2, 'raw-v2', 'kb://vpn')
        with self.assertRaisesRegex(PipelineError, 'stale_revision'):
            self.store.receive(self.scope, 'vpn', 1, 'raw-v1', 'kb://vpn')
        with self.assertRaisesRegex(PipelineError, 'stale_revision'):
            self.build()

    def test_quality_gate_does_not_leave_partial_candidates(self):
        for blocks in ([], [{'text': '', 'page': 1, 'section': 'x'}],
                       [{'text': 'VPN', 'page': None, 'section': 'x'}]):
            with self.assertRaisesRegex(PipelineError, 'quality_gate_failed'):
                self.store.build(self.scope, 'vpn', 1, 'p1', blocks)
        self.assertEqual(0, self.connection.execute('SELECT count(*) FROM generations').fetchone()[0])
        self.assertEqual([], self.store.search(self.scope, 'VPN'))

    def test_transaction_failure_rolls_back_generation_and_chunks(self):
        self.connection.execute("CREATE TRIGGER fail_chunks BEFORE INSERT ON chunks BEGIN SELECT RAISE(ABORT, 'disk simulation'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.build()
        self.assertEqual(0, self.connection.execute('SELECT count(*) FROM generations').fetchone()[0])
        self.assertEqual(0, self.connection.execute('SELECT count(*) FROM chunks').fetchone()[0])

    def test_delete_blocks_late_publish_build_and_receive(self):
        generation = self.build()
        self.assertEqual(2, self.store.delete(self.scope, 'vpn', 1))
        for operation in (lambda: self.store.publish(self.scope, generation, None), self.build,
                          lambda: self.store.receive(self.scope, 'vpn', 3, 'raw-v3', 'kb://vpn')):
            with self.assertRaisesRegex(PipelineError, 'document_deleted'):
                operation()
        self.assertEqual([], self.store.search(self.scope, 'VPN'))

    def test_tenant_scope_applies_to_reads_and_publication(self):
        generation = self.build()
        self.store.publish(self.scope, generation, None)
        other = Scope('company-b')
        self.assertEqual([], self.store.search(other, 'VPN'))
        with self.assertRaisesRegex(PipelineError, 'generation_not_found'):
            self.store.publish(other, generation, None)

    def test_identical_text_in_distinct_documents_keeps_distinct_identity(self):
        generation = self.build()
        self.store.publish(self.scope, generation, None)
        self.store.receive(self.scope, 'vpn-copy', 1, 'raw-v1', 'kb://vpn-copy')
        copy = self.store.build(self.scope, 'vpn-copy', 1, 'p1', self.blocks)
        self.store.publish(self.scope, copy, None)
        results = self.store.search(self.scope, 'VPN')
        self.assertEqual(2, len(results))
        self.assertNotEqual(results[0]['chunk_id'], results[1]['chunk_id'])

    def test_pipeline_rebuild_publication_uses_pointer_cas(self):
        first, second = self.build('p1'), self.build('p2')
        self.store.publish(self.scope, second, None)
        with self.assertRaisesRegex(PipelineError, 'publication_conflict'):
            self.store.publish(self.scope, first, None)
        self.assertEqual('p2', self.store.search(self.scope, 'VPN')[0]['pipeline_version'])

    def test_generation_replay_requires_identical_manifest(self):
        generation = self.build()
        self.assertEqual(generation, self.build())
        with self.assertRaisesRegex(PipelineError, 'generation_payload_conflict'):
            self.store.build(self.scope, 'vpn', 1, 'p1', self.blocks, max_chars=3)

    def test_chunk_offsets_reconstruct_exact_block(self):
        generation = self.store.build(self.scope, 'vpn', 1, 'p1', self.blocks, max_chars=3)
        self.store.publish(self.scope, generation, None)
        chunks = self.store.search(self.scope, '')
        self.assertEqual(self.blocks[0]['text'], ''.join(item['text'] for item in chunks))
        for item in chunks:
            self.assertEqual(item['text'], self.blocks[0]['text'][item['start_offset']:item['end_offset']])


if __name__ == '__main__':
    unittest.main()
