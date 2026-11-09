FROM ghcr.io/astral-sh/uv:0.11.2 AS uv
FROM python:3.12-slim

COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:$PATH" \
    MLFLOW_HTTP_REQUEST_TIMEOUT=10 MLFLOW_HTTP_REQUEST_MAX_RETRIES=0 \
    MLFLOW_SUPPRESS_PRINTING_URL_TO_STDOUT=true INGEST_REPORT_DIR=/artifacts/ingest
WORKDIR /app
COPY apps/knowledge/pyproject.toml apps/knowledge/uv.lock ./
RUN uv sync --locked --no-dev --no-install-project
COPY apps/knowledge/src ./src
COPY corpus ./corpus
RUN uv sync --locked --no-dev --no-editable \
    && useradd --uid 10001 --create-home app \
    && mkdir -p /artifacts/ingest && chown -R app:app /artifacts
USER app
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health/live', timeout=3)"
CMD ["uvicorn", "elice_knowledge.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8080"]
