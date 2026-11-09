from dataclasses import replace

import pytest
import tiktoken
from llama_index.core.schema import NodeRelationship, TextNode
from test_corpus import URL, article_html

from elice_knowledge.chunking import ChunkingPolicy, chunk_document, chunk_set_key
from elice_knowledge.corpus import extract_article


def chunk_body(body, policy=None):
    article = extract_article(article_html(), URL)
    html = f'<article><div itemprop="articleBody">{body}</div></article>'
    return chunk_document(html, article.to_document(), policy or ChunkingPolicy())


def test_h2_sections_preserve_intro_and_group_subheadings_without_crossing_boundaries():
    nodes = chunk_body(
        '<p>도입부입니다.</p><h2 id="env">실험 환경</h2><p>환경 내용</p>'
        '<h3 id="gpu">GPU</h3><p>장비 정보</p><h4>메모리</h4><p>용량</p>'
        '<h2 id="result">결과</h2><p>결과 내용</p>'
    )
    assert len(nodes) == 3
    assert nodes[0].text == "도입부입니다."
    assert "### GPU" in nodes[1].text and "#### 메모리" in nodes[1].text
    assert "결과 내용" not in nodes[1].text
    assert nodes[1].metadata["anchor"] == "env"
    assert nodes[1].metadata["heading_path"] == "/실험 환경"
    assert nodes[2].metadata["anchor"] == "result"


def test_missing_h2_falls_back_to_shallowest_heading_and_no_heading_keeps_body():
    nodes = chunk_body('<h3 id="a">첫째</h3><p>A</p><h4>하위</h4><p>B</p><h3>둘째</h3><p>C</p>')
    assert len(nodes) == 2 and "#### 하위" in nodes[0].text
    assert nodes[0].metadata["anchor"] == "a"
    assert chunk_body("<p>제목 없는 본문</p>")[0].text == "제목 없는 본문"


def test_profiles_have_distinct_boundaries_and_identity():
    body = '<h2 id="a">첫째</h2><p>A</p><h3>하위</h3><p>B</p><h2>둘째</h2><p>C</p>'
    assert len(chunk_body(body, ChunkingPolicy(profile="h2"))) == 2
    assert len(chunk_body(body, ChunkingPolicy(profile="every_heading"))) == 3
    assert len(chunk_body(body, ChunkingPolicy(profile="page"))) == 1
    policy = ChunkingPolicy()
    assert policy.key == ChunkingPolicy().key
    for other in (
        replace(policy, profile="page"),
        replace(policy, overflow="none"),
        replace(policy, chunk_size=1024),
        replace(policy, chunk_overlap=32),
    ):
        assert other.key != policy.key
        assert chunk_set_key("version", other.key) != chunk_set_key("version", policy.key)
    assert chunk_set_key("new-version", policy.key) != chunk_set_key("version", policy.key)


def test_long_prose_is_bounded_reproducible_and_preserves_source_relationships():
    policy = ChunkingPolicy(chunk_size=96, chunk_overlap=16)
    body = '<h2 id="long">긴 실험</h2><p>' + "문장입니다. 여러 조건에서 실험합니다. " * 80 + "</p>"
    nodes = chunk_body(body, policy)
    assert len(nodes) > 1
    assert [n.to_dict() for n in nodes] == [n.to_dict() for n in chunk_body(body, policy)]
    assert len({n.id_ for n in nodes}) == len(nodes)
    for i, node in enumerate(nodes):
        assert len(tiktoken.get_encoding("cl100k_base").encode_ordinary(node.text)) <= 96
        assert node.metadata["window_index"] == i
        assert node.metadata["window_count"] == len(nodes)
        assert node.metadata["anchor"] == "long"
        assert node.relationships[NodeRelationship.SOURCE].node_id == node.metadata["version_id"]
        assert "chunk_set_key" in node.excluded_embed_metadata_keys
        assert "html_nodes" not in node.metadata and "source" not in node.metadata
        assert TextNode.from_dict(node.to_dict()).to_dict() == node.to_dict()
    assert NodeRelationship.PREVIOUS not in nodes[0].relationships
    assert NodeRelationship.NEXT not in nodes[-1].relationships
    assert nodes[0].relationships[NodeRelationship.NEXT].node_id == nodes[1].id_


def test_code_and_merged_table_are_atomic_and_explicitly_oversized():
    code = "# not a heading\n" + '    print("hello")\n' * 100
    table = (
        '<table><tr><th rowspan="2">条件</th></tr><tr><td>' + "데이터 " * 100 + "</td></tr></table>"
    )
    body = (
        '<h2 id="section">실험</h2><p>이전 내용</p><pre><code class="language-python">'
        + code
        + "</code></pre>"
        + table
        + "<p>이후 내용</p><h2>결론</h2><p>끝</p>"
    )
    nodes = chunk_body(body, ChunkingPolicy(chunk_size=64, chunk_overlap=8))
    code_nodes = [n for n in nodes if code in n.text]
    table_nodes = [n for n in nodes if 'rowspan="2"' in n.text]
    assert len(code_nodes) == len(table_nodes) == 1
    assert code_nodes[0].metadata["oversized"] and table_nodes[0].metadata["oversized"]
    assert "```python" in code_nodes[0].text
    text = "\n".join(n.text for n in nodes)
    assert "이전 내용" in text and "이후 내용" in text and "끝" in text
    assert all(n.metadata["section_title"] != "not a heading" for n in nodes)


def test_overflow_none_and_duplicate_heading_anchors():
    body = (
        '<h2 id="first">같은 제목</h2><p>'
        + "아주 긴 내용입니다. " * 100
        + '</p><h2 id="second">같은 제목</h2><p>다른 내용</p>'
    )
    nodes = chunk_body(body, ChunkingPolicy(overflow="none", chunk_size=64, chunk_overlap=8))
    assert len(nodes) == 2
    assert [node.metadata["anchor"] for node in nodes] == ["first", "second"]
    assert nodes[0].metadata["oversized"]


def test_heading_before_long_structured_block_is_kept_with_that_block():
    body = "<h2>실험</h2><h3>코드 예제</h3><pre>" + "    print(1)\n" * 100 + "</pre>"
    nodes = chunk_body(body, ChunkingPolicy(chunk_size=64, chunk_overlap=8))
    assert len(nodes) == 1
    assert nodes[0].text.startswith("## 실험\n\n### 코드 예제\n\n```")
    assert nodes[0].metadata["oversized"]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"profile": "invalid"},
        {"overflow": "invalid"},
        {"chunk_size": 0},
        {"chunk_overlap": -1},
        {"chunk_size": 64, "chunk_overlap": 64},
    ],
)
def test_invalid_policy_is_rejected(kwargs):
    with pytest.raises(ValueError):
        ChunkingPolicy(**kwargs)
