from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore", hide_input_in_errors=True)

    app_env: Literal["dev", "prod"] = "prod"
    database_url: SecretStr | None = None
    embedding_backend: Literal["ollama"] = "ollama"
    embedding_base_url: SecretStr | None = None
    embedding_model: str = "bge-m3"
    embedding_api_key: SecretStr | None = None
    embedding_timeout_seconds: float = Field(default=30, gt=0, le=300)
    embedding_batch_size: int = Field(default=16, gt=0, le=256)
    embedding_dimensions: int = Field(default=1024, gt=0, le=16000)
    mlflow_enabled: bool = True
    mlflow_tracking_uri: SecretStr | None = None
    mlflow_experiment_name: str = Field(default="elice-techblog-rag-connectivity", min_length=1)
    crawl_interval_seconds: float = Field(default=0.5, ge=0, le=60)
    crawl_timeout_seconds: float = Field(default=30, gt=0, le=120)
    crawl_retries: int = Field(default=2, ge=0, le=5)
    crawl_max_pages: int = Field(default=100, ge=1, le=1000)
    corpus_min_documents: int = Field(default=20, ge=1)
    ingest_report_dir: Path = Path("artifacts/ingest")

    @model_validator(mode="after")
    def validate_endpoints(self):
        for field, schemes in (
            ("database_url", {"postgres", "postgresql"}),
            ("embedding_base_url", {"http", "https"}),
            ("mlflow_tracking_uri", {"http", "https"}),
        ):
            value = getattr(self, field)
            if value is not None:
                parsed = urlsplit(value.get_secret_value())
                if parsed.scheme not in schemes or not parsed.hostname:
                    raise ValueError(f"{field} must be a valid remote endpoint")
        return self
