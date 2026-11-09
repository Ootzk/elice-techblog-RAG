import httpx
import pytest

from elice_knowledge.corpus import ORIGIN, CorpusError
from elice_knowledge.crawler import Crawler


def listing(slug, pages=(1, 2)):
    return (
        '<html lang="ko"><body>'
        + f'<a href="/resources/blog/{slug}">문서</a>'
        + "".join(f'<a href="/resources/blog?filter=tech_blog&page={p}">{p}</a>' for p in pages)
        + "</body></html>"
    )


def test_discovers_all_pages_and_normalizes_urls():
    def handler(request):
        assert request.headers["Accept-Language"].startswith("ko")
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-Agent: *\nAllow: /\n")
        page = request.url.params["page"]
        return httpx.Response(200, text=listing("first" if page == "1" else "second"))

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        crawler = Crawler(client, interval=0)
        crawler.load_robots()
        assert crawler.discover() == [
            ORIGIN + "/ko/resources/blog/first",
            ORIGIN + "/ko/resources/blog/second",
        ]


def test_repeated_page_is_an_error_instead_of_silent_truncation():
    with httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, text=listing("same")))
    ) as client:
        with pytest.raises(CorpusError, match="repeated"):
            Crawler(client, interval=0).discover()


def test_ignores_phantom_next_link_beyond_last_numbered_page():
    calls = []

    def handler(request):
        page = request.url.params["page"]
        calls.append(page)
        assert page in {"1", "2"}
        html = listing("article-" + page) + (
            '<a aria-label="Go to next page" href="/resources/blog?filter=tech_blog&page=3"></a>'
        )
        return httpx.Response(200, text=html)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert len(Crawler(client, interval=0).discover()) == 2
    assert calls == ["1", "2"]


@pytest.mark.parametrize("location", ["https://other.test/", "/en/resources/blog/example"])
def test_redirect_is_rejected_before_requesting_wrong_language_or_origin(location):
    calls = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(308, headers={"Location": location})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(CorpusError):
            Crawler(client, interval=0).fetch(ORIGIN + "/ko/resources/blog/example")
    assert len(calls) == 1


def test_robots_disallow_stops_collection():
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, text="User-Agent: *\nDisallow: /ko/\n")
        )
    ) as client:
        crawler = Crawler(client, interval=0)
        crawler.load_robots()
        with pytest.raises(CorpusError, match="disallows"):
            crawler.discover()


def test_retry_only_transient_failures(monkeypatch):
    monkeypatch.setattr("elice_knowledge.crawler.time.sleep", lambda _: None)
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(503 if len(calls) == 1 else 200)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert Crawler(client, interval=0).fetch(ORIGIN + "/robots.txt").status_code == 200
    assert len(calls) == 2
