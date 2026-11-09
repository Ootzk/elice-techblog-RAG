import pytest
from bs4 import BeautifulSoup
from llama_index.core.schema import TextNode

from elice_knowledge.corpus import CorpusError, article_url, extract_article
from elice_knowledge.corpus_store import snapshot_identity

URL = "https://elice.io/ko/resources/blog/example"
PROSE = "한국어 기술 문서의 수집과 버전 관리를 검증하는 문장입니다. " * 8


def article_html(*, lang="ko", canonical=URL, prose=PROSE):
    return f'''<html lang="{lang}"><head><link rel="canonical" href="{canonical}">
    <script type="application/ld+json">{{"@type":"BlogPosting",
    "datePublished":"2026-01-01", "dateModified":"2026-02-01"}}</script></head>
    <body><nav>메뉴 오염</nav><article><h1 itemprop="headline">테스트 문서</h1>
    <div itemprop="articleBody"><div class="MuiBox-root"><p>{prose}</p>
    <h2 id="section">실험 조건</h2><h3 id="detail">세부 조건</h3>
    <table><caption>지연 시간</caption><tr><th rowspan="2">환경</th><th>ms</th></tr>
    <tr><td>장비 A</td><td>12</td></tr></table>
    <pre><code class="language-python">if True:\n    print("ok")\n</code></pre>
    <figure><img src="/diagram.png" alt="실험 구성"><figcaption>GPU 두 개</figcaption></figure>
    </div><div><p>공유 UI 오염</p></div></div></article><footer>푸터 오염</footer></body></html>'''


def test_korean_urls_and_strict_origin():
    assert article_url("/resources/blog/example?tracking=1#section") == URL
    for value in (
        "https://other.test/resources/blog/example",
        "/en/resources/blog/example",
        "/ko/resources/blog",
        "https://elice.io.evil.test/ko/resources/blog/example",
    ):
        with pytest.raises(CorpusError):
            article_url(value)


def test_preserves_source_structure_and_uses_native_nodes():
    article = extract_article(article_html(), URL)
    assert article.published_at == "2026-01-01"
    assert article.modified_at == "2026-02-01"
    assert all(isinstance(node, TextNode) for node in article.nodes)
    assert not any("오염" in node.text for node in article.nodes)
    assert article.source_metadata["sections"] == [
        {"id": "section", "level": 2, "title": "실험 조건"},
        {"id": "detail", "level": 3, "title": "세부 조건"},
    ]
    table = BeautifulSoup(article.source_metadata["tables"][0], "html.parser").table
    assert table.th["rowspan"] == "2" and table.th.text == "환경"
    assert table.caption.text == "지연 시간"
    assert article.source_metadata["code_blocks"][0]["text"] == 'if True:\n    print("ok")\n'
    assert article.source_metadata["images"] == [
        {"url": "https://elice.io/diagram.png", "alt": "실험 구성"}
    ]
    assert any(
        node.metadata["tag"] == "figcaption" and node.text == "GPU 두 개" for node in article.nodes
    )
    text = "\n".join(node.text for node in article.nodes)
    assert text.count("GPU 두 개") == 1
    assert text.count("지연 시간") == 1


@pytest.mark.parametrize(
    "kwargs",
    [
        {"lang": "en"},
        {"canonical": URL.replace("/ko/", "/en/")},
        {"prose": "English only " * 50},
        {"prose": "한국어"},
    ],
)
def test_rejects_language_canonical_and_body_failures(kwargs):
    with pytest.raises(CorpusError):
        extract_article(article_html(**kwargs), URL)


def test_rejects_english_final_url_even_if_html_claims_korean():
    with pytest.raises(CorpusError):
        extract_article(article_html(), URL.replace("/ko/", "/en/"))


def test_raw_page_noise_does_not_create_new_content_version():
    first = extract_article(article_html(), URL)
    second = extract_article(article_html().replace("메뉴 오염", "달라진 메뉴"), URL)
    assert first.content_hash == second.content_hash
    assert first.raw_hash != second.raw_hash
    third = extract_article(article_html(prose=PROSE + "새로운 결과입니다."), URL)
    assert third.content_hash != first.content_hash


def test_nested_list_does_not_drop_or_duplicate_item_text():
    html = article_html().replace(
        f"<p>{PROSE}</p>",
        f"<p>{PROSE}</p><ul><li>부모 항목<ul><li>자식 항목</li></ul></li></ul>",
    )
    text = "\n".join(node.text for node in extract_article(html, URL).nodes)
    assert text.count("부모 항목") == 1
    assert text.count("자식 항목") == 1


def test_snapshot_identity_ignores_page_chrome_provenance_and_manifest_order():
    first = [
        {"url": URL, "version_id": "v1", "raw_hash": "first"},
        {"url": URL + "-other", "version_id": "v2", "raw_hash": "second"},
    ]
    fresh_download = [dict(first[1], raw_hash="changed"), dict(first[0], raw_hash="new-html")]
    assert snapshot_identity(first) == snapshot_identity(fresh_download)
    assert snapshot_identity(first) != snapshot_identity(
        [dict(first[0], version_id="v3"), first[1]]
    )


def test_whole_body_is_a_single_pipeline_input(monkeypatch):
    from llama_index.core.ingestion import IngestionPipeline

    prose = PROSE + '<strong>강조 문장</strong>과 <a href="/reference">참고 자료</a>입니다.'
    original_run = IngestionPipeline.run
    captured = []

    def capture(self, **kwargs):
        captured.append(kwargs["documents"])
        return original_run(self, **kwargs)

    monkeypatch.setattr(IngestionPipeline, "run", capture)
    article = extract_article(article_html(prose=prose), URL)
    assert len(captured) == 1 and len(captured[0]) == 1
    assert "<table" in captured[0][0].text and "<pre" in captured[0][0].text
    assert article.source_metadata["links"] == [
        {"text": "참고 자료", "url": "https://elice.io/reference"}
    ]


def test_native_nested_list_parser_retains_text_without_duplicate_child():
    html = article_html().replace(
        f"<p>{PROSE}</p>",
        f"<p>{PROSE}</p><ul><li>부모 시작<ul><li>자식 항목</li></ul>부모 끝</li></ul>",
    )
    text = "\n".join(node.text for node in extract_article(html, URL).nodes)
    assert all(word in text for word in ("부모 시작", "자식 항목", "부모 끝"))
    assert text.count("자식 항목") == 1


def test_literal_html_text_is_not_reinterpreted_as_markup_and_comments_stay_hidden():
    html = article_html().replace(
        '<h2 id="section">',
        '<div>리터럴 &lt;not-a-tag&gt; &amp; &lt;p&gt;<!--숨긴 주석--></div><h2 id="section">',
    )
    text = "\n".join(node.text for node in extract_article(html, URL).nodes)
    assert "리터럴 <not-a-tag> & <p>" in text
    assert "숨긴 주석" not in text


def test_source_metadata_retains_nested_heading_anchor_and_image():
    html = article_html().replace(
        '<h2 id="section">실험 조건</h2>',
        '<p><h2 id="section">실험 조건</h2><img src="/nested.png" alt="중첩 이미지"></p>',
    )
    article = extract_article(html, URL)
    assert article.source_metadata["sections"][0]["id"] == "section"
    assert article.source_metadata["images"][0]["alt"] == "중첩 이미지"
