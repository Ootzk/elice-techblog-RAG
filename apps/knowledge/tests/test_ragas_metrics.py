"""Real Ragas algorithms with structured fake LLM outputs, never paid requests."""

import asyncio

import pytest

from elice_knowledge.ragas_metrics import contract, measurement_metrics, metric_score, payload
from elice_knowledge.settings import Settings


def test_real_ragas_metrics_and_gateway_adapter(monkeypatch):
    from ragas.llms.base import InstructorBaseRagasLLM

    configuration, prompts = [], []

    class FakeClient:
        def __init__(self, **kwargs):
            self.http = kwargs["http_client"]
            configuration.append(kwargs)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            await self.http.aclose()

    class FakeLLM(InstructorBaseRagasLLM):
        model_args = {"max_tokens": 1024}

        def generate(self, *args):
            raise AssertionError("Only async metrics expected")

        async def agenerate(self, prompt, response_model):
            prompts.append(response_model.__name__)
            name = response_model.__name__
            if name == "ClaimDecompositionOutput":
                data = {"claims": ["supported", "unsupported"]}
            elif name == "StatementGeneratorOutput":
                data = {"statements": ["supported", "unsupported"]}
            elif name == "NLIStatementOutput":
                data = {
                    "statements": [
                        dict(statement=s, reason="fake", verdict=v)
                        for s, v in [("supported", 1), ("unsupported", 0)]
                    ]
                }
            elif name == "ContextRecallOutput":
                data = {
                    "classifications": [
                        dict(statement=s, reason="fake", attributed=v)
                        for s, v in [("supported", 1), ("unsupported", 0)]
                    ]
                }
            elif name == "ContextPrecisionOutput":
                data = {"reason": "fake", "verdict": int("good_doc" in prompt)}
            else:
                data = {"reason": "fake", "value": "2"}
            return response_model(**data)

    monkeypatch.setattr("openai.AsyncOpenAI", FakeClient)
    monkeypatch.setattr("ragas.llms.llm_factory", lambda model, **kw: FakeLLM())
    settings = Settings(mlflow_tracking_uri="http://mlflow.test:5000")

    async def check():
        for dimension in ("factual_precision",):
            score, reason, _ = await metric_score(
                settings,
                "gateway:/configured-judge",
                dimension,
                dict(response="answer", reference="reference"),
                "",
            )
            assert score == 0.5 and "NLIStatementOutput" in reason
        score, _, _ = await metric_score(
            settings,
            "gateway:/configured-judge",
            "faithfulness",
            dict(user_input="q", response="answer", retrieved_contexts=["source"]),
            "",
        )
        assert score == 0.5
        data = dict(
            user_input="q", reference="reference", retrieved_contexts=["good_doc", "bad_doc"]
        )
        score, _, _ = await metric_score(
            settings, "gateway:/configured-judge", "retrieved_context_recall", data, ""
        )
        assert score == 0.5
        score, _, _ = await metric_score(
            settings, "gateway:/configured-judge", "required_point_coverage", data, ""
        )
        assert score == 0.5
        score, _, _ = await metric_score(
            settings, "gateway:/configured-judge", "retrieved_context_precision", data, ""
        )
        assert score == pytest.approx(1.0)  # Average Precision, not a 1/2 document fraction.
        data["retrieved_contexts"].reverse()
        score, _, _ = await metric_score(
            settings, "gateway:/configured-judge", "generation_context_precision", data, ""
        )
        assert score == pytest.approx(0.5)
        score, _, _ = await metric_score(
            settings,
            "gateway:/configured-judge",
            "relevance",
            dict(payload="data"),
            "rubric",
            allowed_values=["0", "1", "2", "not_applicable"],
        )
        assert score == "2"

    asyncio.run(check())
    assert all(c["base_url"] == "http://mlflow.test:5000/gateway/mlflow/v1" for c in configuration)
    assert all(c["max_retries"] == 0 for c in configuration)
    assert len(prompts) > 7


def test_scope_deduplication_na_and_measurement_coverage():
    record = dict(inputs={"question": "q"}, expectations={"answerable": True, "answer": "a"})
    case = dict(
        result={
            "status": "answered",
            "generation_sources": [{"text": "good"}, {"text": "good"}],
            "retrieved_sources": [{"text": "bad"}, {"text": "good"}, {"text": "bad"}],
        }
    )
    assert payload(record, case, "generation_context_recall")["retrieved_contexts"] == ["good"]
    assert payload(record, case, "retrieved_context_precision")["retrieved_contexts"] == [
        "bad",
        "good",
    ]
    record["expectations"]["answerable"] = False
    assert payload(record, case, "generation_context_recall") is None
    summary = measurement_metrics(
        [
            dict(metric="factual_precision", score=0.5, status="scored"),
            dict(metric="factual_precision", score=None, status="error"),
            dict(metric="factual_precision", score=None, status="not_applicable"),
        ]
    )
    assert summary["ragas.factual_precision.mean"] == 0.5
    assert (
        summary["ragas.factual_precision.scored_cases"]
        == summary["ragas.factual_precision.error_cases"]
        == 1
    )
    assert contract()["engine"] == "ragas"
    assert len(contract()["implementation_digest"]) == 64


@pytest.mark.parametrize(
    "streaming,finish_reason,wire_value",
    [
        (False, "stop", "2"),
        (False, "stop", 2),
        (True, "stop", "2"),
        (True, "stop", 2),
        (True, "length", "2"),
    ],
)
def test_real_instructor_and_openai_sdk_use_gateway_structured_json(
    monkeypatch, streaming, finish_reason, wire_value
):
    import json

    import httpx

    requests = []
    original_client = httpx.AsyncClient

    def transport(request):
        requests.append(request)
        if streaming:
            content = json.dumps({"value": wire_value, "reason": "검증"})
            chunks = [
                {"content": content[:15], "role": "assistant"},
                {"content": content[15:]},
                {},
            ]
            events = [
                dict(
                    id="fake-completion",
                    object="chat.completion.chunk",
                    created=1,
                    model="configured-judge",
                    choices=[
                        dict(index=0, delta=delta, finish_reason=finish_reason if i == 2 else None)
                    ],
                )
                for i, delta in enumerate(chunks)
            ]
            body = "".join("data: " + json.dumps(e) + "\n\n" for e in events)
            return httpx.Response(
                200,
                content=(body + "data: [DONE]\n\n").encode(),
                headers={"content-type": "text/event-stream"},
            )
        return httpx.Response(
            200,
            json={
                "id": "fake-completion",
                "object": "chat.completion",
                "created": 1,
                "model": "configured-judge",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {
                            "role": "assistant",
                            "content": json.dumps({"value": wire_value, "reason": "검증"}),
                        },
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            },
        )

    class MockClient(original_client):
        def __init__(self, **kwargs):
            super().__init__(transport=httpx.MockTransport(transport), **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", MockClient)
    monkeypatch.setenv("MLFLOW_TRACKING_TOKEN", "fake-gateway-token")
    request_options = (
        {"reasoning_effort": "none", "stream": True, "max_completion_tokens": 8192}
        if streaming
        else {"temperature": 0.1, "top_p": 0.9, "max_tokens": 300}
        if wire_value == 2
        else None
    )

    def run():
        return asyncio.run(
            metric_score(
                Settings(mlflow_tracking_uri="http://mlflow.test:5000"),
                "gateway:/configured-judge",
                "relevance",
                {"payload": "candidate"},
                "Check relevance",
                request_options=request_options,
                allowed_values=["0", "1", "2", "not_applicable"],
            )
        )

    if finish_reason == "length":
        from openai import LengthFinishReasonError

        with pytest.raises(LengthFinishReasonError):
            run()
    else:
        score, reason, _ = run()
        assert score == wire_value and reason == "검증"
    assert len(requests) == 1
    assert str(requests[0].url) == "http://mlflow.test:5000/gateway/mlflow/v1/chat/completions"
    body = json.loads(requests[0].content)
    assert body["model"] == "configured-judge"
    assert body["response_format"]["type"] == ("json_schema" if streaming else "json_object")
    assert contract()["temperature"] is None and contract()["top_p"] is None
    if request_options and not streaming:
        assert body["temperature"] == 0.1 and body["top_p"] == 0.9 and body["max_tokens"] == 300
    else:
        assert "temperature" not in body and "top_p" not in body and "max_tokens" not in body
    if streaming:
        assert body["max_completion_tokens"] == 8192
    else:
        assert "max_completion_tokens" not in body
    if streaming:
        assert body["stream"] and body["reasoning_effort"] == "none"
    assert requests[0].headers["authorization"] == "Bearer fake-gateway-token"
