"""Run a paid, isolated smoke test against a disposable PostgreSQL and MLflow."""

import json
import os
import re
import secrets
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import psycopg
from dotenv import dotenv_values, set_key
from mlflow import MlflowClient

ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = ROOT / "tmp/full-test/.env"
WORK = ENV_FILE.parent
RAG = [sys.executable, "tools/operations/rag.py", "--env-file", str(ENV_FILE)]
MODEL_OPTIONS = json.dumps(
    {
        "chatgpt": {
            "reasoning_effort": "none",
            "stream": True,
            "max_completion_tokens": 8192,
        }
    }
)


def command(*args):
    result = subprocess.run(
        [*RAG, *args], cwd=ROOT, text=True, capture_output=True, check=False
    )
    if result.returncode:
        # Provider output and URLs can contain credentials; do not publish raw logs.
        match = re.search(r"API request failed: HTTP (\d{3})", result.stderr)
        detail = f"; HTTP {match.group(1)}" if match else ""
        raise RuntimeError(
            f"{' '.join(args[:2])} failed (exit {result.returncode}){detail}; output withheld"
        )
    return result.stdout


def json_line(output, key):
    for line in output.splitlines():
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict) and key in value:
            return value
    raise RuntimeError(f"missing {key} in command output")


def env():
    values = os.environ.copy()
    values.update({k: v for k, v in dotenv_values(ENV_FILE).items() if v is not None})
    values["DATABASE_URL"] = values["RAG_HOST_DATABASE_URL"]
    values["MLFLOW_TRACKING_URI"] = values["RAG_HOST_MLFLOW_TRACKING_URI"]
    return values


def main():
    key = os.environ.get("ELICE_API_KEY")
    db = os.environ.get("FULL_TEST_DATABASE_URL")
    if not key or not db:
        raise RuntimeError("ELICE_API_KEY and FULL_TEST_DATABASE_URL are required")
    if ENV_FILE.exists() or (ROOT / "data/mlflow-local").exists():
        raise RuntimeError("full-test environment or local MLflow data already exists")
    with psycopg.connect(db) as conn:
        count = conn.execute(
            "SELECT count(*) FROM pg_catalog.pg_tables WHERE schemaname = 'public'"
        ).fetchone()[0]
        if count:
            raise RuntimeError(
                "PostgreSQL must be empty; refusing to use an existing database"
            )
    WORK.mkdir(parents=True, exist_ok=True)
    command("init-env")
    for name, value in {
        "ELICE_API_KEY": key,
        "DATABASE_URL": db,
        "RAG_HOST_DATABASE_URL": db,
        "MLFLOW_CRYPTO_KEK_PASSPHRASE": secrets.token_urlsafe(48),
        "KNOWLEDGE_PORT": "18082",
        "CORPUS_MIN_DOCUMENTS": "1",
    }.items():
        set_key(str(ENV_FILE), name, value, quote_mode="always")
    processes = []
    mlflow_started = False
    try:
        print(
            "Starting fresh MLflow and registering models, datasets, prompts and judges.",
            flush=True,
        )
        command("mlflow-start", "--port", "15782", "--configure")
        mlflow_started = True
        command(
            "setup",
            "--experiment",
            "full-test",
            "--answer",
            "terra",
            "--embedding",
            "text-embedding-3-small",
            "--judge",
            "chatgpt",
            "--judge",
            "claude",
            "--judge",
            "gemini",
        )
        command("corpus", "init-db")
        manifest = json.loads((ROOT / "corpus/manifest.json").read_text())
        url = "https://elice.io/ko/resources/blog/ai-storage-poc-workload-comparison"
        manifest["documents"] = [
            doc for doc in manifest["documents"] if doc["url"] == url
        ]
        if len(manifest["documents"]) != 1:
            raise RuntimeError("Gold sample source is missing from manifest")
        manifest_path = WORK / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False))
        print(
            "Collecting one pinned document and creating real embeddings.", flush=True
        )
        command("corpus", "ingest", "--manifest", str(manifest_path))
        chunk = json_line(command("corpus", "chunk"), "policy_key")
        index = json_line(
            command("corpus", "index", "--policy-key", chunk["policy_key"]), "index_key"
        )
        if index["created"] < 1:
            raise RuntimeError("Fresh index did not create vectors")
        runtime = env()
        for cmd in (
            [
                sys.executable,
                "-m",
                "uvicorn",
                "elice_knowledge.main:create_app",
                "--factory",
                "--host",
                "127.0.0.1",
                "--port",
                "18082",
            ],
            [sys.executable, "-m", "elice_knowledge", "worker"],
        ):
            processes.append(
                subprocess.Popen(
                    cmd,
                    cwd=ROOT,
                    env=runtime,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            )
        for _ in range(90):
            if any(p.poll() is not None for p in processes):
                raise RuntimeError("Knowledge or worker terminated during startup")
            try:
                with urllib.request.urlopen(
                    "http://127.0.0.1:18082/health/ready", timeout=2
                ) as response:
                    if response.status == 200:
                        break
            except OSError:
                time.sleep(1)
        else:
            raise RuntimeError("Knowledge startup timed out")
        client = MlflowClient(tracking_uri=runtime["MLFLOW_TRACKING_URI"])
        experiment = client.get_experiment_by_name("full-test")
        datasets = client.search_datasets(experiment_ids=[experiment.experiment_id])
        dataset = next(d for d in datasets if d.name == "elice-techblog-gold")
        request = urllib.request.Request(
            "http://127.0.0.1:18082/api/evaluation-datasets/" + dataset.dataset_id,
            headers={"Authorization": "Bearer " + runtime["EVALUATION_API_TOKEN"]},
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            rows = json.load(response)["records"]
        source = json.loads(
            (ROOT / "tools/evaluation/gold_set/gold.jsonl").read_text().splitlines()[0]
        )
        matches = [row["id"] for row in rows if row["inputs"] == source["inputs"]]
        if len(matches) != 1:
            raise RuntimeError("Gold sample was not found uniquely in MLflow")
        report = WORK / "evaluation.json"
        print("Evaluating one Gold question with three judges.", flush=True)
        command(
            "evaluate",
            "--base-url",
            "http://127.0.0.1:18082",
            "--dataset-id",
            dataset.dataset_id,
            "--record-id",
            matches[0],
            "--submit",
            "--wait",
            "--timeout",
            "900",
            "--report",
            str(report),
            "--judge-options",
            MODEL_OPTIONS,
        )
        result = json.loads(report.read_text())["evaluation"]
        summary = result["summary"]
        if summary["completed"] != 1 or summary["failed"] != 0:
            raise RuntimeError("The answer was not completed")
        gradings = summary["grading_runs"]
        if {g["rater_id"] for g in gradings if g["state"] == "FINISHED"} != {
            "model:chatgpt",
            "model:claude",
            "model:gemini",
        }:
            raise RuntimeError("Expected three completed judge runs")
        for grading in gradings:
            metrics = grading["metrics"]
            for dimension in (
                "correctness",
                "completeness",
                "groundedness",
                "relevance",
            ):
                if metrics.get(f"rubric.{dimension}.error_cases") != 0:
                    raise RuntimeError(
                        "A judge returned a scoring error or missing metric"
                    )
                if metrics.get(f"rubric.{dimension}.pending_cases") != 0:
                    raise RuntimeError("A judge left a score pending or missing metric")
                scored = metrics.get(f"rubric.{dimension}.scored_cases", 0)
                not_applicable = metrics.get(
                    f"rubric.{dimension}.not_applicable_cases", 0
                )
                if scored + not_applicable != 1:
                    raise RuntimeError("A judge did not record a result for the sample")
        agreement = json_line(
            command(
                "agreement",
                "--answers-run-id",
                result["mlflow_run_id"],
                "--output-dir",
                str(WORK / "agreement"),
                "--log",
            ),
            "agreement_run_id",
        )
        output = ROOT / "artifacts/full-test/agreement.html"
        output.parent.mkdir(parents=True, exist_ok=True)
        command(
            "report",
            "agreement",
            "--run-id",
            agreement["agreement_run_id"],
            "--output",
            str(output),
        )
        if not output.is_file() or output.stat().st_size == 0:
            raise RuntimeError("Agreement report was not created")
        print(
            f"Passed: 1 document, {index['created']} vectors, 1 answer, 3 judges and {output.relative_to(ROOT)}",
            flush=True,
        )
    finally:
        for process in processes:
            process.terminate()
        for process in processes:
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        if mlflow_started:
            command("mlflow-stop")
        ENV_FILE.unlink(missing_ok=True)


if __name__ == "__main__":
    try:
        main()
    except RuntimeError as exc:
        print(f"Full test failed: {exc}", file=sys.stderr)
        sys.exit(1)
    except Exception:  # noqa: BLE001 - exceptions may include private connection details
        print(
            "Full test failed: check the test environment and connections",
            file=sys.stderr,
        )
        sys.exit(1)
