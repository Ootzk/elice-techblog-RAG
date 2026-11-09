"""Whole-body parsing through LlamaIndex, with source metadata kept separately."""

from urllib.parse import urljoin
from uuid import NAMESPACE_URL, uuid5

from bs4 import Comment, Tag
from llama_index.core.ingestion import IngestionPipeline
from llama_index.core.node_parser import HTMLNodeParser
from llama_index.core.schema import Document, TextNode

HTML_TAGS = [
    "div",
    "section",
    "p",
    "ul",
    "ol",
    "li",
    "blockquote",
    "figure",
    "figcaption",
    "table",
    "caption",
    "thead",
    "tbody",
    "tfoot",
    "tr",
    "th",
    "td",
    "pre",
    *[f"h{level}" for level in range(1, 7)],
]


def parse_html(content: Tag, url: str) -> tuple[list[TextNode], dict]:
    for comment in content.find_all(string=lambda text: isinstance(text, Comment)):
        comment.extract()
    document = Document(
        id_=str(uuid5(NAMESPACE_URL, url)), text=str(content), metadata={"url": url}
    )
    pipeline = IngestionPipeline(
        transformations=[HTMLNodeParser(tags=HTML_TAGS)],
        disable_cache=True,
    )
    nodes = pipeline.run(documents=[document])
    # Preserve source details that the native text parser simplifies or omits.
    source = {
        "sections": [
            {"id": h.get("id"), "level": int(h.name[1]), "title": h.get_text(" ", strip=True)}
            for h in content.find_all([f"h{i}" for i in range(1, 7)])
        ],
        "tables": [str(table) for table in content.find_all("table")],
        "code_blocks": [
            {
                "text": pre.get_text(),
                "language": (pre.code.get("class", []) if pre.code else pre.get("class", [])),
            }
            for pre in content.find_all("pre")
        ],
        "images": [
            {"url": urljoin(url, img.get("src", "")), "alt": img.get("alt", "")}
            for img in content.find_all("img")
        ],
        "links": [
            {"url": urljoin(url, a["href"]), "text": a.get_text(" ", strip=True)}
            for a in content.select("a[href]")
        ],
    }
    return nodes, source
