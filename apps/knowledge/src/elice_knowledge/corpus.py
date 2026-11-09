"""Source-specific extraction. No model calls or database writes."""

import hashlib
import json
import re
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup, Tag
from llama_index.core.schema import Document, TextNode

from elice_knowledge.documents import article_document
from elice_knowledge.html_parser import parse_html

ORIGIN = "https://elice.io"
LISTING_PATH = "/ko/resources/blog"
PROCESSOR_VERSION = "elice-html-v1"


class CorpusError(ValueError):
    """Messages in this class are safe to include in collection reports."""


def digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def article_url(value: str) -> str:
    parsed = urlsplit(urljoin(ORIGIN, value))
    if parsed.scheme != "https" or parsed.netloc != "elice.io":
        raise CorpusError("Article must belong to https://elice.io")
    path = parsed.path.rstrip("/")
    if path.startswith("/resources/blog/"):
        path = "/ko" + path
    if not re.fullmatch(r"/ko/resources/blog/[^/]+", path):
        raise CorpusError("Article must use the Korean blog path")
    return urlunsplit(("https", "elice.io", path, "", ""))


def korean_page(soup: BeautifulSoup):
    if soup.html is None or soup.html.get("lang", "").lower().split("-")[0] != "ko":
        raise CorpusError("HTML language is not Korean")


@dataclass(frozen=True)
class Article:
    url: str
    title: str
    published_at: str | None
    modified_at: str | None
    nodes: list[TextNode]
    source_metadata: dict
    raw_html: str

    @property
    def content_hash(self) -> str:
        return digest(
            {
                "processor_version": PROCESSOR_VERSION,
                "url": self.url,
                "title": self.title,
                "published_at": self.published_at,
                "modified_at": self.modified_at,
                "nodes": [node.hash for node in self.nodes],
                "source": self.source_metadata,
            }
        )

    @property
    def raw_hash(self) -> str:
        return hashlib.sha256(self.raw_html.encode()).hexdigest()

    def to_document(self) -> Document:
        return article_document(
            url=self.url,
            title=self.title,
            published_at=self.published_at,
            modified_at=self.modified_at,
            nodes=self.nodes,
            source_metadata=self.source_metadata,
            content_hash=self.content_hash,
            processor_version=PROCESSOR_VERSION,
        )


def authored_body(soup: BeautifulSoup) -> Tag:
    body = soup.select_one('article [itemprop="articleBody"]')
    if body is None:
        raise CorpusError("Missing articleBody")
    content = body.select_one(":scope > .MuiBox-root") or body
    for node in content.select("script, style, nav, svg, button"):
        node.decompose()
    return content


def extract_article(html: str, url: str) -> Article:
    expected = article_url(url)
    if urlsplit(url).path.rstrip("/") != urlsplit(expected).path:
        raise CorpusError("Final URL is not explicitly Korean")
    soup = BeautifulSoup(html, "html.parser")
    korean_page(soup)
    canonical = soup.select_one('link[rel="canonical"]')
    if canonical is None or canonical.get("href", "").rstrip("/") != expected:
        raise CorpusError("Canonical does not match the Korean article URL")
    title = soup.select_one('article [itemprop="headline"], article h1')
    body = soup.select_one('article [itemprop="articleBody"]')
    if title is None or body is None:
        raise CorpusError("Missing article title or articleBody")
    content = authored_body(soup)
    metadata = {}
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            value = json.loads(script.get_text())
        except (ValueError, TypeError):
            continue
        candidates = value if isinstance(value, list) else [value]
        for candidate in candidates:
            if isinstance(candidate, dict) and candidate.get("@type") == "BlogPosting":
                metadata = candidate

    nodes, source_metadata = parse_html(content, url)
    prose = " ".join(
        node.text for node in nodes if node.metadata.get("tag") not in {"table", "pre"}
    )
    if len(prose) < 100 or len(re.findall("[가-힣]", prose)) < 20:
        raise CorpusError("Body is empty or lacks sufficient Korean text")
    published = soup.select_one('time[itemprop="datePublished"]')
    return Article(
        url=expected,
        title=title.get_text(" ", strip=True),
        published_at=metadata.get("datePublished")
        or (published.get("datetime") if published else None),
        modified_at=metadata.get("dateModified"),
        nodes=nodes,
        source_metadata=source_metadata,
        raw_html=html,
    )
