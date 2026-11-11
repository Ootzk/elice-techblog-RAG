import pytest
from llama_index.core.schema import TextNode

from elice_knowledge.retrieval import OpenIndex
from elice_knowledge.settings import Settings
from elice_knowledge.sparse import (
    UserWord,
    _bm25_index,
    bm25_profile,
    keyword_search,
    load_user_words,
    tokenize,
    user_words,
)
from elice_knowledge.tools import (
    TOOL_CONTRACTS,
    KnowledgeTools,
    ToolInputError,
    ToolUnavailable,
    parse_arguments,
)

INDEX = "b" * 64


def node(chunk_id, text, title="테크블로그"):
    return TextNode(
        id_=chunk_id,
        text=text,
        metadata={
            "url": "https://elice.io/ko/resources/blog/" + chunk_id,
            "anchor": "",
            "title": title,
            "heading_path": "/본문",
            "version_id": "version-" + chunk_id,
        },
        excluded_embed_metadata_keys=["url", "anchor", "version_id"],
    )


@pytest.fixture
def index():
    return OpenIndex(
        INDEX,
        "snapshot",
        "policy",
        "profile",
        {},
        ["version"],
        [
            node("b300", "B300 GPU를 엘리스클라우드에서 가상화해 여러 작업에 나눕니다."),
            node("whisper", "Whisper 모델로 한국어 음성을 인식하는 실험을 했습니다."),
            node("qwen", "Qwen2.5-7B 모델을 vLLM으로 서빙했습니다. 서빙 지연을 측정했습니다."),
        ],
    )


def test_korean_tokenizer_drops_particles_and_keeps_domain_and_mixed_terms():
    tokens = tokenize("엘리스클라우드에서 B300 GPU를 사용했습니다.")
    assert {"엘리스클라우드", "엘리스", "클라우드", "b300", "gpu", "사용"} <= set(tokens)
    assert not {"에서", "를", "했", "습니다", "."} & set(tokens)
    assert tokenize("서빙 서빙").count("서빙") == 2


def test_keyword_search_ranks_exact_terms_and_drops_zero_scores(index):
    results = keyword_search(index, "Qwen2.5-7B 서빙", top_k=3)["results"]
    assert [r["chunk_id"] for r in results] == ["qwen"]
    assert results[0]["score"] > 0 and results[0]["title"] == "테크블로그"
    compound = keyword_search(index, "엘리스 클라우드 가상화", top_k=3)["results"]
    assert compound[0]["chunk_id"] == "b300"
    assert keyword_search(index, "없는용어입니다", top_k=3)["results"] == []


def test_bm25_index_is_cached_per_index_content(index):
    assert _bm25_index(index) is _bm25_index(index)
    changed = OpenIndex(**{**index.__dict__, "nodes": index.nodes[:2]})
    assert _bm25_index(changed) is not _bm25_index(index)


def test_tool_contract_validates_arguments_with_safe_messages():
    assert parse_arguments("keyword_search", {"query": "  GPU  "}).query == "GPU"
    with pytest.raises(ToolInputError, match="Unknown tool"):
        parse_arguments("drop_table", {"query": "x"})
    with pytest.raises(ToolInputError, match="query"):
        parse_arguments("meaning_search", {"query": " "})
    with pytest.raises(ToolInputError, match="top_k"):
        parse_arguments("meaning_search", {"query": "GPU", "top_k": 50})
    with pytest.raises(ToolInputError):
        parse_arguments("meaning_search", {"query": "GPU", "index_key": "other"})
    assert {c.name for c in TOOL_CONTRACTS} == {"keyword_search", "meaning_search"}


def test_tools_open_index_once_cap_top_k_and_sanitize_failures(index, monkeypatch):
    tools = KnowledgeTools(Settings(), INDEX, client=None, max_top_k=1)
    opened = []
    monkeypatch.setattr(tools.store, "open_index", lambda key: opened.append(key) or index)
    first = tools.invoke("keyword_search", {"query": "서빙 모델 GPU", "top_k": 5})
    tools.invoke("keyword_search", {"query": "Whisper"})
    assert opened == [INDEX] and len(first.hits) == 1

    def broken(*args):
        raise RuntimeError("postgresql://user:private-password@db")

    monkeypatch.setattr(tools.store, "dense_search", broken)
    with pytest.raises(ToolUnavailable) as error:
        tools.invoke("meaning_search", {"query": "GPU"})
    assert "private" not in str(error.value)
    output, failure, elapsed = tools.timed_invoke("meaning_search", {"query": "GPU"})
    assert output is None and isinstance(failure, ToolUnavailable) and elapsed >= 0


def write_dictionary(tmp_path, text):
    path = tmp_path / "words.tsv"
    path.write_text(text, encoding="utf-8")
    return path


def test_user_dictionary_file_is_parsed_and_casefolded(tmp_path):
    path = write_dictionary(
        tmp_path,
        "# comment\n\n엘리스클라우드\tNNP\t엘리스, 클라우드\nECI\n런박스\tNNG\n",
    )
    assert load_user_words(path) == (
        UserWord("엘리스클라우드", "NNP", ("엘리스", "클라우드")),
        UserWord("eci", "NNP", ()),
        UserWord("런박스", "NNG", ()),
    )


@pytest.mark.parametrize(
    "line",
    ["엘리스 클라우드\tNNP", "엘리스\tVV", "엘리스\tNNP\t엘 리스", "엘리스\tNNP\t엘리스\textra"],
)
def test_malformed_user_dictionary_lines_fail_with_line_number(tmp_path, line):
    path = write_dictionary(tmp_path, "# header\n" + line + "\n")
    with pytest.raises(ValueError, match="words.tsv:2"):
        load_user_words(path)


def test_duplicate_user_words_are_rejected(tmp_path):
    path = write_dictionary(tmp_path, "엘리스\n엘리스\tNNG\n")
    with pytest.raises(ValueError, match="duplicate"):
        load_user_words(path)


def test_packaged_dictionary_drives_tokenizer_and_bm25_profile():
    words = {entry.word for entry in user_words()}
    assert {"엘리스", "엘리스클라우드"} <= words
    assert len(bm25_profile()["user_dictionary"]) == 64


def test_warm_up_builds_active_index_cache_and_never_raises(index, monkeypatch, caplog):
    import elice_knowledge.sparse as sparse

    sparse._cache.clear()
    monkeypatch.setattr(sparse, "active_index_key", lambda settings: INDEX)
    monkeypatch.setattr(sparse.RetrievalStore, "open_index", lambda self, key: index)
    assert sparse.warm_up(Settings()) == INDEX
    assert len(sparse._cache) == 1 and _bm25_index(index) is next(iter(sparse._cache.values()))

    def broken(settings):
        raise RuntimeError("postgresql://user:private-password@db")

    monkeypatch.setattr(sparse, "active_index_key", broken)
    assert sparse.warm_up(Settings()) is None
    assert "RuntimeError" in caplog.text and "private" not in caplog.text


def test_concurrent_builds_share_one_bm25_index(index):
    import threading

    import elice_knowledge.sparse as sparse

    sparse._cache.clear()
    results = []
    threads = [
        threading.Thread(target=lambda: results.append(_bm25_index(index))) for _ in range(4)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len({id(result) for result in results}) == 1
