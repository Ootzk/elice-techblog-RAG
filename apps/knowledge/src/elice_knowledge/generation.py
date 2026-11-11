"""Elice Serverless Chat Completions adapter, with no implicit model or endpoint."""

from collections.abc import Sequence

import httpx
from llama_index.core.llms import (
    ChatMessage,
    ChatResponse,
    CompletionResponse,
    CustomLLM,
    LLMMetadata,
    MessageRole,
)
from llama_index.core.llms.callbacks import llm_chat_callback, llm_completion_callback
from pydantic import PrivateAttr

from elice_knowledge.settings import Settings
from elice_knowledge.tracing import child_span, finish_span


class GenerationError(RuntimeError):
    code = "generation_failed"

    def __init__(self, message, *, status=None):
        super().__init__(message)
        if status is not None:
            self.code = f"generation_http_{status}"


class GenerationNotConfigured(GenerationError):
    code = "generation_not_configured"


class GenerationTimeout(GenerationError):
    code = "generation_timeout"


class GenerationResponseError(GenerationError):
    code = "invalid_generation_response"


class EliceServerlessLLM(CustomLLM):
    _settings: Settings = PrivateAttr()
    _client: httpx.Client = PrivateAttr()
    _usage_records: list[dict[str, int]] = PrivateAttr(default_factory=list)

    def __init__(self, settings: Settings, client: httpx.Client):
        super().__init__()
        if not settings.generation_configured:
            raise GenerationNotConfigured("Configure generation base URL, API key and model")
        self._settings = settings
        self._client = client

    @property
    def metadata(self) -> LLMMetadata:
        return LLMMetadata(model_name=self._settings.generation_model, is_chat_model=True)

    @llm_chat_callback()
    def chat(self, messages: Sequence[ChatMessage], **kwargs) -> ChatResponse:
        with child_span(
            "generation.chat",
            "LLM",
            {"messages": [{"role": m.role.value, "content": m.content} for m in messages]},
            {
                "mlflow.llm.model": self._settings.generation_model,
                "mlflow.llm.provider": "elice-serverless",
            },
        ) as span:
            response = self._chat(messages, **kwargs)
            usage = response.additional_kwargs["usage"]
            finish_span(
                span,
                {"messages": [{"role": "assistant", "content": response.message.content}]},
                {
                    "mlflow.llm.model": response.additional_kwargs["model"],
                    "mlflow.chat.tokenUsage": {
                        target: usage[source]
                        for source, target in [
                            ("prompt_tokens", "input_tokens"),
                            ("completion_tokens", "output_tokens"),
                            ("total_tokens", "total_tokens"),
                            ("cached_tokens", "cache_read_input_tokens"),
                        ]
                        if source in usage
                    },
                },
            )
            return response

    def _chat(self, messages: Sequence[ChatMessage], **kwargs) -> ChatResponse:
        settings = self._settings
        payload = {
            "model": settings.generation_model,
            "messages": [{"role": m.role.value, "content": m.content} for m in messages],
            "stream": False,
        }
        from elice_knowledge.model_options import validate_generation_options

        payload.update(validate_generation_options({**settings.generation_options, **kwargs}))
        try:
            response = self._client.post(
                settings.generation_base_url.get_secret_value().rstrip("/") + "/chat/completions",
                headers={
                    "Authorization": "Bearer " + settings.generation_api_key.get_secret_value()
                },
                json=payload,
                timeout=settings.generation_timeout_seconds,
                follow_redirects=False,
            )
            response.raise_for_status()
        except httpx.TimeoutException:
            raise GenerationTimeout("Generation request timed out") from None
        except httpx.HTTPStatusError as exc:
            raise GenerationError(
                "Generation endpoint rejected the request", status=exc.response.status_code
            ) from None
        except httpx.HTTPError:
            # Do not expose upstream bodies, request headers, or endpoint URLs.
            raise GenerationError("Generation endpoint request failed") from None
        try:
            data = response.json()
            choice = data["choices"][0]
            content = choice["message"]["content"]
            if (
                choice.get("finish_reason") != "stop"
                or not isinstance(content, str)
                or not content.strip()
            ):
                raise ValueError("Incomplete or empty generation")
            raw_usage = data.get("usage") or {}
            usage = {
                key: value
                for key, value in raw_usage.items()
                if key in {"prompt_tokens", "completion_tokens", "total_tokens"}
                and type(value) is int
                and value >= 0
            }
            details = raw_usage.get("prompt_tokens_details")
            cached = details.get("cached_tokens") if isinstance(details, dict) else None
            if type(cached) is int and 0 <= cached <= usage.get("prompt_tokens", cached):
                usage["cached_tokens"] = cached
            actual_model = data.get("model")
            if not isinstance(actual_model, str):
                actual_model = settings.generation_model
        except (ValueError, KeyError, IndexError, TypeError, AttributeError):
            raise GenerationResponseError("Invalid Chat Completions response") from None
        self._usage_records.append(usage)
        return ChatResponse(
            message=ChatMessage(role=MessageRole.ASSISTANT, content=content),
            additional_kwargs={"model": actual_model, "usage": usage},
        )

    def usage_summary(self):
        """A question total is known only if every successful call reported it."""
        records = self._usage_records
        fields = ("prompt_tokens", "cached_tokens", "completion_tokens", "total_tokens")
        coverage = {field: sum(field in record for record in records) for field in fields}
        return {
            "llm_call_count": len(records),
            "usage_reported_calls": coverage,
            "usage": {
                field: sum(record[field] for record in records)
                for field in fields
                if records and coverage[field] == len(records)
            },
        }

    @llm_completion_callback()
    def complete(self, prompt: str, formatted: bool = False, **kwargs) -> CompletionResponse:
        response = self.chat([ChatMessage(role=MessageRole.USER, content=prompt)], **kwargs)
        return CompletionResponse(
            text=response.message.content, additional_kwargs=response.additional_kwargs
        )

    @llm_completion_callback()
    def stream_complete(self, prompt: str, formatted: bool = False, **kwargs):
        raise NotImplementedError("Streaming is not enabled")
