"""Immutable dense retrieval indexes backed by LlamaIndex PGVectorStore."""

import asyncio
import json
import sys
from dataclasses import dataclass
from importlib.metadata import version
from urllib.parse import quote

import httpx
from llama_index.core import VectorStoreIndex
from llama_index.core.schema import MetadataMode, TextNode
from llama_index.core.vector_stores import FilterOperator, MetadataFilter, MetadataFilters
from llama_index.vector_stores.postgres import PGVectorStore
from psycopg import sql
from psycopg.types.json import Jsonb

from elice_knowledge.chunk_store import ChunkStore
from elice_knowledge.corpus import digest
from elice_knowledge.database import connect, sqlalchemy_url
from elice_knowledge.embedding import Embedder, EmbeddingError, OllamaEmbedder, profile_id
from elice_knowledge.llama_embedding import KnowledgeEmbedding
from elice_knowledge.settings import Settings


def initialize_retrieval(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS corpus_embedding_profiles (
            key text PRIMARY KEY, profile jsonb NOT NULL, table_name text UNIQUE NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now()
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS corpus_vector_indexes (
            key text PRIMARY KEY,
            snapshot_id text NOT NULL REFERENCES corpus_snapshots(id),
            policy_key text NOT NULL REFERENCES corpus_chunk_policies(key),
            profile_key text NOT NULL REFERENCES corpus_embedding_profiles(key),
            chunk_count integer NOT NULL CHECK (chunk_count > 0),
            created_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE(snapshot_id, policy_key, profile_key)
        )
    """)


def retrieval_profile(backend: Embedder) -> dict:
    return {
        "embedding": backend.profile(),
        "input_contract": "elice-dense-v1",
        "llama_index_core": version("llama-index-core"),
        "document_input": (
            "llamaindex-MetadataMode.EMBED:title,heading_path;canonical-metadata-order"
        ),
        "query_input": "strip;no-prefix",
        "distance": "cosine",
    }


def _check_profile(backend: Embedder, expected: dict):
    if profile_id(retrieval_profile(backend)) != profile_id(expected):
        raise EmbeddingError("Embedding profile changed; index/query aborted")


class RetrievalStore:
    def __init__(self, settings: Settings, schema_name="public"):
        self.settings = settings
        self.schema_name = schema_name

    def vectors(self, profile_key: str, profile: dict):
        # One native table per embedding identity, shared across chunk policies
        # and snapshots. Dimensions and model revisions never mix in one table.
        table_name = "elice_vectors_" + profile_key[:32]
        url = sqlalchemy_url(self.settings)
        return PGVectorStore(
            connection_string=url.render_as_string(hide_password=False),
            async_connection_string=url.set(drivername="postgresql+asyncpg").render_as_string(
                hide_password=False
            ),
            table_name=table_name,
            schema_name=self.schema_name,
            embed_dim=profile["embedding"]["dimensions"],
            use_jsonb=True,
            initialization_fail_on_error=True,
            create_engine_kwargs={
                "pool_pre_ping": True,
                "connect_args": {
                    "connect_timeout": 5,
                    "options": "-c statement_timeout=10000 -c lock_timeout=3000",
                },
            },
        )

    def register_profile(self, key: str, profile: dict):
        with connect(self.settings) as conn:
            conn.execute(
                "INSERT INTO corpus_embedding_profiles(key,profile,table_name) "
                "VALUES (%s,%s,%s) ON CONFLICT(key) DO NOTHING",
                (key, Jsonb(profile), "data_elice_vectors_" + key[:32]),
            )

    def ensure_unique_nodes(self, vectors):
        vectors.get_nodes(node_ids=["__initialize__"])
        table = "data_" + vectors.table_name
        with connect(self.settings) as conn:
            conn.execute(
                sql.SQL("CREATE UNIQUE INDEX IF NOT EXISTS {} ON {}.{} (node_id)").format(
                    sql.Identifier("uq_" + vectors.table_name),
                    sql.Identifier(self.schema_name),
                    sql.Identifier(table),
                )
            )

    def load_chunks(self, snapshot_id: str | None, policy_key: str):
        chunks = ChunkStore(self.settings)
        snapshot_id, versions = chunks.snapshot_versions(snapshot_id)
        nodes = []
        for version_id in versions:
            loaded = chunks.load(version_id, policy_key)
            if loaded is None:
                raise ValueError("Missing chunk set for selected snapshot/policy; run chunk first")
            nodes.extend(loaded)
        return snapshot_id, versions, nodes

    def publish(self, snapshot_id, policy_key, profile_key, count):
        key = digest(
            {"snapshot_id": snapshot_id, "policy_key": policy_key, "profile_key": profile_key}
        )
        with connect(self.settings) as conn:
            conn.execute(
                "INSERT INTO corpus_vector_indexes"
                "(key,snapshot_id,policy_key,profile_key,chunk_count) "
                "VALUES (%s,%s,%s,%s,%s) ON CONFLICT(key) DO NOTHING",
                (key, snapshot_id, policy_key, profile_key, count),
            )
        return key

    def index(
        self, policy_key: str, backend: Embedder, snapshot_id: str | None = None, progress=None
    ):
        snapshot_id, _versions, nodes = self.load_chunks(snapshot_id, policy_key)
        profile = retrieval_profile(backend)
        pid = profile_id(profile)
        # Session lock serializes writes for one profile; network inference never
        # runs in a DB transaction. Completed batches remain reusable on failure.
        with connect(self.settings) as lock:
            lock.autocommit = True
            if not lock.execute("SELECT pg_try_advisory_lock(%s)", (int(pid[:15], 16),)).fetchone()[
                0
            ]:
                raise ValueError("An index operation for this embedding profile is already running")
            self.register_profile(pid, profile)
            vectors = self.vectors(pid, profile)
            try:
                self.ensure_unique_nodes(vectors)
                stored = {
                    node.id_: node for node in vectors.get_nodes(node_ids=[n.id_ for n in nodes])
                }
                for node in nodes:
                    if node.id_ in stored and (
                        node.get_content(metadata_mode=MetadataMode.EMBED)
                        != stored[node.id_].get_content(metadata_mode=MetadataMode.EMBED)
                    ):
                        raise ValueError("Immutable chunk ID has different stored text")
                pending = [node for node in nodes if node.id_ not in stored]
                adapter = KnowledgeEmbedding(backend, batch_size=self.settings.embedding_batch_size)
                size = self.settings.embedding_batch_size
                for start in range(0, len(pending), size):
                    batch = pending[start : start + size]
                    embeddings = adapter.get_text_embedding_batch(
                        [node.get_content(metadata_mode=MetadataMode.EMBED) for node in batch]
                    )
                    _check_profile(backend, profile)
                    for node, embedding in zip(batch, embeddings, strict=True):
                        node.embedding = embedding
                    vectors.add(batch)
                    if progress:
                        progress(
                            {
                                "created": min(start + size, len(pending)),
                                "reused": len(stored),
                                "chunks": len(nodes),
                            }
                        )
                _check_profile(backend, profile)
                present = vectors.get_nodes(node_ids=[node.id_ for node in nodes])
                if len(present) != len(nodes):
                    raise ValueError("Vector index is incomplete")
                key = self.publish(snapshot_id, policy_key, pid, len(nodes))
            finally:
                asyncio.run(vectors.close())
        return {
            "status": "ok",
            "index_key": key,
            "snapshot_id": snapshot_id,
            "policy_key": policy_key,
            "profile_key": pid,
            "chunks": len(nodes),
            "created": len(pending),
            "reused": len(stored),
        }

    def open_index(self, index_key: str, backend: Embedder | None = None) -> "OpenIndex":
        """Resolve a published index once and verify it is complete.

        Dense and keyword search share this snapshot/policy view, so one QA run
        reads the chunk set once even when the agent searches several times.
        """
        with connect(self.settings) as conn:
            row = conn.execute(
                "SELECT i.snapshot_id,i.policy_key,i.profile_key,p.profile,i.chunk_count "
                "FROM corpus_vector_indexes i "
                "JOIN corpus_embedding_profiles p ON p.key=i.profile_key "
                "WHERE i.key=%s",
                (index_key,),
            ).fetchone()
        if row is None:
            raise ValueError("Unknown or incomplete index")
        snapshot_id, policy_key, pid, profile, count = row
        if backend is not None:
            _check_profile(backend, profile)
        _, versions, nodes = self.load_chunks(snapshot_id, policy_key)
        if len(nodes) != count:
            raise ValueError("Stored chunk set does not match the published index")
        return OpenIndex(index_key, snapshot_id, policy_key, pid, profile, versions, nodes)

    def dense_search(self, index: "OpenIndex", query: str, backend: Embedder, top_k=5):
        _check_query(query, top_k)
        _check_profile(backend, index.profile)
        vectors = self.vectors(index.profile_key, index.profile)
        try:
            if not index.vectors_verified:
                present = vectors.get_nodes(node_ids=[node.id_ for node in index.nodes])
                if len(present) != len(index.nodes):
                    raise ValueError("Stored vector index is incomplete; rerun index")
                index.vectors_verified = True
            adapter = KnowledgeEmbedding(backend, batch_size=self.settings.embedding_batch_size)
            retriever = VectorStoreIndex.from_vector_store(
                vectors, embed_model=adapter
            ).as_retriever(
                similarity_top_k=top_k,
                filters=MetadataFilters(
                    filters=[
                        MetadataFilter(key="policy_key", value=index.policy_key),
                        MetadataFilter(
                            key="version_id", value=index.versions, operator=FilterOperator.IN
                        ),
                    ]
                ),
            )
            matches = retriever.retrieve(query.strip())
            _check_profile(backend, index.profile)
        finally:
            asyncio.run(vectors.close())
        return search_output(index, query, [(m.node, m.score) for m in matches])

    def search(self, index_key: str, query: str, backend: Embedder, top_k=5):
        _check_query(query, top_k)
        return self.dense_search(self.open_index(index_key, backend), query, backend, top_k)


@dataclass
class OpenIndex:
    index_key: str
    snapshot_id: str
    policy_key: str
    profile_key: str
    profile: dict
    versions: list[str]
    nodes: list[TextNode]
    vectors_verified: bool = False


def _check_query(query: str, top_k: int):
    if not query.strip() or not 1 <= top_k <= 50:
        raise ValueError("Require a non-empty query and top_k between 1 and 50")


def search_result(node: TextNode, score: float | None) -> dict:
    md = node.metadata
    url = md["url"] + ("#" + quote(md["anchor"], safe="") if md.get("anchor") else "")
    return {
        "chunk_id": node.id_,
        "score": score,
        "url": url,
        "title": md["title"],
        "heading_path": md["heading_path"],
        "version_id": md["version_id"],
        "text": node.text,
    }


def search_output(index: OpenIndex, query: str, matches) -> dict:
    return {
        "status": "ok",
        "index_key": index.index_key,
        "query": query.strip(),
        "results": [search_result(node, score) for node, score in matches],
    }


def latest_index_key(settings: Settings) -> str | None:
    """The configured pinned index, or the most recently published one."""
    with connect(settings) as conn:
        if settings.qa_active_index_key:
            row = conn.execute(
                "SELECT key FROM corpus_vector_indexes WHERE key=%s",
                (settings.qa_active_index_key,),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT key FROM corpus_vector_indexes ORDER BY created_at DESC,key LIMIT 1"
            ).fetchone()
    return row[0] if row else None


def run_index(settings: Settings, policy_key: str, snapshot_id: str | None = None):
    with httpx.Client() as client:
        return RetrievalStore(settings).index(
            policy_key,
            OllamaEmbedder(settings, client),
            snapshot_id,
            progress=lambda event: print(json.dumps(event), file=sys.stderr),
        )


def run_search(settings: Settings, index_key: str, query: str, top_k=5):
    with httpx.Client() as client:
        return RetrievalStore(settings).search(
            index_key, query, OllamaEmbedder(settings, client), top_k
        )


def run_keyword_search(settings: Settings, index_key: str, query: str, top_k=5):
    from elice_knowledge.sparse import keyword_search

    _check_query(query, top_k)
    return keyword_search(RetrievalStore(settings).open_index(index_key), query, top_k)
