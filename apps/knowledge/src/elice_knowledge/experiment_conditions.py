"""Freeze the selected retrieval index and the execution recipe, without credentials."""

import platform
from importlib.metadata import version

from elice_knowledge.chunking import CHUNKER_VERSION
from elice_knowledge.corpus import digest
from elice_knowledge.embedding import profile_id

SCHEMA = "experiment-conditions-v1"
PACKAGES = (
    "llama-index-core",
    "llama-index-vector-stores-postgres",
    "langgraph",
    "bm25s",
    "kiwipiepy",
    "kiwipiepy_model",
    "tiktoken",
    "httpx",
    "openai",
    "psycopg",
)


def freeze_conditions(conn, index_key, settings):
    from elice_knowledge.agent import PREVIEW_CHARS
    from elice_knowledge.service_store import ServiceError
    from elice_knowledge.sparse import bm25_profile, user_words
    from elice_knowledge.tools import TOOL_CONTRACTS, TOOL_NAMES

    row = conn.execute(
        """SELECT i.*,p.config AS chunking,e.profile AS embedding,e.table_name,
        (SELECT count(*) FROM corpus_snapshot_documents d WHERE d.snapshot_id=i.snapshot_id)
            AS document_count,
        current_setting('server_version') AS postgres_version,
        (SELECT extversion FROM pg_extension WHERE extname='vector') AS pgvector_version
        FROM corpus_vector_indexes i
        JOIN corpus_chunk_policies p ON p.key=i.policy_key
        JOIN corpus_embedding_profiles e ON e.key=i.profile_key WHERE i.key=%s""",
        (index_key,),
    ).fetchone()
    if row is None:
        raise ServiceError("qa_index_conditions_missing", 409)
    identity = {name: row[name] for name in ("snapshot_id", "policy_key", "profile_key")}
    if (
        digest(identity) != index_key
        or digest(row["chunking"]) != row["policy_key"]
        or profile_id(row["embedding"]) != row["profile_key"]
        or row["chunking"].get("chunker_version") != CHUNKER_VERSION
    ):
        raise ServiceError("qa_index_conditions_changed", 409)
    indexes = conn.execute(
        """SELECT indexname,indexdef FROM pg_indexes
        WHERE schemaname=current_schema() AND tablename=%s ORDER BY indexname""",
        (row["table_name"],),
    ).fetchall()
    return {
        "schema": SCHEMA,
        "corpus": {
            "snapshot_id": row["snapshot_id"],
            "documents": row["document_count"],
            "chunks": row["chunk_count"],
        },
        "chunking": {
            "policy_key": row["policy_key"],
            "policy": row["chunking"],
            "implementation": {
                "version": CHUNKER_VERSION,
                "heading_fallback": "shallowest_heading",
                "preserve_tables": True,
                "preserve_code": True,
                "oversized_blocks": "preserve_without_truncation",
            },
        },
        "embedding": {"profile_key": row["profile_key"], "profile": row["embedding"]},
        "vector_index": {
            "key": index_key,
            "backend": "pgvector",
            "table": row["table_name"],
            "postgres_version": row["postgres_version"],
            "pgvector_version": row["pgvector_version"],
            "sql_indexes": indexes,
        },
        "keyword_search": {
            **bm25_profile(),
            "user_words": [
                {"word": e.word, "tag": e.tag, "parts": list(e.parts)} for e in user_words()
            ],
        },
        "agent": {
            "framework": "LangGraph",
            "planner_protocol": "pydantic-json",
            "tools": sorted(TOOL_NAMES),
            "tool_contracts": [
                {
                    "name": t.name,
                    "description": t.description,
                    "input": t.input_schema(),
                    "output": t.output_schema(),
                }
                for t in TOOL_CONTRACTS
            ],
            "observation_preview_chars": PREVIEW_CHARS,
        },
        "generation": {
            "provider": "elice-serverless",
            "api": "openai-chat-completions",
            "stream": False,
            "seed": None,
            "top_p": None,
        },
        "execution": {
            "worker_concurrency": settings.qa_worker_concurrency,
            "python": platform.python_version(),
            "platform": platform.system(),
            "architecture": platform.machine(),
            "libraries": {name: version(name) for name in PACKAGES},
        },
    }


def mlflow_parameters(conditions):
    """A short searchable projection; the full recipe is kept in artifacts."""
    policy = conditions.get("chunking", {}).get("policy", {})
    embedding = conditions.get("embedding", {}).get("profile", {})
    model = embedding.get("embedding", {})
    values = {
        "experiment_conditions_digest": digest(conditions),
        **{
            f"chunking.{key}": policy.get(key)
            for key in ("profile", "overflow", "chunk_size", "chunk_overlap", "tokenizer")
        },
        "embedding.model": model.get("model"),
        "embedding.dimensions": model.get("dimensions"),
        "retrieval.distance": embedding.get("distance"),
        "retrieval.backend": conditions.get("vector_index", {}).get("backend"),
    }
    return {key: value for key, value in values.items() if value is not None}
