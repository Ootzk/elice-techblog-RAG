from dataclasses import replace

import pytest
from test_corpus import PROSE, URL, article_html

from elice_knowledge.chunk_store import ChunkStore
from elice_knowledge.chunking import ChunkingPolicy
from elice_knowledge.corpus import extract_article
from elice_knowledge.embedding import EmbeddingError, EmbeddingInputTooLong
from elice_knowledge.retrieval import RetrievalStore
from elice_knowledge.sparse import keyword_search


class FakeEmbedder:
    def __init__(self, digest="stable"):
        self.digest = digest
        self.inputs = []
        self.fail_at = None
        self.change_model = False

    def profile(self):
        return {"backend": "fake", "digest": self.digest, "dimensions": 2, "truncate": False}

    def embed(self, texts):
        self.inputs.append(texts)
        if self.fail_at == len(self.inputs):
            raise EmbeddingInputTooLong("too long")
        if self.change_model:
            self.digest = "changed"
        return [[1.0, 0.0] if "GPU" in text else [0.0, 1.0] for text in texts]


@pytest.fixture
def retrieval(store):
    corpus, connect = store
    article = extract_article(article_html(), URL)
    corpus.save(article, {})
    snapshot, _ = corpus.snapshot([URL])
    policy = ChunkingPolicy()
    chunks = ChunkStore(corpus.settings)
    chunks.prepare(article.to_document().id_, policy)
    with connect(None) as conn:
        schema = conn.execute("SELECT current_schema()").fetchone()[0]
    settings = corpus.settings.model_copy(update={"embedding_batch_size": 1})
    return RetrievalStore(settings, schema), corpus, chunks, snapshot, policy, connect


def test_native_vector_index_reuses_embeddings_and_returns_ranked_sources(retrieval):
    repo, _, _, snapshot, policy, connect = retrieval
    backend = FakeEmbedder()
    result = repo.index(policy.key, backend, snapshot)
    assert result["created"] == result["chunks"] > 0
    calls = len(backend.inputs)
    repeat = repo.index(policy.key, backend, snapshot)
    assert repeat["index_key"] == result["index_key"]
    assert repeat["created"] == 0 and repeat["reused"] == result["chunks"]
    assert len(backend.inputs) == calls
    matches = repo.search(result["index_key"], " GPU ", backend)["results"]
    assert "GPU" in matches[0]["text"] and matches[0]["url"].endswith("#section")
    assert matches[0]["score"] == pytest.approx(1.0)
    assert [m["score"] for m in matches] == sorted((m["score"] for m in matches), reverse=True)
    assert backend.inputs[-1] == ["GPU"]
    assert all("policy_key:" not in text for batch in backend.inputs for text in batch)
    with connect(None) as conn:
        assert conn.execute("SELECT count(*) FROM corpus_vector_indexes").fetchone()[0] == 1


def test_policy_and_model_indexes_coexist_without_cross_policy_results(retrieval):
    repo, corpus, chunks, snapshot, policy, connect = retrieval
    backend = FakeEmbedder()
    first = repo.index(policy.key, backend, snapshot)
    other = replace(policy, profile="every_heading")
    article_id = corpus.current_document(URL).id_
    other_nodes, _ = chunks.prepare(article_id, other)
    second = repo.index(other.key, backend, snapshot)
    assert first["index_key"] != second["index_key"]
    assert first["profile_key"] == second["profile_key"]
    other_ids = {n.id_ for n in other_nodes}
    assert not other_ids.intersection(
        m["chunk_id"] for m in repo.search(first["index_key"], "GPU", backend)["results"]
    )
    new_backend = FakeEmbedder(digest="another-model")
    third = repo.index(policy.key, new_backend, snapshot)
    assert third["profile_key"] != first["profile_key"]
    with pytest.raises(EmbeddingError, match="profile changed"):
        repo.search(first["index_key"], "GPU", new_backend)
    with connect(None) as conn:
        assert conn.execute("SELECT count(*) FROM corpus_vector_indexes").fetchone()[0] == 3
        assert conn.execute("SELECT count(*) FROM corpus_embedding_profiles").fetchone()[0] == 2


def test_old_snapshot_search_excludes_new_document_versions(retrieval):
    repo, corpus, chunks, snapshot, policy, _ = retrieval
    backend = FakeEmbedder()
    old_version = corpus.current_document(URL).id_
    first = repo.index(policy.key, backend, snapshot)
    new = extract_article(article_html(prose=PROSE + "새로운 GPU 환경입니다."), URL)
    corpus.save(new, {})
    new_snapshot, _ = corpus.snapshot([URL])
    chunks.prepare(new.to_document().id_, policy)
    repo.index(policy.key, backend, new_snapshot)
    assert {
        m["version_id"] for m in repo.search(first["index_key"], "GPU", backend)["results"]
    } == {old_version}


def test_failed_batch_is_not_published_and_successful_batches_are_reused(retrieval):
    repo, _, _, snapshot, policy, connect = retrieval
    backend = FakeEmbedder()
    backend.fail_at = 2
    with pytest.raises(EmbeddingInputTooLong):
        repo.index(policy.key, backend, snapshot)
    with connect(None) as conn:
        assert conn.execute("SELECT count(*) FROM corpus_vector_indexes").fetchone()[0] == 0
    backend.fail_at = None
    result = repo.index(policy.key, backend, snapshot)
    assert result["reused"] == 1
    assert result["created"] == result["chunks"] - 1


def test_model_change_during_inference_does_not_save_or_publish_batch(retrieval):
    repo, _, _, snapshot, policy, connect = retrieval
    backend = FakeEmbedder()
    backend.change_model = True
    with pytest.raises(EmbeddingError, match="profile changed"):
        repo.index(policy.key, backend, snapshot)
    with connect(None) as conn:
        assert conn.execute("SELECT count(*) FROM corpus_vector_indexes").fetchone()[0] == 0
    backend.change_model = False
    backend.digest = "stable"
    result = repo.index(policy.key, backend, snapshot)
    assert result["reused"] == 0


def test_missing_chunk_sets_cannot_be_published(retrieval):
    repo, _, _, snapshot, _, _ = retrieval
    with pytest.raises(ValueError, match="Missing chunk set"):
        repo.index("unknown-policy", FakeEmbedder(), snapshot)


@pytest.mark.parametrize("query,top_k", [(" ", 5), ("GPU", 0), ("GPU", 51)])
def test_invalid_search_input_is_rejected_without_backend_calls(query, top_k):
    from elice_knowledge.settings import Settings

    backend = FakeEmbedder()
    with pytest.raises(ValueError):
        RetrievalStore(Settings()).search("unused", query, backend, top_k)
    assert not backend.inputs


def test_keyword_and_dense_search_share_one_verified_index_view(retrieval):
    repo, _, _, snapshot, policy, _ = retrieval
    backend = FakeEmbedder()
    key = repo.index(policy.key, backend, snapshot)["index_key"]
    index = repo.open_index(key, backend)
    assert index.snapshot_id == snapshot and index.policy_key == policy.key and index.nodes
    sparse = keyword_search(index, "GPU", top_k=3)["results"]
    assert sparse and all("GPU" in match["text"] for match in sparse)
    assert sparse[0]["url"].endswith("#section") and sparse[0]["score"] > 0
    dense = repo.dense_search(index, "GPU", backend, top_k=3)["results"]
    assert index.vectors_verified and dense[0]["chunk_id"] in {m["chunk_id"] for m in sparse}
    with pytest.raises(ValueError, match="Unknown"):
        repo.open_index("0" * 64)


class CountingEmbedder(FakeEmbedder):
    def __init__(self):
        super().__init__()
        self.profile_calls = 0

    def profile(self):
        self.profile_calls += 1
        return super().profile()


def test_repeated_dense_search_on_open_index_checks_profile_once_per_search(retrieval):
    repo, _, _, snapshot, policy, _ = retrieval
    backend = CountingEmbedder()
    key = repo.index(policy.key, backend, snapshot)["index_key"]
    index = repo.open_index(key)
    backend.profile_calls = 0
    repo.dense_search(index, "GPU", backend, top_k=2)
    assert backend.profile_calls == 2
    repo.dense_search(index, "GPU", backend, top_k=2)
    assert backend.profile_calls == 3
    # A model swap between searches is still caught by the post-search check.
    backend.digest = "swapped"
    with pytest.raises(EmbeddingError, match="profile changed"):
        repo.dense_search(index, "GPU", backend, top_k=2)


def test_active_profile_freezes_resolved_prompt_content(retrieval, monkeypatch):
    from elice_knowledge.prompts import PLANNER, PROMPT_NAMES, bundled
    from elice_knowledge.service_store import ServiceStore

    repo, corpus, _, snapshot, policy, _ = retrieval
    key = repo.index(policy.key, FakeEmbedder(), snapshot)["index_key"]
    store = ServiceStore(corpus.settings)
    default = store.profile(store.active_profile(key, 3))
    assert {p["source"] for p in default["prompts"].values()} == {"bundled"}
    assert default["prompts"][PLANNER]["template"] == bundled(PLANNER).template

    def pinned(settings, selectors):
        out = {n: bundled(n) for n in PROMPT_NAMES}
        if selectors:
            out[PLANNER] = out[PLANNER].model_copy(
                update={"source": "registry", "uri": f"prompts:/{PLANNER}/7", "version": 7}
            )
        return out

    monkeypatch.setattr("elice_knowledge.service_store.resolve", pinned)
    experiment = store.active_profile(key, 3, {PLANNER: "7"})
    assert experiment != store.active_profile(key, 3)
    assert store.profile(experiment)["prompts"][PLANNER]["version"] == 7
