from fastapi.testclient import TestClient

from elice_knowledge.main import create_app
from elice_knowledge.settings import Settings


def test_live_works_without_configuration_and_ready_reports_each_dependency():
    client = TestClient(create_app(Settings(_env_file=None)))
    assert client.get("/health/live").json() == {"status": "ok"}
    response = client.get("/health/ready")
    assert response.status_code == 503
    assert set(response.json()["dependencies"]) == {"database", "embedding", "mlflow"}
    assert all(item["status"] == "error" for item in response.json()["dependencies"].values())


def test_ready_is_read_only_and_allows_tracking_disabled(monkeypatch):
    monkeypatch.setattr("elice_knowledge.main.check_database", lambda _: None)
    monkeypatch.setattr("elice_knowledge.main.OllamaEmbedder.profile", lambda _: {})
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
