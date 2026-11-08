import hashlib
import json
import math
from typing import Protocol

import httpx

from elice_knowledge.settings import Settings


class EmbeddingError(ValueError):
    pass


class Embedder(Protocol):
    def embed(self, texts: list[str]) -> list[list[float]]: ...

    def profile(self) -> dict: ...


def profile_id(profile: dict) -> str:
    return hashlib.sha256(
        json.dumps(profile, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


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
            result = self._request(
                "/api/embed",
                {
                    "model": self.settings.embedding_model,
                    "input": batch,
                    "truncate": False,
                },
            )
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
