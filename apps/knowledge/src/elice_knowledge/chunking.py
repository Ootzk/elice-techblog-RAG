"""Heading-first, model-free chunking of archived article versions."""

import json
import re
from collections import defaultdict, deque
from dataclasses import asdict, dataclass
from importlib.metadata import version
from urllib.parse import urljoin
from uuid import NAMESPACE_URL, uuid5

import tiktoken
from bs4 import BeautifulSoup
from llama_index.core.node_parser import MarkdownNodeParser, SentenceSplitter
from llama_index.core.schema import Document, NodeRelationship, RelatedNodeInfo, TextNode
from markdownify import markdownify

from elice_knowledge.corpus import authored_body, digest

CHUNKER_VERSION = "elice-section-v1"
TOKENIZER = "cl100k_base"


@dataclass(frozen=True)
class ChunkingPolicy:
    profile: str = "h2"
    overflow: str = "sentence"
    chunk_size: int = 512
    chunk_overlap: int = 64

    def __post_init__(self):
        if self.profile not in {"h2", "every_heading", "page"}:
            raise ValueError("Unsupported heading profile")
        if self.overflow not in {"sentence", "none"}:
            raise ValueError("Unsupported overflow policy")
        if self.chunk_size <= 0 or not 0 <= self.chunk_overlap < self.chunk_size:
            raise ValueError("Require chunk_size > chunk_overlap >= 0")

    @property
    def payload(self) -> dict:
        return {
            **asdict(self),
            "chunker_version": CHUNKER_VERSION,
            "tokenizer": TOKENIZER,
            "dependencies": {
                name: version(name)
                for name in ("llama-index-core", "markdownify", "beautifulsoup4", "tiktoken")
            },
        }

    @property
    def key(self) -> str:
        return digest(self.payload)


def chunk_set_key(version_id: str, policy_key: str) -> str:
    return digest({"version_id": str(version_id), "policy_key": policy_key})


def _markdown(html: str, **options) -> str:
    return markdownify(html, heading_style="ATX", bullets="-", **options).strip()


def _heading(text: str) -> tuple[int, str] | None:
    match = re.match(r"^(#{1,6})\s+(.+)", text)
    return (len(match[1]), match[2].strip()) if match else None


def _sections(raw_html: str, document: Document, policy: ChunkingPolicy):
    body = authored_body(BeautifulSoup(raw_html, "html.parser"))
    for link in body.select("a[href]"):
        link["href"] = urljoin(document.metadata["url"], link["href"])
    for image in body.select("img[src]"):
        image["src"] = urljoin(document.metadata["url"], image["src"])
    # Hide structured blocks from the text splitters, then restore their complete
    # contents. Markers are chosen outside the source, so authored text cannot collide.
    prefix = "ELICEPROTECTED" + digest(raw_html)
    while prefix in raw_html:
        prefix += "X"
    protected = {}
    for block in list(body.find_all(["table", "pre"])):
        if block.parent is None:
            continue
        marker = f"{prefix}X{len(protected)}END"
        if block.name == "pre":
            code = block.code or block
            classes = code.get("class", [])
            language = next((c[9:] for c in classes if c.startswith("language-")), "")
            code_text = block.get_text()
            # A longer fence preserves even code containing Markdown fences.
            fence = "`" * max(3, max((len(s) + 1 for s in re.findall(r"`+", code_text)), default=3))
            text = f"{fence}{language}\n{code_text}\n{fence}"
        else:
            # Markdown tables cannot express merged cells; retain original HTML there.
            text = str(block) if block.select("[rowspan], [colspan]") else _markdown(str(block))
        protected[marker] = text
        block.replace_with(BeautifulSoup(f"<p>{marker}</p>", "html.parser").p)

    anchors = defaultdict(deque)
    for header in body.find_all([f"h{i}" for i in range(1, 7)]):
        heading = _heading(_markdown(str(header)))
        if heading:
            anchors[heading].append(header.get("id"))
    markdown = _markdown(str(body))
    source = Document(id_=document.id_, text=markdown)
    parsed = MarkdownNodeParser(include_metadata=True).get_nodes_from_documents([source])
    levels = [heading[0] for node in parsed if (heading := _heading(node.text))]
    boundary = 2 if 2 in levels else min(levels, default=2)
    sections = []
    for node in parsed:
        heading = _heading(node.text)
        anchor = anchors[heading].popleft() if heading and anchors[heading] else None
        starts = heading and (
            policy.profile == "every_heading" or (policy.profile == "h2" and heading[0] <= boundary)
        )
        if not sections or starts:
            # Native MarkdownNodeParser supplies the ancestor path; include this
            # section's own heading without implementing another heading stack.
            parent_path = node.metadata.get("header_path", "/")
            sections.append(
                {
                    "text": node.text,
                    "heading_path": parent_path + heading[1] if heading else "",
                    "section_title": heading[1] if heading else document.metadata["title"],
                    "anchor": anchor,
                }
            )
        else:
            sections[-1]["text"] += "\n\n" + node.text
    if policy.profile == "page" and sections:
        sections[0].update(heading_path="", section_title=document.metadata["title"], anchor=None)
    return sections, protected


def chunk_document(raw_html: str, document: Document, policy: ChunkingPolicy) -> list[TextNode]:
    """Derive stable standard TextNodes without mutating the source Document."""
    tokenizer = tiktoken.get_encoding(TOKENIZER).encode_ordinary
    splitter = SentenceSplitter(
        chunk_size=policy.chunk_size,
        chunk_overlap=policy.chunk_overlap,
        tokenizer=tokenizer,
        paragraph_separator="\n\n",
    )
    sections, protected = _sections(raw_html, document, policy)
    marker_pattern = "(" + "|".join(re.escape(marker) for marker in protected) + ")"
    nodes = []
    policy_key = policy.key
    result_key = chunk_set_key(document.id_, policy_key)
    for section_index, section in enumerate(sections):
        parts = re.split(marker_pattern, section["text"]) if protected else [section["text"]]
        restored = "".join(protected.get(part, part) for part in parts).strip()
        if not restored:
            continue
        if policy.overflow == "none" or len(tokenizer(restored)) <= policy.chunk_size:
            windows = [restored]
        else:
            windows = []
            pending_headings = []
            for part in parts:
                if part in protected:
                    windows.append("\n\n".join([*pending_headings, protected[part]]))
                    pending_headings = []
                elif part.strip():
                    lines = [line for line in part.strip().splitlines() if line.strip()]
                    if all(_heading(line) for line in lines):
                        pending_headings.append(part.strip())
                    else:
                        text = "\n\n".join([*pending_headings, part.strip()])
                        pending_headings = []
                        windows.extend(splitter.split_text(text))
            if pending_headings:
                windows.append("\n\n".join(pending_headings))
        section_nodes = []
        for window_index, text in enumerate(windows):
            token_count = len(tokenizer(text))
            metadata = {
                "url": document.metadata["url"],
                "title": document.metadata["title"],
                "version_id": document.id_,
                "policy_key": policy_key,
                "chunk_set_key": result_key,
                "heading_path": section["heading_path"],
                "section_title": section["section_title"],
                "anchor": section["anchor"],
                "section_index": section_index,
                "window_index": window_index,
                "window_count": len(windows),
                "token_count": token_count,
                "oversized": token_count > policy.chunk_size,
            }
            excluded = [key for key in metadata if key not in {"title", "heading_path"}]
            section_nodes.append(
                TextNode(
                    id_=str(uuid5(NAMESPACE_URL, f"{result_key}/{section_index}/{window_index}")),
                    text=text,
                    metadata=json.loads(json.dumps(metadata, ensure_ascii=False, sort_keys=True)),
                    excluded_embed_metadata_keys=excluded,
                    excluded_llm_metadata_keys=excluded,
                    relationships={NodeRelationship.SOURCE: RelatedNodeInfo(node_id=document.id_)},
                )
            )
        for index, node in enumerate(section_nodes):
            if index:
                node.relationships[NodeRelationship.PREVIOUS] = section_nodes[
                    index - 1
                ].as_related_node_info()
            if index + 1 < len(section_nodes):
                node.relationships[NodeRelationship.NEXT] = section_nodes[
                    index + 1
                ].as_related_node_info()
        nodes.extend(section_nodes)
    if not nodes:
        raise ValueError("No chunkable article content")
    return nodes
