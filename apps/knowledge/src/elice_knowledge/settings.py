from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator, model_validator
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
    generation_base_url: SecretStr | None = None
    generation_api_key: SecretStr | None = None
    generation_model: str | None = None
    generation_timeout_seconds: float = Field(default=90, gt=0, le=300)
    generation_temperature: float | None = Field(default=None, ge=0, le=2)
    generation_max_output_tokens: int | None = Field(default=None, gt=0, le=32768)
    generation_token_limit_field: Literal["max_tokens", "max_completion_tokens"] = (
        "max_completion_tokens"
    )
    qa_max_context_tokens: int = Field(default=8192, ge=512, le=65536)
    admin_access_code: SecretStr | None = None
    auth_session_secret: SecretStr | None = None
    evaluation_api_token: SecretStr | None = None
    qa_active_index_key: str | None = None
    chat_session_hours: int = Field(default=168, ge=1, le=720)
    chat_login_attempts_per_minute: int = Field(default=10, ge=1, le=100)
    qa_history_pairs: int = Field(default=3, ge=0, le=10)
    qa_history_tokens: int = Field(default=2048, ge=128, le=8192)
    qa_agent_max_tool_calls: int = Field(default=4, ge=1, le=8)
    # MLflow Prompt Registry alias for new QA profiles; bundled templates are the fallback.
    prompt_alias: str = Field(default="production", pattern=r"^[a-z][a-z0-9_-]{0,62}$")
    mcp_api_token: SecretStr | None = None
    qa_worker_concurrency: int = Field(default=1, ge=1, le=8)
    qa_build_revision: str = "development"
    qa_build_dirty: bool = True
    worker_lease_seconds: int = Field(default=300, ge=30, le=1800)
    web_dist_dir: Path = Path("/app/web")
    qa_mlflow_experiment_name: str = "elice-techblog-rag-evaluation"

    @field_validator("generation_base_url", "generation_api_key", "generation_model", mode="before")
    @classmethod
    def blank_generation_value(cls, value):
        return (value.strip() or None) if isinstance(value, str) else value

    @field_validator("mcp_api_token", mode="before")
    @classmethod
    def blank_mcp_token(cls, value):
        return (value.strip() or None) if isinstance(value, str) else value

    @field_validator("admin_access_code", "auth_session_secret", "mcp_api_token")
    @classmethod
    def authentication_secrets(cls, value, info):
        if value is not None and len(value.get_secret_value()) < (
            32 if info.field_name == "auth_session_secret" else 20
        ):
            raise ValueError(
                "Use at least 32 characters for session secret and 20 for admin/MCP tokens"
            )
        return value

    @property
    def generation_configured(self) -> bool:
        return bool(self.generation_base_url and self.generation_api_key and self.generation_model)

    @model_validator(mode="after")
    def validate_endpoints(self):
        for field, schemes in (
            ("database_url", {"postgres", "postgresql"}),
            ("embedding_base_url", {"http", "https"}),
            ("mlflow_tracking_uri", {"http", "https"}),
            ("generation_base_url", {"http", "https"}),
        ):
            value = getattr(self, field)
            if value is not None:
                parsed = urlsplit(value.get_secret_value())
                if parsed.scheme not in schemes or not parsed.hostname:
                    raise ValueError(f"{field} must be a valid remote endpoint")
        if self.generation_base_url:
            parsed = urlsplit(self.generation_base_url.get_secret_value())
            if (
                parsed.query
                or parsed.fragment
                or parsed.username
                or not parsed.path.rstrip("/").endswith("/v1")
            ):
                raise ValueError(
                    "GENERATION_BASE_URL must be a /v1 API base URL without credentials or query"
                )
        return self
