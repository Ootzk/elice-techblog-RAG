import os
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql

from elice_knowledge.chunk_store import initialize_chunks
from elice_knowledge.corpus_store import CorpusStore, initialize_corpus
from elice_knowledge.documents import VersionDocuments
from elice_knowledge.settings import Settings


@pytest.fixture
def store(monkeypatch):
    dsn = os.environ.get("TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("Set TEST_DATABASE_URL to run isolated PostgreSQL integration tests")
    schema = "test_ingest_" + uuid4().hex
    with psycopg.connect(dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))

    def isolated_connect(_):
        return psycopg.connect(dsn, options=f"-c search_path={schema}")

    monkeypatch.setattr("elice_knowledge.corpus_store.connect", isolated_connect)
    monkeypatch.setattr("elice_knowledge.chunk_store.connect", isolated_connect)
    version_documents = None
    try:
        with isolated_connect(None) as conn:
            initialize_corpus(conn)
            initialize_chunks(conn)
        settings = Settings(database_url=dsn)
        version_documents = VersionDocuments.postgres(settings, schema_name=schema)
        yield CorpusStore(settings, version_documents), isolated_connect
    finally:
        if version_documents:
            version_documents.close()
        with psycopg.connect(dsn, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
