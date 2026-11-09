"""Optional real PostgreSQL tests; every test owns an isolated disposable schema."""

import os
from dataclasses import replace
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from test_corpus import PROSE, URL, article_html

from elice_knowledge.corpus import extract_article
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
    version_documents = None
    try:
        with isolated_connect(None) as conn:
            initialize_corpus(conn)
        settings = Settings(database_url=dsn)
        version_documents = VersionDocuments.postgres(settings, schema_name=schema)
        yield CorpusStore(settings, version_documents), isolated_connect
    finally:
        if version_documents:
            version_documents.close()
        with psycopg.connect(dsn, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def test_repeated_content_is_idempotent_and_old_snapshot_is_immutable(store):
    repository, connection = store
    first = extract_article(article_html(), URL)
    assert repository.save(first, {"etag": '"a"'}) == "created"
    snapshot_id, manifest = repository.snapshot([URL])
    assert repository.save(first, {"etag": '"b"'}) == "unchanged"
    assert repository.snapshot([URL])[0] == snapshot_id
    second = extract_article(article_html(prose=PROSE + "변경된 결과입니다."), URL)
    assert repository.save(second, {}) == "updated"
    assert repository.snapshot([URL])[0] != snapshot_id
    with connection(None) as conn:
        assert conn.execute("SELECT count(*) FROM corpus_versions").fetchone()[0] == 2
        old = conn.execute(
            "SELECT manifest FROM corpus_snapshots WHERE id=%s", (snapshot_id,)
        ).fetchone()[0]
        assert old == manifest
        assert old[0]["version_id"] != str(repository.documents()[URL]["current_version_id"])


def test_failed_document_update_rolls_back_and_preserves_last_good_version(store):
    repository, connection = store
    article = extract_article(article_html(), URL)
    repository.save(article, {"etag": '"good"'})
    before = repository.documents()
    with pytest.raises(psycopg.errors.NotNullViolation):
        repository.save(replace(article, title=None), {"etag": '"bad"'})
    after = repository.documents()
    assert before[URL]["current_version_id"] == after[URL]["current_version_id"]
    assert after[URL]["etag"] == '"good"'
    with connection(None) as conn:
        assert conn.execute("SELECT count(*) FROM corpus_versions").fetchone()[0] == 1


def test_snapshot_selection_and_conditional_request_metadata(store):
    repository, _ = store
    first = extract_article(article_html(), URL)
    other = replace(first, url=URL + "-other")
    repository.save(first, {"etag": '"first"'})
    repository.save(other, {})
    repository.not_modified(URL, {"etag": '"new-validator"'})
    assert repository.documents()[URL]["etag"] == '"new-validator"'
    _, manifest = repository.snapshot([URL])
    assert [entry["url"] for entry in manifest] == [URL]


def test_new_versions_archive_standard_documents_and_use_native_docstore(store):
    repository, connection = store
    article = extract_article(article_html(), URL)
    repository.save(article, {})
    document = article.to_document()
    stored = repository.version_documents.docstore.get_document(document.id_)
    assert stored.metadata["source"] == article.source_metadata
    assert stored.hash == document.hash
    fresh = extract_article(article_html(), URL)
    assert repository.save(fresh, {}) == "unchanged"
    assert repository.version_documents.docstore.get_document(document.id_).hash == stored.hash
    assert repository.current_document(URL).hash == stored.hash
    with connection(None) as conn:
        row = conn.execute("SELECT document FROM corpus_versions").fetchone()
        assert row[0]["id_"] == document.id_
        assert row[0]["metadata"]["source"] == article.source_metadata


def test_docstore_failure_is_repairable_and_does_not_replace_published_snapshot(store, monkeypatch):
    repository, connection = store
    first = extract_article(article_html(), URL)
    repository.save(first, {})
    snapshot_id, manifest = repository.snapshot([URL])
    second = extract_article(article_html(prose=PROSE + "새로운 관측입니다."), URL)
    docstore = repository.version_documents.docstore
    original_add = docstore.add_documents

    def failed(*args, **kwargs):
        raise RuntimeError("injected docstore persistence failure")

    monkeypatch.setattr(docstore, "add_documents", failed)
    with pytest.raises(RuntimeError):
        repository.save(second, {})
    with connection(None) as conn:
        assert conn.execute("SELECT count(*) FROM corpus_snapshots").fetchone()[0] == 1
        assert (
            conn.execute(
                "SELECT manifest FROM corpus_snapshots WHERE id=%s", (snapshot_id,)
            ).fetchone()[0]
            == manifest
        )
    monkeypatch.setattr(docstore, "add_documents", original_add)
    repository.not_modified(URL, {})
    assert docstore.get_document(second.to_document().id_).text == second.to_document().text
    assert repository.snapshot([URL])[0] != snapshot_id
