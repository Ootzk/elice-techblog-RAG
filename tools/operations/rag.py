"""Operations entry point for configuring and evaluating the RAG service."""

import argparse
import importlib.util
import json
import os
import subprocess
import sys
from datetime import UTC
from pathlib import Path

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[2]


def models_module():
    spec = importlib.util.spec_from_file_location(
        "rag_models", ROOT / "tools/models/models.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def environment(env_file):
    models = models_module()
    path = models.safe_path(env_file)
    if not path.is_file():
        raise ValueError("환경 파일이 없습니다")
    env = os.environ.copy()
    env.update({k: v for k, v in dotenv_values(path).items() if v is not None})
    if env.get("RAG_HOST_DATABASE_URL"):
        env["DATABASE_URL"] = env["RAG_HOST_DATABASE_URL"]
    if env.get("RAG_HOST_MLFLOW_TRACKING_URI"):
        env["MLFLOW_TRACKING_URI"] = env["RAG_HOST_MLFLOW_TRACKING_URI"]
    return env


def run(args, env):
    subprocess.run([sys.executable, *args], cwd=ROOT, env=env, check=True)


def doctor(env_file, catalog):
    from elice_knowledge.settings import Settings
    from mlflow import MlflowClient

    env = environment(env_file)
    os.environ.update(env)
    settings = Settings()
    profiles = models_module().read_catalog(catalog)
    result = {
        "model_profiles": {key: sorted(value) for key, value in profiles.items()},
        "answer_configured": settings.generation_configured,
        "embedding_configured": bool(
            settings.embedding_base_url and settings.embedding_model
        ),
        "judge_raters": [x for x in settings.evaluation_judge_raters.split(",") if x],
        "experiment": settings.qa_mlflow_experiment_name,
    }
    if not settings.mlflow_tracking_uri:
        raise ValueError("MLFLOW_TRACKING_URI가 필요합니다")
    client = MlflowClient(tracking_uri=settings.mlflow_tracking_uri.get_secret_value())
    result["mlflow_connected"] = True
    result["experiment_exists"] = (
        client.get_experiment_by_name(settings.qa_mlflow_experiment_name) is not None
    )
    from urllib.error import URLError
    from urllib.request import urlopen

    try:
        port = int(env.get("KNOWLEDGE_PORT", "8080"))
        with urlopen(f"http://127.0.0.1:{port}/health/ready", timeout=3) as response:
            health = json.load(response)
        result["knowledge"] = health.get("status", "unknown")
        result["dependencies"] = health.get("dependencies", {})
    except (URLError, OSError, ValueError):
        result["knowledge"] = "unreachable"
    print(json.dumps(result, ensure_ascii=False, indent=2))


def init_environment(env_file, kind):
    import secrets

    models = models_module()
    target = models.safe_path(env_file)
    if target.exists():
        raise ValueError("환경 파일이 이미 있습니다")
    source = ROOT / (".env.dev.example" if kind == "dev" else ".env.example")
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w") as out:
            out.write(source.read_text())
        values = {
            "KNOWLEDGE_ENV_FILE": str(target.relative_to(ROOT)),
            "ADMIN_ACCESS_CODE": secrets.token_urlsafe(32),
            "AUTH_SESSION_SECRET": secrets.token_urlsafe(48),
            "EVALUATION_API_TOKEN": secrets.token_urlsafe(32),
        }
        if kind == "dev":
            password = secrets.token_urlsafe(24)
            values.update(
                {
                    "POSTGRES_PASSWORD": password,
                    "DATABASE_URL": f"postgresql://knowledge:{password}@postgres:5432/knowledge",
                    "RAG_HOST_DATABASE_URL": f"postgresql://knowledge:{password}@127.0.0.1:5432/knowledge",
                }
            )
        models.save_env(target, values)
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    print(
        "환경 파일을 만들었습니다. 비밀값은 출력하지 않으며 ELICE_API_KEY와 MLflow 접속을 입력하세요."
    )


def setup(args):
    models = models_module()
    env = environment(args.env_file)
    data = models.read_catalog(args.catalog)
    if not all((args.experiment, args.answer, args.embedding, args.judge)):
        if not sys.stdin.isatty():
            raise ValueError(
                "대화식 입력이 불가능합니다. 실험 이름과 모델을 옵션으로 지정하세요"
            )
        print("사용 가능한 모델:")
        for kind, profiles in data.items():
            print(f"  {kind}: {', '.join(profiles) or '(없음)'}")
        args.experiment = args.experiment or input("MLflow 실험 이름: ").strip()
        args.answer = args.answer or input("답변 모델 프로필: ").strip()
        args.embedding = args.embedding or input("임베딩 모델 프로필: ").strip()
        args.judge = args.judge or [
            x.strip()
            for x in input("Judge 프로필 (쉼표 구분): ").split(",")
            if x.strip()
        ]
    for kind, name in (("answer", args.answer), ("embedding", args.embedding)):
        if name not in data[kind]:
            raise ValueError(f"{kind}/{name}: 등록된 프로필이 없습니다")
    if not args.judge or len(set(args.judge)) != len(args.judge):
        raise ValueError("중복 없는 Judge를 하나 이상 지정하세요")
    for name in args.judge:
        models.validate_name(name)
        if name not in data["judge"]:
            raise ValueError(f"judge/{name}: 등록된 프로필이 없습니다")
    if not args.experiment.strip():
        raise ValueError("실험 이름이 필요합니다")
    if not env.get("ELICE_API_KEY"):
        if not sys.stdin.isatty():
            raise ValueError("환경 파일에 ELICE_API_KEY가 필요합니다")
        from getpass import getpass

        key = getpass("Elice Serverless API 키 (입력 내용 숨김): ")
        if not key.strip():
            raise ValueError("API 키가 비어 있습니다")
        models.save_env(args.env_file, {"ELICE_API_KEY": key})
        env["ELICE_API_KEY"] = key
    # Validate everything we can before modifying the service configuration.
    for kind, name in (("answer", args.answer), ("embedding", args.embedding)):
        profile = models.resolve_profile(data, kind, name, env)
        prefix = "GENERATION_" if kind == "answer" else "EMBEDDING_"
        if any(
            not profile.get(key.removeprefix(prefix).lower())
            for key in models.REQUIRED[kind]
        ):
            raise ValueError(f"{kind}/{name}: 필수 모델 설정이 없습니다")
    models.select("answer", args.answer, args.catalog, args.env_file)
    models.select("embedding", args.embedding, args.catalog, args.env_file)
    models.save_env(
        args.env_file,
        {
            "QA_MLFLOW_EXPERIMENT_NAME": args.experiment,
            "MLFLOW_EXPERIMENT_NAME": args.experiment,
            "EVALUATION_JUDGE_RATERS": ",".join(args.judge),
            "EVALUATION_JUDGES_ENABLED": "true",
        },
    )
    env = environment(args.env_file)
    # Existing resource registration is idempotent. Calls do not perform inference.
    models.register_judges(args.catalog, args.env_file)
    run(
        [
            "-m",
            "elice_knowledge",
            "prompt",
            "register",
            "--alias",
            env.get("PROMPT_ALIAS") or "development",
        ],
        env,
    )
    run(["tools/evaluation/register.py"], env)
    command = ["tools/evaluation/judges.py"]
    if env.get("APP_ENV", "prod") == "dev":
        command.append("--enable-dev")
    run(command, env)
    print("설정 완료. 환경 변경을 적용하려면 Knowledge와 worker를 재생성하세요.")


def serve_mlflow(args):
    import socket
    import time
    import urllib.request
    from importlib.metadata import version

    env = environment(args.env_file)
    if not env.get("MLFLOW_CRYPTO_KEK_PASSPHRASE"):
        raise ValueError("환경 파일에 MLFLOW_CRYPTO_KEK_PASSPHRASE가 필요합니다")
    if not 1 <= args.port <= 65535:
        raise ValueError("포트 번호는 1부터 65535 사이여야 합니다")
    with socket.socket() as probe:
        try:
            probe.bind(("127.0.0.1", args.port))
        except OSError:
            raise ValueError("선택한 로컬 포트가 이미 사용 중입니다") from None
    directory = (ROOT / "data" / "mlflow-local").resolve()
    directory.mkdir(parents=True, exist_ok=True)
    pidfile = directory / "server.pid"
    if pidfile.exists():
        raise ValueError("MLflow PID 파일이 이미 있습니다. 실행 상태를 확인하세요")
    command = [
        "uv",
        "run",
        "--project",
        "apps/knowledge",
        "--locked",
        "--with",
        f"mlflow=={version('mlflow-skinny')}",
        "mlflow",
        "server",
        "--backend-store-uri",
        f"sqlite:///{directory / 'mlflow.db'}",
        "--artifacts-destination",
        str(directory / "artifacts"),
        "--host",
        "127.0.0.1",
        "--port",
        str(args.port),
        "--workers",
        "1",
    ]
    with (directory / "server.log").open("a") as log:
        proc = subprocess.Popen(
            command, cwd=ROOT, env=env, stdout=log, stderr=log, start_new_session=True
        )
    pidfile.write_text(str(proc.pid))
    for _ in range(60):
        if proc.poll() is not None:
            pidfile.unlink(missing_ok=True)
            raise ValueError(
                "MLflow 서버 기동에 실패했습니다. data/mlflow-local/server.log를 확인하세요"
            )
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{args.port}/health", timeout=1
            ) as response:
                if response.status == 200:
                    if args.configure:
                        models_module().save_env(
                            args.env_file,
                            {
                                "MLFLOW_TRACKING_URI": f"http://host.docker.internal:{args.port}",
                                "RAG_HOST_MLFLOW_TRACKING_URI": f"http://127.0.0.1:{args.port}",
                            },
                        )
                    print(f"MLflow 서버 실행 중: http://127.0.0.1:{args.port}")
                    return
        except OSError:
            time.sleep(1)
    proc.terminate()
    pidfile.unlink(missing_ok=True)
    raise ValueError("MLflow 기동 시간 초과")


def stop_mlflow():
    import signal

    pidfile = ROOT / "data" / "mlflow-local" / "server.pid"
    if not pidfile.exists():
        raise ValueError("관리 중인 MLflow PID 파일이 없습니다")
    pid = int(pidfile.read_text())
    result = subprocess.run(
        ["ps", "-p", str(pid), "-o", "command="],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        pidfile.unlink()
        print("서버가 이미 종료되었습니다.")
        return
    if "mlflow" not in result.stdout or "server" not in result.stdout:
        raise ValueError("PID가 다른 프로세스를 가리킵니다. 직접 확인하세요")
    os.killpg(pid, signal.SIGTERM)
    pidfile.unlink()
    print("로컬 MLflow 서버를 종료했습니다. 데이터는 보존됩니다.")


def list_datasets(env_file):
    from elice_knowledge.settings import Settings
    from elice_knowledge.tracking import client

    os.environ.update(environment(env_file))
    settings = Settings()
    tracking = client(settings)
    experiment = tracking.get_experiment_by_name(settings.qa_mlflow_experiment_name)
    if experiment is None:
        raise ValueError("설정된 MLflow 실험이 없습니다")
    datasets = tracking.search_datasets(experiment_ids=[experiment.experiment_id])
    print(
        json.dumps(
            [{"name": item.name, "dataset_id": item.dataset_id} for item in datasets],
            ensure_ascii=False,
            indent=2,
        )
    )


def create_agreement(args):
    import re

    from elice_knowledge.settings import Settings
    from elice_knowledge.tracking import client

    if not re.fullmatch(r"[a-f0-9]{32}", args.answers_run_id):
        raise ValueError("답변 Run ID가 올바르지 않습니다")
    env = environment(args.env_file)
    os.environ.update(env)
    tracking = client(Settings())
    answers = tracking.get_run(args.answers_run_id)
    if (
        answers.info.status not in {"FINISHED", "FAILED"}
        or answers.data.tags.get("evaluation.kind") != "answers"
    ):
        raise ValueError("종료된 answers Run이 필요합니다")
    runs = tracking.search_runs(
        [answers.info.experiment_id],
        filter_string=f"tags.`source_run_id` = '{args.answers_run_id}' and tags.`evaluation.kind` = 'grading'",
        max_results=1000,
        order_by=["attributes.start_time ASC"],
    )
    if len(runs) >= 1000:
        raise ValueError(
            "채점 Run이 너무 많습니다. review 명령으로 명시적으로 선택하세요"
        )
    matches = [
        r
        for r in runs
        if r.info.status == "FINISHED"
        and (not args.rater or r.data.tags.get("rater_id") in args.rater)
        and (not args.round or r.data.tags.get("round") in {str(n) for n in args.round})
    ]
    if not matches:
        raise ValueError("조건에 맞는 완료된 채점 Run이 없습니다")
    if args.rater and set(args.rater) != {r.data.tags.get("rater_id") for r in matches}:
        raise ValueError("지정한 평가자 중 조건에 맞는 완료된 채점 Run이 없습니다")
    command = [
        "tools/evaluation/review.py",
        "agreement",
        "--source-run-id",
        args.answers_run_id,
        "--output-dir",
        args.output_dir,
    ]
    for item in matches:
        command += ["--grading-run-id", item.info.run_id]
    if args.log:
        command.append("--log")
    print(f"완료된 채점 {len(matches)}회를 사용합니다.", flush=True)
    run(command, env)


def list_runs(env_file, kind, limit):
    from datetime import datetime

    from elice_knowledge.settings import Settings
    from elice_knowledge.tracking import client

    os.environ.update(environment(env_file))
    settings = Settings()
    tracking = client(settings)
    experiment = tracking.get_experiment_by_name(settings.qa_mlflow_experiment_name)
    if experiment is None:
        raise ValueError("설정된 MLflow 실험이 없습니다")
    if not 1 <= limit <= 200:
        raise ValueError("조회 개수는 1부터 200까지 가능합니다")
    filter_string = f"tags.`evaluation.kind` = '{kind}'" if kind else ""
    runs = tracking.search_runs(
        [experiment.experiment_id],
        filter_string=filter_string,
        max_results=limit,
        order_by=["attributes.start_time DESC"],
    )
    result = []
    for run in runs:
        result.append(
            {
                "run_id": run.info.run_id,
                "kind": run.data.tags.get("evaluation.kind"),
                "name": run.data.tags.get("mlflow.runName"),
                "status": run.info.status,
                "source_run_id": run.data.tags.get("source_run_id"),
                "started_at": datetime.fromtimestamp(
                    run.info.start_time / 1000, tz=UTC
                ).isoformat()
                if run.info.start_time
                else None,
            }
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))


def manage_service(env_file, action):
    """Run the project Compose lifecycle without touching shared MLflow."""
    env = environment(env_file)
    compose = [
        "docker",
        "compose",
        "--env-file",
        str(models_module().safe_path(env_file)),
        "-f",
        "compose.yaml",
    ]
    development = env.get("APP_ENV", "prod") == "dev"
    if development:
        compose += ["-f", "compose.dev.yaml"]

    def execute(*arguments):
        subprocess.run(compose + list(arguments), cwd=ROOT, env=env, check=True)

    if action == "init-db":
        if development:
            execute("up", "-d", "--wait", "postgres")
            execute("build", "knowledge")
        execute(
            "run",
            "--rm",
            "--no-deps",
            "knowledge",
            "python",
            "-m",
            "elice_knowledge",
            "init-db",
        )
    elif action == "start":
        execute(
            "up",
            "-d",
            *(["--build"] if development else ["--no-build"]),
            "knowledge",
            "worker",
        )
    elif action == "stop":
        execute("stop", "knowledge", "worker")
    else:
        execute("ps")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--env-file", default=".env.dev")
    p.add_argument("--catalog", default="models.json")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("doctor")
    init = sub.add_parser("init-env")
    init.add_argument("--kind", choices=("dev", "prod"), default="dev")
    sub.add_parser("datasets")
    agreement = sub.add_parser("agreement")
    agreement.add_argument("--answers-run-id", required=True)
    agreement.add_argument("--output-dir", required=True)
    agreement.add_argument("--rater", action="append")
    agreement.add_argument("--round", type=int, action="append")
    agreement.add_argument("--log", action="store_true")
    runs = sub.add_parser("runs")
    runs.add_argument(
        "--kind",
        choices=("answers", "grading", "agreement", "repeatability", "comparison"),
    )
    runs.add_argument("--limit", type=int, default=20)
    sub.add_parser("mlflow-stop")
    service = sub.add_parser("serve")
    service.add_argument("action", choices=("init-db", "start", "stop", "status"))
    report = sub.add_parser("report")
    report.add_argument("kind", choices=("agreement", "repeatability", "comparison"))
    report.add_argument("--run-id", required=True)
    report.add_argument("--output", required=True)
    server = sub.add_parser("mlflow-start")
    server.add_argument("--port", type=int, default=5000)
    server.add_argument(
        "--configure", action="store_true", help="로컬 서버를 환경 파일에 연결"
    )
    x = sub.add_parser("setup")
    x.add_argument("--experiment")
    x.add_argument("--answer")
    x.add_argument("--embedding")
    x.add_argument("--judge", action="append")
    for name in (
        "model",
        "corpus",
        "prompt",
        "dataset",
        "evaluate",
        "review",
        "random-baseline",
        "repeatability",
        "comparison",
        "comparison-agreement",
        "judge",
    ):
        x = sub.add_parser(name)
        x.add_argument("arguments", nargs=argparse.REMAINDER)
    passthrough = {
        "model",
        "corpus",
        "prompt",
        "dataset",
        "evaluate",
        "review",
        "random-baseline",
        "repeatability",
        "comparison",
        "comparison-agreement",
        "judge",
    }
    raw = sys.argv[1:]
    index = next(
        (
            i
            for i, item in enumerate(raw)
            if item in passthrough
            and (i == 0 or raw[i - 1] not in {"--env-file", "--catalog", "report"})
        ),
        None,
    )
    if index is None:
        args = p.parse_args(raw)
    else:
        args = p.parse_args(raw[: index + 1])
        args.arguments = raw[index + 1 :]
    try:
        if args.command == "init-env":
            init_environment(args.env_file, args.kind)
        elif args.command == "doctor":
            doctor(args.env_file, args.catalog)
        elif args.command == "datasets":
            list_datasets(args.env_file)
        elif args.command == "runs":
            list_runs(args.env_file, args.kind, args.limit)
        elif args.command == "agreement":
            create_agreement(args)
        elif args.command == "setup":
            setup(args)
        elif args.command == "serve":
            manage_service(args.env_file, args.action)
        elif args.command == "mlflow-start":
            serve_mlflow(args)
        elif args.command == "mlflow-stop":
            stop_mlflow()
        elif args.command == "report":
            env = environment(args.env_file)
            subprocess.run(
                [
                    "uv",
                    "run",
                    "--project",
                    "tools/reporting",
                    "--locked",
                    "python",
                    "tools/operations/export_report.py",
                    args.kind,
                    "--run-id",
                    args.run_id,
                    "--output",
                    args.output,
                ],
                cwd=ROOT,
                env=env,
                check=True,
            )
        else:
            env = environment(args.env_file)
            commands = {
                "model": [
                    "tools/models/models.py",
                    "--env-file",
                    args.env_file,
                    "--catalog",
                    args.catalog,
                ],
                "corpus": ["-m", "elice_knowledge"],
                "prompt": ["-m", "elice_knowledge", "prompt"],
                "dataset": ["tools/evaluation/register.py"],
                "evaluate": ["tools/evaluation/evaluate.py"],
                "review": ["tools/evaluation/review.py"],
                "random-baseline": ["tools/evaluation/random_baseline.py"],
                "repeatability": ["tools/evaluation/repeatability.py"],
                "comparison": ["tools/evaluation/compare_repeated.py"],
                "comparison-agreement": ["tools/evaluation/compare.py"],
                "judge": ["tools/evaluation/judges.py"],
            }
            run(commands[args.command] + args.arguments, env)
    except ValueError as exc:
        print(f"작업 실패: {exc}", file=sys.stderr)
        return 1
    except (OSError, subprocess.CalledProcessError):
        print("작업 실패: 실행 환경 또는 하위 명령을 확인하세요.", file=sys.stderr)
        return 1
    except Exception:  # noqa: BLE001 - provider errors may contain private endpoints or keys
        print("작업 실패: MLflow 연결과 리소스 설정을 확인하세요.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
