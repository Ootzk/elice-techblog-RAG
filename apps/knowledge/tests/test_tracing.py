"""Trace real graph/adapter boundaries with local storage and mocked external I/O."""

import json
from types import SimpleNamespace

import httpx
import mlflow
import pytest
from mlflow.entities import MlflowExperimentLocation
from test_agent import OTHER, run, step
from test_generation import generation_settings
from test_qa import ANSWER, INDEX, SOURCE

from elice_knowledge.settings import Settings
from elice_knowledge.tools import KnowledgeTools, ToolUnavailable


@pytest.fixture
def trace_backend(tmp_path, monkeypatch):
    previous = mlflow.get_tracking_uri()
    monkeypatch.setenv("MLFLOW_ALLOW_FILE_STORE", "true")
    # set_tracking_uri also changes this environment variable; restore its absence too.
    monkeypatch.setenv("MLFLOW_TRACKING_URI", previous)
    mlflow.set_tracking_uri((tmp_path / "mlruns").as_uri())
    # The app accepts remote URIs; only the SDK test backend uses local files.
    monkeypatch.setattr(
        "test_generation.Settings", lambda **kw: Settings(mlflow_tracking_uri=None, **kw)
    )
    experiment = mlflow.MlflowClient().create_experiment("span-contract")
    try:
        yield MlflowExperimentLocation(experiment_id=experiment)
    finally:
        mlflow.flush_trace_async_logging()
        mlflow.set_tracking_uri(previous)


def persisted(root):
    mlflow.flush_trace_async_logging()
    trace = mlflow.get_trace(root.trace_id)
    assert trace is not None
    return trace.data.spans


def test_graph_tools_models_and_final_context_are_one_trace(trace_backend, monkeypatch):
    with httpx.Client() as client:
        tools = KnowledgeTools(generation_settings(), INDEX, client)
        tools.store = SimpleNamespace(
            open_index=lambda *a: object(),
            dense_search=lambda *a: {"results": [OTHER]},
        )
        monkeypatch.setattr(
            "elice_knowledge.tools.keyword_search", lambda *a: {"results": [SOURCE]}
        )
        with mlflow.start_span(name="techblog-qa", trace_destination=trace_backend) as root:
            result, _, _ = run(
                monkeypatch,
                [
                    step("keyword_search", arguments={"query": "GPU"}),
                    step("meaning_search", arguments={"query": "공유 방식"}),
                    step("answer", source_ids=[1]),
                    ANSWER,
                ],
                tools=tools,
            )
    spans = persisted(root)
    names = [s.name for s in spans]
    assert result.status == "answered"
    assert names.count("elice.agentic_rag") == 1
    assert names.count("plan") == 3 and names.count("act") == 2
    assert names.count("generation.chat") == 4
    for tool in ("keyword_search", "meaning_search"):
        parent = next(s for s in spans if s.name == tool)
        retriever = next(s for s in spans if s.name == "retrieval." + tool)
        assert parent.span_type == "TOOL" and retriever.span_type == "RETRIEVER"
        assert retriever.parent_id == parent.span_id
        assert retriever.inputs["query"]
        assert retriever.outputs[0]["page_content"]
    context = next(s for s in spans if s.name == "retrieval.final_context")
    assert [doc["id"] for doc in context.outputs] == [SOURCE["chunk_id"]]
    validator = next(s for s in spans if s.name == "answer.parse_and_validate")
    assert validator.outputs["status"] == "answered"
    assert validator.attributes["elice.citation.outcome"] == "valid"
    models = [s for s in spans if s.span_type == "LLM"]
    assert len(models) == 4
    assert all(s.attributes["mlflow.chat.tokenUsage"]["total_tokens"] == 25 for s in models)
    assert all(s.inputs["messages"] and s.outputs["messages"] for s in models)
    assert "private-serverless-key" not in json.dumps([s.to_dict() for s in spans], default=str)


def test_tool_failure_has_error_status_without_upstream_secrets(trace_backend, monkeypatch):
    with httpx.Client() as client:
        tools = KnowledgeTools(generation_settings(), INDEX, client)
        tools._open = lambda: object()

        def fail(*a):
            raise RuntimeError("canary-password-do-not-export")

        monkeypatch.setattr("elice_knowledge.tools.keyword_search", fail)
        with mlflow.start_span(name="tool-failure", trace_destination=trace_backend) as root:
            output, error, _ = tools.timed_invoke("keyword_search", {"query": "GPU"})
    assert output is None and isinstance(error, ToolUnavailable)
    spans = persisted(root)
    assert all(s.status.status_code.value == "ERROR" for s in spans if s.name != "tool-failure")
    assert "canary-password-do-not-export" not in json.dumps(
        [s.to_dict() for s in spans], default=str
    )


def test_untraced_request_stays_untraced_after_autologging(trace_backend, monkeypatch):
    completed = []
    with mlflow.tracing.configure(span_processors=[lambda span: completed.append(span.name)]):
        result, _, _ = run(monkeypatch, [step("clarify")], mlflow_enabled=False)
    assert result.status == "needs_clarification"
    assert completed == []


def test_child_span_failure_does_not_break_answer(trace_backend, monkeypatch):
    with mlflow.start_span(name="tracing-outage", trace_destination=trace_backend):

        def broken(*a, **kw):
            raise RuntimeError("telemetry unavailable")

        monkeypatch.setattr(mlflow, "start_span", broken)
        result, _, _ = run(
            monkeypatch,
            [step("keyword_search", arguments={"query": "GPU"}), step("answer"), ANSWER],
        )
    assert result.status == "answered"
