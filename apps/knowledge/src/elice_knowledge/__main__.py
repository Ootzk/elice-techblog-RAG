import argparse
import json
from pathlib import Path

import httpx

from elice_knowledge.chunk_store import run_chunking
from elice_knowledge.chunking import ChunkingPolicy
from elice_knowledge.database import initialize_database, store_and_search
from elice_knowledge.embedding import OllamaEmbedder, profile_id
from elice_knowledge.ingest import run_ingest
from elice_knowledge.settings import Settings


def main():
    parser = argparse.ArgumentParser(description="Knowledge ingestion and infrastructure checks")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("init-db", help="Initialize connectivity and corpus tables")
    subparsers.add_parser("smoke", help="Check embedding, vector storage and MLflow")
    ingest = subparsers.add_parser("ingest", help="Collect and version Korean techblog articles")
    ingest.add_argument(
        "--manifest", type=Path, help="Use a fixed URL manifest instead of discovery"
    )
    ingest.add_argument("--report", type=Path, help="Write the collection report to this path")
    ingest.add_argument("--force", action="store_true", help="Re-download without HTTP validators")
    chunk = subparsers.add_parser(
        "chunk", help="Chunk an archived corpus snapshot without downloads"
    )
    chunk.add_argument("--snapshot-id", help="Corpus snapshot ID; defaults to the latest snapshot")
    chunk.add_argument("--profile", choices=["h2", "every_heading", "page"], default="h2")
    chunk.add_argument("--overflow", choices=["sentence", "none"], default="sentence")
    chunk.add_argument("--chunk-size", type=int, default=512)
    chunk.add_argument("--chunk-overlap", type=int, default=64)
    args = parser.parse_args()
    stage = "configuration"
    try:
        settings = Settings()
        if args.command == "chunk":
            stage = "chunking"
            policy = ChunkingPolicy(
                profile=args.profile,
                overflow=args.overflow,
                chunk_size=args.chunk_size,
                chunk_overlap=args.chunk_overlap,
            )
            print(json.dumps(run_chunking(settings, policy, args.snapshot_id)))
            return
        if args.command == "ingest":
            stage = "ingest"
            result = run_ingest(
                settings, manifest=args.manifest, report_path=args.report, force=args.force
            )
            print(json.dumps(result))
            if result["status"] != "ok":
                raise SystemExit(1)
            return
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
            from elice_knowledge.tracking import record_connectivity

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
