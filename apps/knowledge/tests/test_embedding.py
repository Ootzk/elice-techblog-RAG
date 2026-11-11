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


def serverless_settings(**overrides):
    values = dict(
        embedding_backend="elice-serverless",
        embedding_base_url="https://embedding.test/deployment/v1",
        embedding_api_key="private-key",
        embedding_model="text-embedding-3-small",
        embedding_dimensions=2,
        embedding_batch_size=2,
    )
    values.update(overrides)
    return Settings(_env_file=None, **values)


def test_serverless_batch_order_auth_and_profile():
    import json

    from elice_knowledge.embedding import create_embedder

    requests = []

    def handler(request):
        assert request.url.path == "/deployment/v1/embeddings"
        assert request.headers["authorization"] == "Bearer private-key"
        payload = json.loads(request.content)
        assert payload["model"] == "text-embedding-3-small"
        assert payload["encoding_format"] == "float"
        requests.append(payload)
        return httpx.Response(
            200,
            json={
                "model": "text-embedding-3-small",
                "data": [
                    {"index": i, "embedding": [int(text), 1]}
                    for i, text in reversed(list(enumerate(payload["input"])))
                ],
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        backend = create_embedder(serverless_settings(), client)
        profile = backend.profile()
        assert backend.embed(["1", "2", "3"]) == [[1, 1], [2, 1], [3, 1]]
    assert [r["input"] for r in requests] == [["1", "2"], ["3"]]
    assert (
        profile["backend"] == "elice-serverless" and profile["revision"] == "provider-unavailable"
    )
    assert "base_url" not in profile and "api_key" not in profile
    assert profile_id(profile) != profile_id({**profile, "revision": "new-revision"})


@pytest.mark.parametrize(
    "data",
    [
        [],
        [{"index": 0, "embedding": [0, 0]}],
        [{"index": 0, "embedding": [1]}],
        [{"index": 1, "embedding": [1, 2]}],
        [{"index": True, "embedding": [1, 2]}],
        [{"index": 0, "embedding": [float("nan"), 2]}],
    ],
)
def test_serverless_rejects_bad_vectors(data):
    import json

    from elice_knowledge.embedding import EliceServerlessEmbedder

    with httpx.Client(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, content=json.dumps({"data": data}))
        )
    ) as client:
        with pytest.raises(EmbeddingError):
            EliceServerlessEmbedder(serverless_settings(), client).embed(["input"])


def test_serverless_blocks_oversized_input_locally():
    from elice_knowledge.embedding import EliceServerlessEmbedder, EmbeddingInputTooLong

    def handler(request):
        pytest.fail("oversized text must not be sent")

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(EmbeddingInputTooLong):
            EliceServerlessEmbedder(serverless_settings(), client).embed([" test" * 8192])


def test_serverless_rejects_unexpected_model():
    from elice_knowledge.embedding import EliceServerlessEmbedder

    with httpx.Client(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200, json={"model": "other", "data": [{"index": 0, "embedding": [1, 2]}]}
            )
        )
    ) as client:
        with pytest.raises(EmbeddingError, match="response model"):
            EliceServerlessEmbedder(serverless_settings(), client).embed(["input"])


def test_embedding_execution_options_are_sent_and_identify_vector_contract():
    import json

    from elice_knowledge.embedding import EliceServerlessEmbedder, profile_id

    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={"model": "text-embedding-3-small", "data": [{"index": 0, "embedding": [1, 1]}]},
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        plain = EliceServerlessEmbedder(serverless_settings(), client)
        selected = EliceServerlessEmbedder(
            serverless_settings(embedding_options={"dimensions": 2}), client
        )
        assert selected.embed(["text"]) == [[1, 1]]
        assert requests[0]["dimensions"] == 2
        assert profile_id(plain.profile()) != profile_id(selected.profile())
        with pytest.raises(EmbeddingError, match="dimensions"):
            EliceServerlessEmbedder(
                serverless_settings(embedding_options={"dimensions": 3}), client
            ).profile()
