import httpx
import pytest

from elice_knowledge.embedding import EmbeddingError, OllamaEmbedder, profile_id
from elice_knowledge.settings import Settings


def settings(**values):
    return Settings(
        _env_file=None,
        embedding_base_url="http://ollama.test",
        embedding_dimensions=2,
        embedding_batch_size=2,
        **values,
    )


def test_batch_order_auth_and_no_truncation():
    requests = []

    def handler(request):
        import json

        payload = json.loads(request.content)
        requests.append(payload)
        assert request.headers["authorization"] == "Bearer private-key"
        assert payload["truncate"] is False
        return httpx.Response(200, json={"embeddings": [[int(x), 1] for x in payload["input"]]})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = OllamaEmbedder(settings(embedding_api_key="private-key"), client).embed(
            ["1", "2", "3"]
        )
    assert result == [[1, 1], [2, 1], [3, 1]]
    assert [r["input"] for r in requests] == [["1", "2"], ["3"]]


@pytest.mark.parametrize(
    "vectors", [[], [[1, 2], [2, 3]], [[1]], [[0, 0]], [[True, 1]], [["x", 1]]]
)
def test_rejects_corrupt_or_misaligned_embeddings(vectors):
    with httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"embeddings": vectors}))
    ) as client:
        with pytest.raises(EmbeddingError):
            OllamaEmbedder(settings(), client).embed(["sample"])


def test_profile_requires_installed_digest_and_embedding_capability():
    def handler(request):
        return httpx.Response(
            200,
            json=(
                {"capabilities": ["embedding"], "details": {"quantization_level": "F16"}}
                if request.url.path == "/api/show"
                else {"models": [{"name": "bge-m3:latest", "digest": "sha256:abc"}]}
            ),
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        profile = OllamaEmbedder(settings(), client).profile()
    assert profile["digest"] == "sha256:abc"
    assert profile_id(profile) != profile_id({**profile, "digest": "sha256:changed"})
    assert "base_url" not in profile


def test_http_failure_is_not_silently_replaced_with_another_backend():
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(503))) as client:
        with pytest.raises(httpx.HTTPStatusError):
            OllamaEmbedder(settings(), client).embed(["sample"])


def test_empty_optional_key_does_not_send_invalid_bearer_header():
    def handler(request):
        assert "authorization" not in request.headers
        return httpx.Response(200, json={"embeddings": [[1, 2]]})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert OllamaEmbedder(settings(embedding_api_key=""), client).embed(["sample"]) == [[1, 2]]


def test_oversized_input_is_rejected_without_retrying_with_truncation():
    from elice_knowledge.embedding import EmbeddingInputTooLong

    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(400, json={"error": "the input length exceeds the context length"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(EmbeddingInputTooLong):
            OllamaEmbedder(settings(), client).embed(["long input"])
    assert len(requests) == 1
