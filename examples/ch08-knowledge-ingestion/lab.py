"""Deterministic ingestion lifecycle lab; no network, models or real parser."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import sqlite3


def fingerprint(value: object) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':')).encode()).hexdigest()


class PipelineError(ValueError):
    pass


@dataclass(frozen=True)
class Scope:
    tenant_id: str  # Injected by trusted test code, not a real authentication layer.


class KnowledgeStore:
    def __init__(self, connection: sqlite3.Connection):
        self.db = connection
        self.db.isolation_level = None
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA foreign_keys = ON')
        self.db.executescript('''
          CREATE TABLE documents (
            tenant TEXT, doc_id TEXT, desired_revision INTEGER NOT NULL,
            source_hash TEXT NOT NULL, source_uri TEXT NOT NULL,
            deleted INTEGER NOT NULL DEFAULT 0, active_generation TEXT,
            PRIMARY KEY (tenant, doc_id));
          CREATE TABLE generations (
            generation_id TEXT PRIMARY KEY, tenant TEXT, doc_id TEXT,
            source_revision INTEGER, source_hash TEXT, pipeline_version TEXT,
            manifest_hash TEXT, state TEXT NOT NULL,
            FOREIGN KEY (tenant, doc_id) REFERENCES documents(tenant, doc_id));
          CREATE TABLE chunks (
            chunk_id TEXT PRIMARY KEY, generation_id TEXT NOT NULL,
            text TEXT NOT NULL, page INTEGER NOT NULL, section TEXT NOT NULL,
            block_index INTEGER NOT NULL, start_offset INTEGER NOT NULL,
            end_offset INTEGER NOT NULL,
            FOREIGN KEY (generation_id) REFERENCES generations(generation_id));
        ''')

    @contextmanager
    def transaction(self):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            yield
            self.db.execute('COMMIT')
        except BaseException:
            self.db.execute('ROLLBACK')
            raise

    def document(self, scope: Scope, doc_id: str):
        row = self.db.execute('SELECT * FROM documents WHERE tenant=? AND doc_id=?',
                              (scope.tenant_id, doc_id)).fetchone()
        if row is None:
            raise PipelineError('document_not_found')
        return row

    def receive(self, scope: Scope, doc_id: str, revision: int,
                raw_content: str, source_uri: str) -> str:
        if type(revision) is not int or revision < 1 or not raw_content or not source_uri:
            raise PipelineError('invalid_source')
        digest = fingerprint(raw_content)
        with self.transaction():
            row = self.db.execute('SELECT * FROM documents WHERE tenant=? AND doc_id=?',
                                  (scope.tenant_id, doc_id)).fetchone()
            if row is not None:
                if row['deleted']:
                    raise PipelineError('document_deleted')
                if revision < row['desired_revision']:
                    raise PipelineError('stale_revision')
                if revision == row['desired_revision']:
                    if (digest, source_uri) != (row['source_hash'], row['source_uri']):
                        raise PipelineError('revision_payload_conflict')
                    return 'replayed'
            self.db.execute('''INSERT INTO documents VALUES (?, ?, ?, ?, ?, 0, NULL)
              ON CONFLICT(tenant, doc_id) DO UPDATE SET
                desired_revision=excluded.desired_revision,
                source_hash=excluded.source_hash, source_uri=excluded.source_uri,
                active_generation=NULL''',
                (scope.tenant_id, doc_id, revision, digest, source_uri))
        return 'accepted'

    def build(self, scope: Scope, doc_id: str, source_revision: int,
              pipeline_version: str, blocks: list[dict], max_chars: int = 120) -> str:
        # Blocks are already parsed and trusted as belonging to this source version.
        # This lab does not validate PDF/OCR fidelity or use a model tokenizer.
        if not pipeline_version or type(max_chars) is not int or max_chars < 1:
            raise PipelineError('invalid_pipeline')
        if not blocks:
            raise PipelineError('quality_gate_failed')
        prepared = []
        for block_index, block in enumerate(blocks):
            text, page, section = block.get('text'), block.get('page'), block.get('section')
            if (not isinstance(text, str) or not text.strip() or type(page) is not int
                    or page < 1 or not isinstance(section, str) or not section.strip()):
                raise PipelineError('quality_gate_failed')
            for start in range(0, len(text), max_chars):
                prepared.append((text[start:start + max_chars], page, section,
                                 block_index, start, min(start + max_chars, len(text))))
        generation = fingerprint([scope.tenant_id, doc_id, source_revision, pipeline_version])
        manifest = fingerprint([blocks, max_chars])
        with self.transaction():
            doc = self.document(scope, doc_id)
            self._check_current(doc, source_revision)
            existing = self.db.execute('SELECT * FROM generations WHERE generation_id=?',
                                       (generation,)).fetchone()
            if existing:
                if existing['manifest_hash'] != manifest:
                    raise PipelineError('generation_payload_conflict')
                return generation
            self.db.execute('INSERT INTO generations VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                            (generation, scope.tenant_id, doc_id, source_revision,
                             doc['source_hash'], pipeline_version, manifest, 'ready'))
            self.db.executemany('INSERT INTO chunks VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                               [(fingerprint([generation, i]), generation, *chunk)
                                for i, chunk in enumerate(prepared)])
        return generation

    @staticmethod
    def _check_current(doc, revision: int):
        if doc['deleted']:
            raise PipelineError('document_deleted')
        if doc['desired_revision'] != revision:
            raise PipelineError('stale_revision')

    def publish(self, scope: Scope, generation: str,
                expected_active_generation: str | None) -> str:
        with self.transaction():
            candidate = self.db.execute('SELECT * FROM generations WHERE generation_id=? AND tenant=?',
                                        (generation, scope.tenant_id)).fetchone()
            if candidate is None:
                raise PipelineError('generation_not_found')
            doc = self.document(scope, candidate['doc_id'])
            self._check_current(doc, candidate['source_revision'])
            if doc['source_hash'] != candidate['source_hash'] or candidate['state'] != 'ready':
                raise PipelineError('candidate_not_ready')
            if doc['active_generation'] == generation:
                return 'replayed'
            if doc['active_generation'] != expected_active_generation:
                raise PipelineError('publication_conflict')
            self.db.execute('UPDATE documents SET active_generation=? WHERE tenant=? AND doc_id=?',
                            (generation, scope.tenant_id, candidate['doc_id']))
        return 'published'

    def delete(self, scope: Scope, doc_id: str, expected_revision: int) -> int:
        with self.transaction():
            doc = self.document(scope, doc_id)
            self._check_current(doc, expected_revision)
            revision = expected_revision + 1
            self.db.execute('''UPDATE documents SET desired_revision=?, deleted=1,
              active_generation=NULL WHERE tenant=? AND doc_id=?''',
                            (revision, scope.tenant_id, doc_id))
        return revision

    def search(self, scope: Scope, literal: str) -> list[dict]:
        # Candidate lookup and authoritative eligibility checks share one SQL statement.
        # Literal matching demonstrates lifecycle visibility, not semantic retrieval.
        rows = self.db.execute('''
          SELECT d.doc_id, d.source_uri, g.source_revision, g.pipeline_version,
                 c.chunk_id, c.text, c.page, c.section, c.block_index,
                 c.start_offset, c.end_offset
          FROM documents d JOIN generations g ON g.generation_id=d.active_generation
            AND g.tenant=d.tenant AND g.doc_id=d.doc_id
          JOIN chunks c ON c.generation_id=g.generation_id
          WHERE d.tenant=? AND d.deleted=0 AND g.state='ready'
            AND g.source_revision=d.desired_revision AND g.source_hash=d.source_hash
            AND instr(c.text, ?) > 0
          ORDER BY d.doc_id, c.block_index, c.start_offset
        ''', (scope.tenant_id, literal)).fetchall()
        return [dict(row) for row in rows]


def demo() -> dict:
    with sqlite3.connect(':memory:') as connection:
        store, scope = KnowledgeStore(connection), Scope('demo-company')
        raw = 'VPN 连接失败时，请先检查客户端版本。'
        blocks = [{'text': raw, 'page': 2, 'section': 'VPN / 基础排查'}]
        store.receive(scope, 'vpn-guide', 1, raw, 'kb://it/vpn')
        generation1 = store.build(scope, 'vpn-guide', 1, 'parser1-chunker1', blocks)
        store.publish(scope, generation1, None)
        first = store.search(scope, 'VPN')[0]
        replay = store.receive(scope, 'vpn-guide', 1, raw, 'kb://it/vpn')
        raw_v2 = raw + '仍失败时记录错误码并联系 IT。'
        blocks_v2 = [{'text': raw_v2, 'page': 2, 'section': 'VPN / 基础排查'}]
        store.receive(scope, 'vpn-guide', 2, raw_v2, 'kb://it/vpn')
        hidden_while_building = store.search(scope, 'VPN') == []
        try:
            store.publish(scope, generation1, None)
        except PipelineError as error:
            stale_error = str(error)
        generation2 = store.build(scope, 'vpn-guide', 2, 'parser1-chunker1', blocks_v2)
        store.publish(scope, generation2, None)
        source_v2 = store.search(scope, 'VPN')[0]
        deletion_revision = store.delete(scope, 'vpn-guide', 2)
        try:
            store.publish(scope, generation2, None)
        except PipelineError as error:
            deletion_error = str(error)
        return {'first_source': {key: first[key] for key in
                                 ('doc_id', 'source_revision', 'page', 'section')},
                'receive_replay': replay, 'hidden_while_building': hidden_while_building,
                'stale_publish_error': stale_error,
                'source_v2': {key: source_v2[key] for key in ('source_revision', 'text')},
                'deletion_revision': deletion_revision,
                'late_publish_error': deletion_error,
                'visible_after_delete': bool(store.search(scope, 'VPN'))}


if __name__ == '__main__':
    print(json.dumps(demo(), ensure_ascii=False, indent=2))
