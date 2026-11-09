import argparse
import json
from pathlib import Path

import httpx

from elice_knowledge.chunk_store import run_chunking
from elice_knowledge.chunking import ChunkingPolicy
from elice_knowledge.database import initialize_database, store_and_search
from elice_knowledge.embedding import EmbeddingInputTooLong, OllamaEmbedder, profile_id
from elice_knowledge.execution import execute
from elice_knowledge.generation import GenerationError
from elice_knowledge.ingest import run_ingest
from elice_knowledge.retrieval import run_index, run_search
from elice_knowledge.service_store import ServiceError, ServiceStore
from elice_knowledge.settings import Settings


def main():
    parser = argparse.ArgumentParser(description="Knowledge ingestion and infrastructure checks")
    subparsers = parser.add_subparsers(dest="command", required=True)
    worker = subparsers.add_parser("worker", help="Run durable chat/evaluation worker")
    worker.add_argument("--once", action="store_true")
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
    index = subparsers.add_parser("index", help="Embed stored chunks and publish a dense index")
    index.add_argument("--policy-key", required=True)
    index.add_argument("--snapshot-id", help="Defaults to the latest corpus snapshot")
    search = subparsers.add_parser("search", help="Find chunks and sources in a published index")
    search.add_argument("query")
    search.add_argument("--index-key", required=True)
    search.add_argument("--top-k", type=int, default=5)
    ask = subparsers.add_parser("ask", help="Answer from a selected index using Elice Serverless")
    ask.add_argument("question")
    ask.add_argument("--index-key", required=True)
    ask.add_argument("--top-k", type=int, default=5)
    args = parser.parse_args()
    stage = "configuration"
    try:
        settings = Settings()
        if args.command == "worker":
            from elice_knowledge.worker import run_worker

            run_worker(settings, once=args.once)
            return
        if args.command == "ask":
            stage = "qa"
            store = ServiceStore(settings)
            key = store.active_profile(args.index_key, args.top_k)
            output = execute(store, key, args.question)
            if output["error"]:
                raise ServiceError(output["error"])
            print(json.dumps({**output["result"], "run_id": output["run_id"]}, ensure_ascii=False))
            return
        if args.command == "index":
            stage = "indexing"
            print(json.dumps(run_index(settings, args.policy_key, args.snapshot_id)))
            return
        if args.command == "search":
            stage = "search"
            print(
                json.dumps(
                    run_search(settings, args.index_key, args.query, args.top_k), ensure_ascii=False
                )
            )
            return
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
        result = {"status": "error", "stage": stage, "error_type": type(exc).__name__}
        if isinstance(exc, (GenerationError, ServiceError)):
            result["reason"] = exc.code
        if isinstance(exc, EmbeddingInputTooLong):
            result["reason"] = (
                "embedding_input_too_long; use a smaller chunk policy or handle oversized blocks"
            )
        print(json.dumps(result))
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
