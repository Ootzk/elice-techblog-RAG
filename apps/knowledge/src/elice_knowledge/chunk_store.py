"""Content-addressed policy and chunk-set values in PostgreSQL JSONB."""

import json

from llama_index.core.schema import Document, TextNode
from psycopg.types.json import Jsonb

from elice_knowledge.chunking import ChunkingPolicy, chunk_document, chunk_set_key
from elice_knowledge.database import connect
from elice_knowledge.settings import Settings


def _restore_nodes(values) -> list[TextNode]:
    nodes = [TextNode.from_dict(value) for value in values]
    for node in nodes:
        # JSONB does not preserve object order; native hashes use metadata repr.
        node.metadata = json.loads(json.dumps(node.metadata, ensure_ascii=False, sort_keys=True))
    return nodes


def initialize_chunks(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS corpus_chunk_policies (
            key text PRIMARY KEY, config jsonb NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now()
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS corpus_chunk_sets (
            key text PRIMARY KEY,
            version_id uuid NOT NULL REFERENCES corpus_versions(id),
            policy_key text NOT NULL REFERENCES corpus_chunk_policies(key),
            nodes jsonb NOT NULL CHECK (jsonb_typeof(nodes) = 'array'
                                       AND jsonb_array_length(nodes) > 0),
            created_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE(version_id, policy_key)
        )
    """)


class ChunkStore:
    def __init__(self, settings: Settings):
        self.settings = settings

    def load(self, version_id: str, policy_key: str) -> list[TextNode] | None:
        with connect(self.settings) as conn:
            row = conn.execute(
                "SELECT nodes FROM corpus_chunk_sets WHERE key=%s",
                (chunk_set_key(version_id, policy_key),),
            ).fetchone()
        return _restore_nodes(row[0]) if row else None

    def save(self, version_id: str, policy: ChunkingPolicy, nodes: list[TextNode]):
        # Policy and complete result become visible in one transaction. The first
        # writer owns this immutable key; concurrent callers reuse that value.
        with connect(self.settings) as conn:
            conn.execute(
                "INSERT INTO corpus_chunk_policies(key,config) VALUES (%s,%s) "
                "ON CONFLICT(key) DO NOTHING",
                (policy.key, Jsonb(policy.payload)),
            )
            inserted = conn.execute(
                "INSERT INTO corpus_chunk_sets(key,version_id,policy_key,nodes) "
                "VALUES (%s,%s,%s,%s) ON CONFLICT(key) DO NOTHING RETURNING key",
                (
                    chunk_set_key(version_id, policy.key),
                    version_id,
                    policy.key,
                    Jsonb([node.to_dict() for node in nodes]),
                ),
            ).fetchone()
            row = conn.execute(
                "SELECT nodes FROM corpus_chunk_sets WHERE key=%s",
                (chunk_set_key(version_id, policy.key),),
            ).fetchone()
        return _restore_nodes(row[0]), inserted is None

    def prepare(self, version_id: str, policy: ChunkingPolicy):
        cached = self.load(version_id, policy.key)
        if cached is not None:
            return cached, True
        with connect(self.settings) as conn:
            row = conn.execute(
                "SELECT raw_html, document FROM corpus_versions WHERE id=%s", (version_id,)
            ).fetchone()
        if row is None:
            raise ValueError("Unknown archived document version")
        nodes = chunk_document(row[0], Document.from_dict(row[1]), policy)
        return self.save(version_id, policy, nodes)

    def snapshot_versions(self, snapshot_id: str | None):
        with connect(self.settings) as conn:
            if snapshot_id is None:
                row = conn.execute(
                    "SELECT id FROM corpus_snapshots ORDER BY created_at DESC, id LIMIT 1"
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT id FROM corpus_snapshots WHERE id=%s", (snapshot_id,)
                ).fetchone()
            if row is None:
                raise ValueError("No matching corpus snapshot; ingest first")
            snapshot_id = row[0]
            versions = conn.execute(
                "SELECT version_id FROM corpus_snapshot_documents "
                "WHERE snapshot_id=%s ORDER BY version_id",
                (snapshot_id,),
            ).fetchall()
        if not versions:
            raise ValueError("Corpus snapshot is empty")
        return snapshot_id, [str(row[0]) for row in versions]


def run_chunking(settings: Settings, policy: ChunkingPolicy, snapshot_id: str | None = None):
    store = ChunkStore(settings)
    snapshot_id, versions = store.snapshot_versions(snapshot_id)
    summary = {
        "status": "ok",
        "snapshot_id": snapshot_id,
        "policy_key": policy.key,
        "policy": policy.payload,
        "documents": len(versions),
        "created": 0,
        "reused": 0,
        "chunks": 0,
        "oversized": 0,
    }
    for version_id in versions:
        nodes, reused = store.prepare(version_id, policy)
        summary["reused" if reused else "created"] += 1
        summary["chunks"] += len(nodes)
        summary["oversized"] += sum(node.metadata["oversized"] for node in nodes)
    return summary
