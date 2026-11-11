import hashlib
import json
import math
from typing import Protocol

import httpx

from elice_knowledge.settings import Settings


class EmbeddingError(ValueError):
    pass


class EmbeddingInputTooLong(EmbeddingError):
    pass


class Embedder(Protocol):
    def embed(self, texts: list[str]) -> list[list[float]]: ...

    def profile(self) -> dict: ...


def profile_id(profile: dict) -> str:
    return hashlib.sha256(
        json.dumps(profile, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def create_embedder(settings: Settings, client: httpx.Client) -> Embedder:
    """Select the explicitly configured embedding backend."""
    if settings.embedding_backend == "ollama":
        return OllamaEmbedder(settings, client)
    if settings.embedding_backend == "elice-serverless":
        return EliceServerlessEmbedder(settings, client)
    raise EmbeddingError("Unsupported embedding backend")


class OllamaEmbedder:
    def __init__(self, settings: Settings, client: httpx.Client):
        self.settings = settings
        self.client = client

    def _request(self, path: str, payload: dict | None = None) -> dict:
        endpoint = self.settings.embedding_base_url
        if endpoint is None:
            raise EmbeddingError("EMBEDDING_BASE_URL is required")
        headers = {}
        if self.settings.embedding_api_key and self.settings.embedding_api_key.get_secret_value():
            headers["Authorization"] = (
                f"Bearer {self.settings.embedding_api_key.get_secret_value()}"
            )
        url = endpoint.get_secret_value().rstrip("/") + path
        response = self.client.request(
            "POST" if payload is not None else "GET",
            url,
            json=payload,
            headers=headers,
            timeout=self.settings.embedding_timeout_seconds,
        )
        response.raise_for_status()
        result = response.json()
        if not isinstance(result, dict):
            raise EmbeddingError("Expected an Ollama JSON object")
        return result

    def profile(self) -> dict:
        if self.settings.embedding_options:
            raise EmbeddingError("Ollama embedding request options are not supported")
        model = self.settings.embedding_model
        shown = self._request("/api/show", {"model": model})
        tags = self._request("/api/tags")
        canonical = model if ":" in model else f"{model}:latest"
        matches = [
            tag
            for tag in tags.get("models", [])
            if tag.get("name") in {model, canonical} or tag.get("model") in {model, canonical}
        ]
        if len(matches) != 1 or not matches[0].get("digest"):
            raise EmbeddingError("Unable to identify the installed model digest")
        if "embedding" not in shown.get("capabilities", []):
            raise EmbeddingError("Configured model does not support embeddings")
        return {
            "backend": "ollama",
            "model": canonical,
            "digest": matches[0]["digest"],
            "dimensions": self.settings.embedding_dimensions,
            "details": shown.get("details", {}),
            "model_info": shown.get("model_info", {}),
            "parameters": shown.get("parameters", ""),
            "preprocessing": "identity",
            "truncate": False,
            "normalization": "ollama-api-embed-unit-length",
            "pooling": "model-defined",
            "length_limit": "model-context-window; reject-overflow",
        }

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts or any(not text.strip() for text in texts):
            raise EmbeddingError("At least one non-empty input is required")
        vectors = []
        size = self.settings.embedding_batch_size
        for start in range(0, len(texts), size):
            batch = texts[start : start + size]
            try:
                result = self._request(
                    "/api/embed",
                    {"model": self.settings.embedding_model, "input": batch, "truncate": False},
                )
            except httpx.HTTPStatusError as exc:
                # Never log response bodies: a backend may echo private input.
                detail = exc.response.text.lower()
                if exc.response.status_code == 400 and (
                    "input length exceeds" in detail or "context length" in detail
                ):
                    raise EmbeddingInputTooLong(
                        "Input exceeds the embedding context window"
                    ) from None
                raise
            received = result.get("embeddings")
            if not isinstance(received, list) or len(received) != len(batch):
                raise EmbeddingError("Embedding count differs from input count")
            for vector in received:
                if (
                    not isinstance(vector, list)
                    or len(vector) != self.settings.embedding_dimensions
                ):
                    raise EmbeddingError("Unexpected embedding dimensions")
                if any(
                    isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v)
                    for v in vector
                ) or not any(v != 0 for v in vector):
                    raise EmbeddingError("Embedding must contain finite numbers and be non-zero")
            # Ollama /api/embed returns one vector per input in request order.
            vectors.extend(received)
        return vectors


class EliceServerlessEmbedder:
    """OpenAI-compatible embedding endpoint, with explicit server-side error handling."""

    def __init__(self, settings: Settings, client: httpx.Client):
        self.settings = settings
        self.client = client

    def profile(self) -> dict:
        if not self.settings.embedding_base_url:
            raise EmbeddingError("EMBEDDING_BASE_URL is required")
        if (
            not self.settings.embedding_api_key
            or not self.settings.embedding_api_key.get_secret_value()
        ):
            raise EmbeddingError("EMBEDDING_API_KEY is required")
        if not self.settings.embedding_model.strip():
            raise EmbeddingError("EMBEDDING_MODEL is required")
        if self.settings.embedding_model.removeprefix("openai/") != "text-embedding-3-small":
            raise EmbeddingError("Serverless embedding model token contract is not configured")
        from elice_knowledge.model_options import validate_embedding_options

        options = validate_embedding_options(self.settings.embedding_options)
        if "dimensions" in options and options["dimensions"] != self.settings.embedding_dimensions:
            raise EmbeddingError("Embedding dimensions option differs from configured dimensions")
        return {
            **({"request_options": options} if options else {}),
            "backend": "elice-serverless",
            "api": "openai-embeddings-v1",
            "model": self.settings.embedding_model,
            "revision": self.settings.embedding_model_revision or "provider-unavailable",
            "dimensions": self.settings.embedding_dimensions,
            "preprocessing": "identity",
            "truncate": False,
            "normalization": "provider-defined",
            "pooling": "provider-defined",
            "length_limit": "8191 cl100k_base tokens; reject-overflow",
        }

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts or any(not text.strip() for text in texts):
            raise EmbeddingError("At least one non-empty input is required")
        self.profile()
        import tiktoken

        encoding = tiktoken.get_encoding("cl100k_base")
        if any(len(encoding.encode_ordinary(text)) > 8191 for text in texts):
            raise EmbeddingInputTooLong("Input exceeds the embedding context window")
        vectors = []
        for start in range(0, len(texts), self.settings.embedding_batch_size):
            batch = texts[start : start + self.settings.embedding_batch_size]
            api_key = self.settings.embedding_api_key.get_secret_value()
            try:
                response = self.client.post(
                    self.settings.embedding_base_url.get_secret_value().rstrip("/") + "/embeddings",
                    headers={"Authorization": f"Bearer {api_key}"},
                    json={
                        "model": self.settings.embedding_model,
                        "input": batch,
                        "encoding_format": "float",
                        **self.settings.embedding_options,
                    },
                    timeout=self.settings.embedding_timeout_seconds,
                )
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 400 and any(
                    term in exc.response.text.lower()
                    for term in ("maximum context length", "max tokens", "input is too long")
                ):
                    raise EmbeddingInputTooLong(
                        "Input exceeds the embedding context window"
                    ) from None
                raise
            result = response.json()
            received = result.get("data") if isinstance(result, dict) else None
            if not isinstance(received, list) or len(received) != len(batch):
                raise EmbeddingError("Embedding count differs from input count")
            indexed = {}
            for item in received:
                if not isinstance(item, dict) or type(item.get("index")) is not int:
                    raise EmbeddingError("Embedding index is missing or invalid")
                index, vector = item["index"], item.get("embedding")
                if index not in range(len(batch)) or index in indexed:
                    raise EmbeddingError("Embedding indices are invalid or duplicated")
                if (
                    not isinstance(vector, list)
                    or len(vector) != self.settings.embedding_dimensions
                ):
                    raise EmbeddingError("Unexpected embedding dimensions")
                if any(
                    isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v)
                    for v in vector
                ) or not any(v != 0 for v in vector):
                    raise EmbeddingError("Embedding must contain finite numbers and be non-zero")
                indexed[index] = vector
            returned_model = result.get("model")
            if not isinstance(returned_model, str) or returned_model not in {
                self.settings.embedding_model,
                self.settings.embedding_model.removeprefix("openai/"),
                "openai/" + self.settings.embedding_model.removeprefix("openai/"),
            }:
                raise EmbeddingError("Embedding response model differs from configured model")
            vectors.extend(indexed[i] for i in range(len(batch)))
        return vectors
