import json

import httpx
import pytest
from llama_index.core.llms import ChatMessage, MessageRole

from elice_knowledge.generation import (
    EliceServerlessLLM,
    GenerationError,
    GenerationNotConfigured,
    GenerationResponseError,
    GenerationTimeout,
)
from elice_knowledge.settings import Settings


def generation_settings(**values):
    return Settings(
        generation_base_url="https://mlapi.test/endpoint/v1",
        generation_api_key="private-serverless-key",
        generation_model="selected-model",
        **values,
    )


def completion(text="answer", **values):
    return {
        "model": "selected-model-revision",
        "choices": [{"message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
        "usage": {
            "prompt_tokens": 20,
            "completion_tokens": 5,
            "total_tokens": 25,
            "private_debug": "do-not-expose",
        },
        **values,
    }


def test_elice_chat_contract_roles_auth_and_no_implicit_model_parameters():
    requests = []

    def handler(request):
        requests.append(request)
        assert str(request.url) == "https://mlapi.test/endpoint/v1/chat/completions"
        assert request.headers["authorization"] == "Bearer private-serverless-key"
        payload = json.loads(request.content)
        assert set(payload) == {"model", "messages", "stream"}
        assert payload["model"] == "selected-model" and payload["stream"] is False
        assert [m["role"] for m in payload["messages"]] == ["system", "user"]
        return httpx.Response(200, json=completion())

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        llm = EliceServerlessLLM(generation_settings(), client)
        result = llm.chat(
            [
                ChatMessage(role=MessageRole.SYSTEM, content="rules"),
                ChatMessage(role=MessageRole.USER, content="question"),
            ]
        )
        assert result.message.content == "answer"
        assert result.additional_kwargs == {
            "model": "selected-model-revision",
            "usage": {
                "prompt_tokens": 20,
                "completion_tokens": 5,
                "total_tokens": 25,
            },
        }
    assert len(requests) == 1


def test_optional_parameters_are_only_sent_when_explicitly_configured():
    def handler(request):
        payload = json.loads(request.content)
        assert payload["temperature"] == 0
        assert payload["max_tokens"] == 200 and "max_completion_tokens" not in payload
        return httpx.Response(200, json=completion())

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        llm = EliceServerlessLLM(
            generation_settings(
                generation_options={"temperature": 0, "max_tokens": 200},
            ),
            client,
        )
        assert llm.complete("question").text == "answer"


@pytest.mark.parametrize("status", [302, 401, 429, 500])
def test_failures_are_not_retried_or_leaked(status):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            status,
            headers={"location": "https://private-host/redirect"},
            text="private-serverless-key private-endpoint",
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(GenerationError) as error:
            EliceServerlessLLM(generation_settings(), client).complete("question")
        assert "private-" not in str(error.value)
        assert error.value.code == f"generation_http_{status}"
    assert len(requests) == 1


@pytest.mark.parametrize(
    "response",
    [
        {},
        {"choices": []},
        completion(""),
        completion(None),
        completion(choices=[{"message": {"content": "partial"}, "finish_reason": "length"}]),
    ],
)
def test_malformed_or_incomplete_responses_are_not_accepted(response):
    with httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=response))
    ) as client:
        with pytest.raises(GenerationResponseError):
            EliceServerlessLLM(generation_settings(), client).complete("question")


def test_timeout_has_a_safe_error_type():
    def handler(_):
        raise httpx.ReadTimeout("private-endpoint")

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(GenerationTimeout) as error:
            EliceServerlessLLM(generation_settings(), client).complete("question")
        assert "private-" not in str(error.value)


def test_empty_templates_do_not_enable_generation():
    settings = Settings(generation_base_url="", generation_api_key="", generation_model="")
    assert not settings.generation_configured
    with httpx.Client() as client:
        with pytest.raises(GenerationNotConfigured):
            EliceServerlessLLM(settings, client)


@pytest.mark.parametrize(
    "url",
    [
        "https://mlapi.test/endpoint",
        "https://mlapi.test/v1?key=secret",
        "https://user:password@mlapi.test/v1",
    ],
)
def test_base_url_is_explicit_and_has_no_embedded_credentials(url):
    with pytest.raises(ValueError):
        Settings(generation_base_url=url)


@pytest.mark.parametrize("cached", [0, 7, None, -1, True, "7", 21])
def test_cached_tokens_are_optional_validated_and_not_added_to_total(cached):
    usage = {
        "prompt_tokens": 20,
        "completion_tokens": 5,
        "total_tokens": 25,
        "prompt_tokens_details": {"cached_tokens": cached},
    }
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=completion(usage=usage))
        )
    ) as client:
        llm = EliceServerlessLLM(generation_settings(), client)
        response = llm.complete("Q")
        actual = response.additional_kwargs["usage"]
        assert actual["total_tokens"] == 25
        if type(cached) is int and 0 <= cached <= 20:
            assert actual["cached_tokens"] == cached
        else:
            assert "cached_tokens" not in actual


def test_question_usage_omits_totals_with_incomplete_reporting():
    responses = iter(
        [
            completion(
                usage={
                    "prompt_tokens": 20,
                    "completion_tokens": 5,
                    "total_tokens": 25,
                    "prompt_tokens_details": {"cached_tokens": 0},
                }
            ),
            completion(usage={"prompt_tokens": 30, "completion_tokens": 6, "total_tokens": 36}),
        ]
    )
    with httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=next(responses)))
    ) as client:
        llm = EliceServerlessLLM(generation_settings(), client)
        llm.complete("planner")
        llm.complete("answer")
        summary = llm.usage_summary()
    assert summary["llm_call_count"] == 2
    assert summary["usage"]["prompt_tokens"] == 50
    assert summary["usage"]["total_tokens"] == 61
    assert "cached_tokens" not in summary["usage"]
    assert summary["usage_reported_calls"]["cached_tokens"] == 1


def test_call_kwargs_override_profile_options():
    def handler(request):
        payload = json.loads(request.content)
        assert payload["temperature"] == 0.6
        assert payload["seed"] == 7
        return httpx.Response(200, json=completion())

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        llm = EliceServerlessLLM(
            generation_settings(generation_options={"temperature": 0.2}), client
        )
        assert llm.complete("question", temperature=0.6, seed=7).text == "answer"
        with pytest.raises(ValueError, match="forbidden"):
            llm.complete("question", model="other")
