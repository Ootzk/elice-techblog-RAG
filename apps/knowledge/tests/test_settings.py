import pytest
from pydantic import ValidationError

from elice_knowledge.settings import Settings


@pytest.mark.parametrize(
    "field,value",
    [
        ("mlflow_tracking_uri", "file:///tmp/mlruns"),
        ("database_url", "sqlite:///data.db"),
        ("embedding_base_url", "ollama.test"),
        ("embedding_batch_size", 0),
        ("embedding_timeout_seconds", -1),
    ],
)
def test_invalid_configuration_is_rejected(field, value):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{field: value})


def test_missing_prod_endpoints_have_no_dev_fallback():
    settings = Settings(_env_file=None, app_env="prod")
    assert settings.database_url is None
    assert settings.embedding_base_url is None
    assert settings.mlflow_tracking_uri is None


def test_common_elice_key_configures_generation_and_serverless_embedding():
    from elice_knowledge.settings import Settings

    settings = Settings(elice_api_key="shared-key", embedding_backend="elice-serverless")
    assert settings.generation_api_key.get_secret_value() == "shared-key"
    assert settings.embedding_api_key.get_secret_value() == "shared-key"


def test_selected_models_override_environment_values(tmp_path):
    import json

    from elice_knowledge.settings import Settings

    catalog = tmp_path / "models.json"
    catalog.write_text(
        json.dumps(
            {
                "answer": {"new": {"model": "chosen", "base_url": "https://example.test/v1"}},
                "embedding": {
                    "embed": {
                        "backend": "elice-serverless",
                        "model": "text-embedding-3-small",
                        "base_url": "https://example.test/v1",
                        "dimensions": 1536,
                    }
                },
            }
        )
    )
    settings = Settings(
        model_catalog_file=catalog,
        answer_model_profile="new",
        embedding_model_profile="embed",
        generation_model="stale",
        embedding_model="stale",
        elice_api_key="shared",
    )
    assert settings.generation_model == "chosen"
    assert settings.embedding_model == "text-embedding-3-small"
    assert settings.embedding_dimensions == 1536
    assert settings.generation_api_key.get_secret_value() == "shared"
