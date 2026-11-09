"""Request/response contract and the grounded, citation-verified answer step."""

import json
import re
from typing import Any, Literal

import tiktoken
from llama_index.core.output_parsers import PydanticOutputParser
from pydantic import BaseModel, ConfigDict, Field, field_validator

from elice_knowledge.generation import GenerationResponseError
from elice_knowledge.prompts import PromptRef, ResolvedPrompt
from elice_knowledge.settings import Settings
from elice_knowledge.tracing import child_span, documents, finish_span

ABSTENTION = "제공된 문서에서 답변에 필요한 근거를 확인하지 못했습니다."


class QARequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question: str = Field(min_length=1, max_length=4000)
    index_key: str = Field(pattern=r"^[a-f0-9]{64}$")
    top_k: int = Field(default=5, ge=1, le=20)
    history: list["DialogueMessage"] = Field(default_factory=list, max_length=20)

    @field_validator("question")
    @classmethod
    def clean_question(cls, value):
        if not value.strip():
            raise ValueError("Question must not be blank")
        return value.strip()

    @field_validator("history")
    @classmethod
    def complete_history(cls, values):
        if len(values) % 2 or any(
            v.role != ("user" if i % 2 == 0 else "assistant") for i, v in enumerate(values)
        ):
            raise ValueError("History must contain complete user/assistant pairs")
        return values


class DialogueMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=16000)


class GeneratedCitation(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    source_id: int = Field(ge=1)
    quote: str = Field(min_length=1)


class GeneratedAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    answerable: bool
    answer: str
    citations: list[GeneratedCitation]


class Citation(BaseModel):
    source_id: int
    chunk_id: str
    title: str
    url: str
    heading_path: str
    version_id: str
    quote: str
    score: float | None = None


class AgentStep(BaseModel):
    """One planner decision and, for tool actions, its execution outcome."""

    action: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    source_ids: list[int] = Field(default_factory=list)
    new_source_ids: list[int] = Field(default_factory=list)
    error: str | None = None
    duration_ms: float = 0
    usage: dict[str, int] = Field(default_factory=dict)


class QAResponse(BaseModel):
    status: Literal["answered", "insufficient_evidence", "needs_clarification"]
    answer: str
    index_key: str
    citations: list[Citation] = Field(default_factory=list)
    reason: str | None = None
    prompts: dict[str, PromptRef] = Field(default_factory=dict)
    generation_model: str | None = None
    usage: dict[str, int] = Field(default_factory=dict)
    retrieved_sources: list[dict] = Field(default_factory=list)
    usage_reported_calls: dict[str, int] = Field(default_factory=dict)
    llm_call_count: int = Field(default=0, ge=0)
    tool_call_count: int = Field(default=0, ge=0)
    history_omitted_count: int = 0
    agent_steps: list[AgentStep] = Field(default_factory=list)
    agent_stop_reason: (
        Literal["planner_answer", "planner_clarify", "tool_budget", "planner_context_budget"] | None
    ) = None


class QARetrievalError(RuntimeError):
    pass


def _messages(
    prompt: ResolvedPrompt,
    question: str,
    sources: list[dict],
    parser: PydanticOutputParser,
    history=None,
):
    return prompt.render(
        payload=json.dumps(
            {
                "question": question,
                "history_for_reference_resolution_only": history or [],
                "sources": [
                    {
                        "source_id": i + 1,
                        "title": source["title"],
                        "heading_path": source["heading_path"],
                        "text": source["text"],
                    }
                    for i, source in enumerate(sources)
                ],
            },
            ensure_ascii=False,
        ),
        output_schema=parser.get_format_string(escape_json=False),
    )


def answer_from_sources(
    settings: Settings, request: QARequest, matches: list[dict], llm, prompt: ResolvedPrompt
):
    parser = PydanticOutputParser(GeneratedAnswer)
    tokenizer = tiktoken.get_encoding("cl100k_base").encode_ordinary
    sources = []
    # The agent already bounded each search by top_k; the token budget decides
    # how many collected sources reach the answer step.
    for match in matches:
        if not match["text"].strip():
            continue
        candidate = [*sources, match]
        messages = _messages(
            prompt, request.question, candidate, parser, [m.model_dump() for m in request.history]
        )
        if len(tokenizer("".join(m.content for m in messages))) <= settings.qa_max_context_tokens:
            sources = candidate

    def abstain(reason, **metadata):
        return _abstain(request, reason, **metadata)

    with child_span(
        "retrieval.final_context",
        "RETRIEVER",
        {"question": request.question},
        {"elice.retrieval.role": "final_context", "elice.prompt.digest": prompt.digest},
    ) as span:
        finish_span(span, documents(sources))

    if not sources:
        return abstain("no_evidence" if not matches else "context_budget")
    response = llm.chat(
        _messages(
            prompt, request.question, sources, parser, [m.model_dump() for m in request.history]
        )
    )
    with child_span(
        "answer.parse_and_validate",
        "PARSER",
        {"text": response.message.content, "sources": documents(sources)},
    ) as span:
        result = _parse_and_validate(response, parser, request, sources)
        finish_span(
            span,
            result.model_dump(),
            {
                "elice.citation.outcome": (
                    "invalid"
                    if result.reason == "invalid_citations"
                    else "valid"
                    if result.status == "answered"
                    else "not_applicable"
                )
            },
        )
        return result


def _abstain(request, reason, **metadata):
    return QAResponse(
        status="insufficient_evidence",
        answer=ABSTENTION,
        index_key=request.index_key,
        reason=reason,
        **metadata,
    )


def _parse_and_validate(response, parser, request, sources):
    def abstain(reason, **metadata):
        return _abstain(request, reason, **metadata)

    try:
        generated = parser.parse(response.message.content)
    except (ValueError, TypeError):
        raise GenerationResponseError("Invalid grounded QA output") from None
    metadata = {
        "generation_model": response.additional_kwargs.get("model"),
        "usage": response.additional_kwargs.get("usage", {}),
    }
    if not generated.answerable:
        return abstain("model_abstained", **metadata)
    cited_ids = {citation.source_id for citation in generated.citations}
    markers = {int(number) for number in re.findall(r"\[(\d+)\]", generated.answer)}
    if not generated.answer.strip() or not cited_ids or markers != cited_ids:
        return abstain("invalid_citations", **metadata)
    citations = []
    for citation in generated.citations:
        if citation.source_id > len(sources):
            return abstain("invalid_citations", **metadata)
        source = sources[citation.source_id - 1]
        quote = " ".join(citation.quote.split())
        if not quote or quote not in " ".join(source["text"].split()):
            return abstain("invalid_citations", **metadata)
        citations.append(
            Citation(
                source_id=citation.source_id,
                quote=citation.quote,
                **{
                    key: source[key]
                    for key in ("chunk_id", "title", "url", "heading_path", "version_id", "score")
                },
            )
        )
    return QAResponse(
        status="answered",
        answer=generated.answer,
        index_key=request.index_key,
        citations=citations,
        **metadata,
    )
