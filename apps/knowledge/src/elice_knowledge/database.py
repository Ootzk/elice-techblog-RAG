import json
from uuid import uuid4

import psycopg
from psycopg.conninfo import conninfo_to_dict
from psycopg.types.json import Jsonb
from sqlalchemy import URL

from elice_knowledge.embedding import profile_id
from elice_knowledge.settings import Settings


def connect(settings: Settings):
    if settings.database_url is None:
        raise ValueError("DATABASE_URL is required")
    return psycopg.connect(
        settings.database_url.get_secret_value(),
        connect_timeout=5,
        options="-c statement_timeout=5000 -c lock_timeout=3000",
    )


def sqlalchemy_url(settings: Settings) -> URL:
    if settings.database_url is None:
        raise ValueError("DATABASE_URL is required")
    params = conninfo_to_dict(settings.database_url.get_secret_value())
    return URL.create(
        "postgresql+psycopg2",
        username=params.get("user"),
        password=params.get("password"),
        host=params.get("host"),
        port=int(params["port"]) if params.get("port") else None,
        database=params.get("dbname"),
        query={
            k: v
            for k, v in params.items()
            if k not in {"user", "password", "host", "port", "dbname"}
        },
    )


def check_database(settings: Settings):
    with connect(settings) as conn:
        row = conn.execute(
            "SELECT extversion FROM pg_extension WHERE extname = 'vector'"
        ).fetchone()
        if row is None:
            raise ValueError("pgvector extension is missing; run init-db")


def initialize_database(settings: Settings):
    from elice_knowledge.chunk_store import initialize_chunks
    from elice_knowledge.corpus_store import initialize_corpus
    from elice_knowledge.retrieval import initialize_retrieval
    from elice_knowledge.service_store import initialize_service

    with connect(settings) as conn:
        conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS connectivity_samples (
                id uuid PRIMARY KEY,
                text_content text NOT NULL,
                profile_id text NOT NULL,
                profile jsonb NOT NULL,
                embedding vector NOT NULL,
                created_at timestamptz NOT NULL DEFAULT now()
            )
        """)
        initialize_corpus(conn)
        initialize_chunks(conn)
        initialize_retrieval(conn)
        initialize_service(conn)
    from elice_knowledge.access_codes import AccessCodes
    from elice_knowledge.service_store import ServiceStore

    AccessCodes(ServiceStore(settings)).bootstrap()


def store_and_search(settings: Settings, text: str, vector: list[float], profile: dict) -> dict:
    sample_id = uuid4()
    pid = profile_id(profile)
    encoded = json.dumps(vector)
    with connect(settings) as conn:
        conn.execute(
            """INSERT INTO connectivity_samples
               (id, text_content, profile_id, profile, embedding)
               VALUES (%s, %s, %s, %s, %s::vector)""",
            (sample_id, text, pid, Jsonb(profile), encoded),
        )
        stored = conn.execute(
            "SELECT vector_dims(embedding) FROM connectivity_samples WHERE id = %s", (sample_id,)
        ).fetchone()
        nearest = conn.execute(
            """SELECT id, embedding <=> %s::vector AS distance
               FROM connectivity_samples WHERE profile_id = %s
               ORDER BY distance, created_at DESC LIMIT 1""",
            (encoded, pid),
        ).fetchone()
        if stored is None or stored[0] != len(vector) or nearest is None:
            raise ValueError("Vector round-trip failed")
        if abs(nearest[1]) > 0.00001:
            raise ValueError("Unexpected cosine distance for sample vector")
    return {
        "sample_id": str(sample_id),
        "profile_id": pid,
        "dimensions": stored[0],
        "nearest_distance": nearest[1],
    }
