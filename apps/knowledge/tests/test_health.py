from fastapi.testclient import TestClient

from elice_knowledge.main import create_app
from elice_knowledge.settings import Settings


def test_openapi_version_comes_from_installed_package(monkeypatch):
    monkeypatch.setattr("elice_knowledge.main.version", lambda name: "0.2.1")
    app = create_app(Settings(_env_file=None))
    assert app.openapi()["info"]["version"] == "0.2.1"


def test_live_works_without_configuration_and_ready_reports_each_dependency():
    client = TestClient(create_app(Settings(_env_file=None)))
    assert client.get("/health/live").json() == {"status": "ok"}
    response = client.get("/health/ready")
    assert response.status_code == 503
    assert set(response.json()["dependencies"]) == {"database", "embedding", "mlflow", "generation"}
    assert all(
        response.json()["dependencies"][name]["status"] == "error"
        for name in ("database", "embedding", "mlflow")
    )
    assert response.json()["dependencies"]["generation"]["status"] == "not_configured"


def test_ready_is_read_only_and_allows_tracking_disabled(monkeypatch):
    monkeypatch.setattr("elice_knowledge.main.check_database", lambda _: None)
    monkeypatch.setattr("elice_knowledge.embedding.OllamaEmbedder.profile", lambda _: {})
    client = TestClient(create_app(Settings(_env_file=None, mlflow_enabled=False)))
    response = client.get("/health/ready")
    assert response.status_code == 200
    assert response.json()["dependencies"]["mlflow"]["status"] == "disabled"


def test_dependency_failure_does_not_expose_secret(monkeypatch):
    def failed(_):
        raise ValueError("postgresql://private-user:private-password@private-host/db")

    monkeypatch.setattr("elice_knowledge.main.check_database", failed)
    response = TestClient(create_app(Settings(_env_file=None, mlflow_enabled=False))).get(
        "/health/ready"
    )
    assert response.status_code == 503
    assert "private-" not in response.text
    assert response.json()["dependencies"]["database"]["status"] == "error"


def test_generation_readiness_is_configuration_only_without_paid_calls(monkeypatch):
    from test_generation import generation_settings

    monkeypatch.setattr("elice_knowledge.main.check_database", lambda _: None)
    monkeypatch.setattr("elice_knowledge.embedding.OllamaEmbedder.profile", lambda _: {})
    monkeypatch.setattr(
        "elice_knowledge.generation.EliceServerlessLLM.chat",
        lambda *args: (_ for _ in ()).throw(AssertionError("No generation on health")),
    )
    client = TestClient(create_app(generation_settings(mlflow_enabled=False)))
    response = client.get("/health/ready")
    assert response.status_code == 200
    assert response.json()["dependencies"]["generation"]["status"] == "configured"


def test_serverless_readiness_is_configuration_only(monkeypatch):
    monkeypatch.setattr("elice_knowledge.main.check_database", lambda _: None)
    monkeypatch.setattr(
        "httpx.Client.post",
        lambda *a, **kw: (_ for _ in ()).throw(
            AssertionError("readiness must not call paid embedding endpoint")
        ),
    )
    config = Settings(
        _env_file=None,
        mlflow_enabled=False,
        embedding_backend="elice-serverless",
        embedding_base_url="https://embedding.test/deployment/v1",
        embedding_api_key="secret",
        embedding_model="text-embedding-3-small",
        embedding_dimensions=1536,
    )
    response = TestClient(create_app(config)).get("/health/ready")
    assert response.status_code == 200
    assert response.json()["dependencies"]["embedding"]["status"] == "configured"
