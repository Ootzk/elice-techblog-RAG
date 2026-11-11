"""Read-only Knowledge tool contracts shared by the QA agent and the MCP endpoint."""

import time
from dataclasses import dataclass
from typing import Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from elice_knowledge.embedding import create_embedder
from elice_knowledge.retrieval import OpenIndex, RetrievalStore
from elice_knowledge.settings import Settings
from elice_knowledge.sparse import keyword_search
from elice_knowledge.tracing import child_span, documents, finish_span

ToolName = Literal["keyword_search", "meaning_search"]
TOOL_NAMES: tuple[ToolName, ...] = ("keyword_search", "meaning_search")
MAX_TOOL_TOP_K = 10


class SearchInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(
        min_length=1,
        max_length=500,
        description="Standalone Korean search query; resolve pronouns before searching.",
    )
    top_k: int = Field(default=5, ge=1, le=MAX_TOOL_TOP_K, description="Chunks to return.")

    @field_validator("query")
    @classmethod
    def clean_query(cls, value):
        if not value.strip():
            raise ValueError("Query must not be blank")
        return value.strip()


class SearchHit(BaseModel):
    chunk_id: str
    title: str
    url: str
    heading_path: str
    version_id: str
    score: float | None
    text: str


class SearchOutput(BaseModel):
    tool: ToolName
    query: str
    index_key: str
    hits: list[SearchHit]


@dataclass(frozen=True)
class ToolContract:
    name: ToolName
    description: str
    input_model: type[BaseModel]
    output_model: type[BaseModel]

    def input_schema(self) -> dict:
        return self.input_model.model_json_schema()

    def output_schema(self) -> dict:
        return self.output_model.model_json_schema()


TOOL_CONTRACTS: tuple[ToolContract, ...] = (
    ToolContract(
        name="keyword_search",
        description=(
            "BM25 keyword search over Korean Elice techblog chunks (Kiwi morphemes). "
            "Prefer it for product/model names, identifiers, numbers and exact wording."
        ),
        input_model=SearchInput,
        output_model=SearchOutput,
    ),
    ToolContract(
        name="meaning_search",
        description=(
            "Dense semantic search (BGE-M3) over Korean Elice techblog chunks. "
            "Prefer it for concepts, intentions and paraphrased questions."
        ),
        input_model=SearchInput,
        output_model=SearchOutput,
    ),
)


class ToolInputError(ValueError):
    """The caller sent arguments that violate the tool contract; safe to show."""


class ToolUnavailable(RuntimeError):
    """Retrieval infrastructure failed; details are withheld from callers."""

    code = "tool_unavailable"


def contract(name: str) -> ToolContract:
    for item in TOOL_CONTRACTS:
        if item.name == name:
            return item
    raise ToolInputError(f"Unknown tool: {name}; use one of {', '.join(TOOL_NAMES)}")


def parse_arguments(name: str, arguments: dict | None) -> SearchInput:
    try:
        return contract(name).input_model.model_validate(arguments or {})
    except ValidationError as exc:
        fields = ", ".join(
            ".".join(str(part) for part in error["loc"]) or "arguments" for error in exc.errors()
        )
        raise ToolInputError(f"Invalid arguments for {name}: {fields}") from None


class KnowledgeTools:
    """Tool executor bound to one published index for one QA run or MCP call.

    The index is opened lazily and once, so several searches in one answer share
    the verified snapshot/policy chunk view.
    """

    def __init__(
        self,
        settings: Settings,
        index_key: str,
        client: httpx.Client,
        *,
        max_top_k: int = MAX_TOOL_TOP_K,
    ):
        self.settings = settings
        self.index_key = index_key
        self.client = client
        self.max_top_k = max_top_k
        self.store = RetrievalStore(settings)
        self._index: OpenIndex | None = None

    def _open(self) -> OpenIndex:
        if self._index is None:
            self._index = self.store.open_index(self.index_key)
        return self._index

    def invoke(self, name: str, arguments: dict | None) -> SearchOutput:
        request = parse_arguments(name, arguments)
        top_k = min(request.top_k, self.max_top_k)
        try:
            with child_span(
                f"retrieval.{name}",
                "RETRIEVER",
                {"query": request.query, "top_k": top_k, "index_key": self.index_key},
                {"elice.retrieval.role": "search", "elice.retrieval.backend": name},
            ) as span:
                index = self._open()
                if name == "keyword_search":
                    result = keyword_search(index, request.query, top_k)
                else:
                    backend = create_embedder(self.settings, self.client)
                    result = self.store.dense_search(index, request.query, backend, top_k)
                finish_span(span, documents(result["results"]))
        except Exception:
            # Upstream errors can carry SQL, endpoints or credentials.
            raise ToolUnavailable(f"{name} is temporarily unavailable") from None
        return SearchOutput(
            tool=name, query=request.query, index_key=self.index_key, hits=result["results"]
        )

    def timed_invoke(self, name: str, arguments: dict | None):
        started = time.monotonic()
        with child_span(name, "TOOL", {"arguments": arguments}) as span:
            try:
                output = self.invoke(name, arguments)
                finish_span(span, output.model_dump())
                return output, None, (time.monotonic() - started) * 1000
            except (ToolInputError, ToolUnavailable) as exc:
                finish_span(
                    span,
                    {"error": getattr(exc, "code", type(exc).__name__)},
                    {"elice.tool.failed": True},
                )
                if span is not None:
                    try:
                        span.set_status("ERROR")
                    except Exception:
                        pass
                return None, exc, (time.monotonic() - started) * 1000
