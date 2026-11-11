"""Operations entry point for configuring and evaluating the RAG service."""

import argparse
import importlib.util
import json
import os
import subprocess
import sys
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


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--env-file", default=".env.dev")
    p.add_argument("--catalog", default="models.json")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("doctor")
    sub.add_parser("mlflow-stop")
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
        "repeatability",
        "comparison",
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
        "repeatability",
        "comparison",
    }
    raw = sys.argv[1:]
    index = next(
        (
            i
            for i, item in enumerate(raw)
            if item in passthrough
            and (i == 0 or raw[i - 1] not in {"--env-file", "--catalog"})
        ),
        None,
    )
    if index is None:
        args = p.parse_args(raw)
    else:
        args = p.parse_args(raw[: index + 1])
        args.arguments = raw[index + 1 :]
    try:
        if args.command == "doctor":
            doctor(args.env_file, args.catalog)
        elif args.command == "setup":
            setup(args)
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
                "repeatability": ["tools/evaluation/repeatability.py"],
                "comparison": ["tools/evaluation/compare_repeated.py"],
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
