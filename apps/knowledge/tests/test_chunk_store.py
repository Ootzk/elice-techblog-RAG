from dataclasses import replace
from uuid import uuid4

import psycopg
import pytest
from test_corpus import PROSE, URL, article_html

from elice_knowledge.chunk_store import ChunkStore, run_chunking
from elice_knowledge.chunking import ChunkingPolicy
from elice_knowledge.corpus import extract_article


def test_policy_results_coexist_and_cached_nodes_survive_roundtrip(store, monkeypatch):
    corpus, connect = store
    article = extract_article(article_html(), URL)
    corpus.save(article, {})
    chunks = ChunkStore(corpus.settings)
    policy = ChunkingPolicy()
    first, reused = chunks.prepare(article.to_document().id_, policy)
    assert not reused
    other, reused = chunks.prepare(
        article.to_document().id_, replace(policy, profile="every_heading")
    )
    assert not reused
    assert {n.id_ for n in first}.isdisjoint({n.id_ for n in other})
    monkeypatch.setattr(
        "elice_knowledge.chunk_store.chunk_document",
        lambda *a: pytest.fail("Cache must bypass parsing"),
    )
    cached, reused = chunks.prepare(article.to_document().id_, policy)
    assert reused and [n.to_dict() for n in cached] == [n.to_dict() for n in first]
    assert [n.hash for n in cached] == [n.hash for n in first]
    with connect(None) as conn:
        assert conn.execute("SELECT count(*) FROM corpus_chunk_policies").fetchone()[0] == 2
        assert conn.execute("SELECT count(*) FROM corpus_chunk_sets").fetchone()[0] == 2
        assert conn.execute("SELECT count(*) FROM corpus_versions").fetchone()[0] == 1


def test_snapshot_reuse_and_new_versions_do_not_replace_old_chunks(store):
    corpus, _ = store
    first = extract_article(article_html(), URL)
    corpus.save(first, {})
    snapshot_id, _ = corpus.snapshot([URL])
    policy = ChunkingPolicy()
    one = run_chunking(corpus.settings, policy, snapshot_id)
    assert one["documents"] == one["created"] == 1
    assert run_chunking(corpus.settings, policy, snapshot_id)["reused"] == 1
    second = extract_article(article_html(prose=PROSE + "변경된 새 내용입니다."), URL)
    corpus.save(second, {})
    new_snapshot, _ = corpus.snapshot([URL])
    assert new_snapshot != snapshot_id
    assert run_chunking(corpus.settings, policy, new_snapshot)["created"] == 1
    assert run_chunking(corpus.settings, policy, snapshot_id)["reused"] == 1


def test_failed_result_save_rolls_back_policy_and_empty_results_are_rejected(store):
    corpus, connect = store
    article = extract_article(article_html(), URL)
    corpus.save(article, {})
    chunks = ChunkStore(corpus.settings)
    with pytest.raises(psycopg.errors.CheckViolation):
        chunks.save(article.to_document().id_, ChunkingPolicy(), [])
    with connect(None) as conn:
        assert conn.execute("SELECT count(*) FROM corpus_chunk_policies").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM corpus_chunk_sets").fetchone()[0] == 0
    with pytest.raises(ValueError, match="Unknown archived"):
        chunks.prepare(str(uuid4()), ChunkingPolicy())


def test_missing_snapshot_is_not_silently_replaced(store):
    corpus, _ = store
    with pytest.raises(ValueError, match="snapshot"):
        run_chunking(corpus.settings, ChunkingPolicy(), "missing")
