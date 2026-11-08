import argparse
import json

import httpx

from elice_knowledge.database import initialize_database, store_and_search
from elice_knowledge.embedding import OllamaEmbedder, profile_id
from elice_knowledge.settings import Settings
from elice_knowledge.tracking import record_connectivity


def main():
    parser = argparse.ArgumentParser(description="Knowledge infrastructure checks")
    parser.add_argument("command", choices=["init-db", "smoke"])
    args = parser.parse_args()
    stage = "configuration"
    try:
        settings = Settings()
        if args.command == "init-db":
            stage = "database initialization"
            initialize_database(settings)
            print(json.dumps({"status": "ok", "operation": "init-db"}))
            return
        text = "엘리스 한국어 테크블로그 기반 RAG 연결 검증입니다."
        stage = "embedding"
        with httpx.Client() as http:
            embedder = OllamaEmbedder(settings, http)
            before = embedder.profile()
            vector = embedder.embed([text])[0]
            after = embedder.profile()
        if profile_id(before) != profile_id(after):
            raise ValueError("Model changed during embedding; retry with a stable model")
        stage = "database storage and search"
        result = store_and_search(settings, text, vector, before)
        if settings.mlflow_enabled:
            stage = "MLflow recording (database sample already committed)"
            result["mlflow_run_id"] = record_connectivity(settings, result, before)
        else:
            result["mlflow"] = "disabled"
        print(json.dumps({"status": "ok", "purpose": "connectivity-only", **result}))
    except Exception as exc:
        # Keep credentials/addresses out of CLI logs as well as HTTP responses.
        print(json.dumps({"status": "error", "stage": stage, "error_type": type(exc).__name__}))
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
