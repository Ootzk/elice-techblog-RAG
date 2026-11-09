"""Bounded agentic RAG on LangGraph.

A planner chooses read-only Knowledge tools (BM25 keyword / dense meaning search)
through a JSON protocol, so it works with any Chat Completions model without
native function calling. Once it decides to answer, or the tool budget is spent,
the grounded answer step writes the cited answer and verifies every quote.
"""

import json
import time
from typing import Any, Literal, TypedDict

import httpx
import tiktoken
from langgraph.graph import END, START, StateGraph
from llama_index.core.llms import ChatMessage
from llama_index.core.output_parsers import PydanticOutputParser
from pydantic import BaseModel, ConfigDict, Field

from elice_knowledge.generation import (
    EliceServerlessLLM,
    GenerationNotConfigured,
    GenerationResponseError,
)
from elice_knowledge.prompts import ANSWER, PLANNER, ResolvedPrompt
from elice_knowledge.qa import (
    AgentStep,
    QARequest,
    QAResponse,
    QARetrievalError,
    answer_from_sources,
)
from elice_knowledge.settings import Settings
from elice_knowledge.tools import (
    TOOL_CONTRACTS,
    TOOL_NAMES,
    KnowledgeTools,
    ToolInputError,
    ToolUnavailable,
)
from elice_knowledge.tracing import active_attributes, child_span, finish_span, graph_tracing

PREVIEW_CHARS = 400
CLARIFICATION = "어떤 대상에 대한 질문인지 조금 더 알려주세요."


class AgentDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    action: Literal["keyword_search", "meaning_search", "answer", "clarify"]
    arguments: dict[str, Any] = Field(default_factory=dict)
    source_ids: list[int] = Field(default_factory=list)


class AgentState(TypedDict, total=False):
    request: QARequest
    steps: list[AgentStep]
    sources: list[dict]
    decision: AgentDecision | None
    stop_reason: str | None
    usage: dict[str, int]
    model: str | None
    result: QAResponse


def _count_tokens(text: str) -> int:
    return len(tiktoken.get_encoding("cl100k_base").encode_ordinary(text))


def trim_history(settings: Settings, request: QARequest) -> tuple[QARequest, int]:
    history = request.history[-2 * settings.qa_history_pairs :] if settings.qa_history_pairs else []

    def size(messages):
        return _count_tokens(json.dumps([m.model_dump() for m in messages]))

    while history and size(history) > settings.qa_history_tokens:
        history = history[2:]
    return request.model_copy(update={"history": history}), len(request.history) - len(history)


def _merge_usage(total: dict[str, int], extra: dict[str, int]) -> dict[str, int]:
    merged = dict(total)
    for key, count in extra.items():
        merged[key] = merged.get(key, 0) + count
    return merged


def _compact_schema(value):
    if isinstance(value, dict):
        return {k: _compact_schema(v) for k, v in value.items() if k != "title"}
    if isinstance(value, list):
        return [_compact_schema(v) for v in value]
    return value


def _preview(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= PREVIEW_CHARS else text[:PREVIEW_CHARS] + "…"


def _observations(steps: list[AgentStep], sources: list[dict]) -> list[dict]:
    observations = []
    for number, step in enumerate(steps, start=1):
        if step.action not in TOOL_NAMES:
            continue
        item: dict[str, Any] = {"step": number, "tool": step.action, "arguments": step.arguments}
        if step.error:
            item["error"] = step.error
        else:
            # Full previews only on first sight; repeated hits are referenced by ID.
            item["new_results"] = [
                {
                    "source_id": source_id,
                    "title": sources[source_id - 1]["title"],
                    "heading_path": sources[source_id - 1]["heading_path"],
                    "preview": _preview(sources[source_id - 1]["text"]),
                }
                for source_id in step.new_source_ids
            ]
            item["already_seen_source_ids"] = [
                i for i in step.source_ids if i not in step.new_source_ids
            ]
        observations.append(item)
    return observations


def _planner_schema(item, max_top_k: int) -> dict:
    """The tool schema as this run enforces it: top_k is capped by the QA request."""
    schema = _compact_schema(item.input_schema())
    top_k = schema.get("properties", {}).get("top_k")
    if top_k is not None:
        top_k["maximum"] = min(top_k.get("maximum", max_top_k), max_top_k)
        top_k["default"] = min(top_k.get("default", max_top_k), max_top_k)
    return schema


def planner_messages(
    state: AgentState, settings: Settings, parser, max_top_k: int, prompt: ResolvedPrompt
) -> list[ChatMessage]:
    request = state["request"]
    used = sum(step.action in TOOL_NAMES for step in state["steps"])
    payload = {
        "question": request.question,
        "history_for_reference_resolution_only": [m.model_dump() for m in request.history],
        "tools": [
            {
                "name": item.name,
                "description": item.description,
                "input_schema": _planner_schema(item, max_top_k),
            }
            for item in TOOL_CONTRACTS
        ],
        "remaining_tool_calls": settings.qa_agent_max_tool_calls - used,
        "observations": _observations(state["steps"], state["sources"]),
    }
    return prompt.render(
        max_calls=settings.qa_agent_max_tool_calls,
        payload=json.dumps(payload, ensure_ascii=False),
        output_schema=parser.get_format_string(escape_json=False),
    )


def _same_model(actual: str | None, expected: str) -> bool:
    return not actual or actual.removeprefix("openai/") == expected.removeprefix("openai/")


def build_agent_graph(settings: Settings, llm, tools, prompts: dict[str, ResolvedPrompt]):
    parser = PydanticOutputParser(AgentDecision)

    def plan(state: AgentState) -> dict:
        used = sum(step.action in TOOL_NAMES for step in state["steps"])
        active_attributes(
            {"elice.agent.tool_calls_used": used, "elice.prompt.digest": prompts[PLANNER].digest}
        )
        if used >= settings.qa_agent_max_tool_calls:
            return {"decision": None, "stop_reason": "tool_budget"}
        messages = planner_messages(state, settings, parser, tools.max_top_k, prompts[PLANNER])
        if _count_tokens("".join(m.content for m in messages)) > settings.qa_max_context_tokens:
            return {"decision": None, "stop_reason": "planner_context_budget"}
        started = time.monotonic()
        response = llm.chat(messages)
        model = response.additional_kwargs.get("model")
        if not _same_model(model, settings.generation_model):
            raise GenerationResponseError("Agent model changed")
        try:
            with child_span("planner.parse", "PARSER", {"text": response.message.content}) as span:
                decision = parser.parse(response.message.content)
                finish_span(span, decision.model_dump())
        except (ValueError, TypeError):
            raise GenerationResponseError("Invalid agent decision") from None
        usage = response.additional_kwargs.get("usage", {})
        active_attributes({"elice.agent.action": decision.action})
        step = AgentStep(
            action=decision.action,
            arguments=decision.arguments,
            source_ids=decision.source_ids if decision.action == "answer" else [],
            duration_ms=(time.monotonic() - started) * 1000,
            usage=usage,
        )
        return {
            "decision": decision,
            "steps": [*state["steps"], step],
            "usage": _merge_usage(state["usage"], usage),
            "model": model or state.get("model"),
        }

    def route(state: AgentState) -> str:
        decision = state.get("decision")
        if decision is None or decision.action == "answer":
            return "answer"
        if decision.action == "clarify":
            return "clarify"
        return "act"

    def act(state: AgentState) -> dict:
        decision = state["decision"]
        output, error, elapsed = tools.timed_invoke(decision.action, decision.arguments)
        sources = list(state["sources"])
        known = {source["chunk_id"]: number for number, source in enumerate(sources, start=1)}
        hit_ids, new_ids = [], []
        for hit in output.hits if output else []:
            if hit.chunk_id not in known:
                sources.append(hit.model_dump())
                known[hit.chunk_id] = len(sources)
                new_ids.append(len(sources))
            hit_ids.append(known[hit.chunk_id])
        if isinstance(error, ToolUnavailable):
            message = error.code
        elif isinstance(error, ToolInputError):
            message = str(error)
        else:
            message = None
        last = state["steps"][-1]
        step = last.model_copy(
            update={
                "source_ids": hit_ids,
                "new_source_ids": new_ids,
                "error": message,
                "duration_ms": last.duration_ms + elapsed,
            }
        )
        return {"steps": [*state["steps"][:-1], step], "sources": sources}

    def answer(state: AgentState) -> dict:
        sources = state["sources"]
        tool_steps = [step for step in state["steps"] if step.action in TOOL_NAMES]
        if (
            not sources
            and tool_steps
            and all(step.error == ToolUnavailable.code for step in tool_steps)
        ):
            raise QARetrievalError("Selected retrieval index is unavailable")
        selected = sources
        decision = state.get("decision")
        if decision is not None and decision.source_ids:
            picked = [
                sources[i - 1] for i in dict.fromkeys(decision.source_ids) if 1 <= i <= len(sources)
            ]
            selected = picked or sources
        result = answer_from_sources(settings, state["request"], selected, llm, prompts[ANSWER])
        result.usage = _merge_usage(state["usage"], result.usage)
        if result.generation_model is None:
            result.generation_model = state.get("model")
        result.agent_stop_reason = state.get("stop_reason") or "planner_answer"
        return {"result": result}

    def clarify(state: AgentState) -> dict:
        return {
            "result": QAResponse(
                status="needs_clarification",
                answer=CLARIFICATION,
                index_key=state["request"].index_key,
                reason="ambiguous_reference",
                generation_model=state.get("model"),
                usage=state["usage"],
                agent_stop_reason="planner_clarify",
            )
        }

    graph = StateGraph(AgentState)
    graph.add_node("plan", plan)
    graph.add_node("act", act)
    graph.add_node("answer", answer)
    graph.add_node("clarify", clarify)
    graph.add_edge(START, "plan")
    graph.add_conditional_edges("plan", route, ["act", "answer", "clarify"])
    graph.add_edge("act", "plan")
    graph.add_edge("answer", END)
    graph.add_edge("clarify", END)
    return graph.compile(name="elice.agentic_rag")


def run_qa(
    settings: Settings, request: QARequest, prompts: dict[str, ResolvedPrompt]
) -> QAResponse:
    if not settings.generation_configured:
        raise GenerationNotConfigured("Configure generation base URL, API key and model")
    request, omitted = trim_history(settings, request)
    with httpx.Client() as client:
        llm = EliceServerlessLLM(settings, client)
        tools = KnowledgeTools(settings, request.index_key, client, max_top_k=request.top_k)
        graph = build_agent_graph(settings, llm, tools, prompts)
        with graph_tracing(settings):
            state = graph.invoke(
                {"request": request, "steps": [], "sources": [], "usage": {}, "model": None},
                config={"recursion_limit": 2 * settings.qa_agent_max_tool_calls + 6},
            )
    result = state["result"]
    for name, value in llm.usage_summary().items():
        setattr(result, name, value)
    result.tool_call_count = sum(step.action in TOOL_NAMES for step in state["steps"])
    result.retrieved_sources = state["sources"]
    result.agent_steps = state["steps"]
    result.history_omitted_count = omitted
    result.prompts = {name: prompt.ref() for name, prompt in prompts.items()}
    return result
