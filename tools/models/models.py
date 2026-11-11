"""Manage local model profiles without printing credentials or calling inference APIs."""

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

from dotenv import dotenv_values, set_key, unset_key

ROOT = Path(__file__).resolve().parents[2]
FIELDS = {
    "answer": (
        "GENERATION_BASE_URL",
        "GENERATION_API_KEY",
        "GENERATION_MODEL",
        "GENERATION_TEMPERATURE",
        "GENERATION_MAX_OUTPUT_TOKENS",
        "GENERATION_TOKEN_LIMIT_FIELD",
    ),
    "embedding": (
        "EMBEDDING_BACKEND",
        "EMBEDDING_BASE_URL",
        "EMBEDDING_MODEL",
        "EMBEDDING_MODEL_REVISION",
        "EMBEDDING_API_KEY",
        "EMBEDDING_DIMENSIONS",
        "EMBEDDING_TIMEOUT_SECONDS",
        "EMBEDDING_BATCH_SIZE",
        "QA_ACTIVE_INDEX_KEY",
    ),
}
REQUIRED = {
    "answer": ("GENERATION_BASE_URL", "GENERATION_API_KEY", "GENERATION_MODEL"),
    "embedding": (
        "EMBEDDING_BACKEND",
        "EMBEDDING_BASE_URL",
        "EMBEDDING_MODEL",
        "EMBEDDING_DIMENSIONS",
    ),
}


def safe_path(path):
    path = Path(path).resolve()
    try:
        rel = path.relative_to(ROOT)
    except ValueError:
        raise ValueError(
            "환경 파일은 저장소 안의 Git 무시 경로에 있어야 합니다"
        ) from None
    if (
        subprocess.run(
            ["git", "ls-files", "--error-unmatch", "--", str(rel)],
            cwd=ROOT,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        ).returncode
        == 0
    ):
        raise ValueError("추적 중인 파일에는 모델 설정을 저장할 수 없습니다")
    if subprocess.run(
        ["git", "check-ignore", "-q", "--", str(rel)], cwd=ROOT, check=False
    ).returncode:
        raise ValueError("Git이 무시하는 환경 파일을 지정해야 합니다")
    return path


def validate_name(name):
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,40}", name):
        raise ValueError(
            "이름은 영문 소문자로 시작하며 소문자·숫자·하이픈만 사용할 수 있습니다"
        )
    return name


def read_catalog(path):
    path = safe_path(path)
    if not path.exists():
        return {"answer": {}, "embedding": {}, "judge": {}}
    data = json.loads(path.read_text())
    if not isinstance(data, dict) or any(
        not isinstance(data.get(k, {}), dict) for k in ("answer", "embedding", "judge")
    ):
        raise ValueError("모델 카탈로그 JSON 형식이 올바르지 않습니다")
    return {k: data.get(k, {}) for k in ("answer", "embedding", "judge")}


def write_catalog(path, data):
    import tempfile

    path = safe_path(path)
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix=".model-catalog-")
    try:
        with os.fdopen(fd, "w") as out:
            json.dump(data, out, ensure_ascii=False, indent=2)
            out.write("\n")
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def save_env(path, values):
    path = safe_path(path)
    if not path.exists():
        raise ValueError("서비스 환경 파일이 없습니다")
    os.chmod(path, 0o600)
    for key, value in values.items():
        set_key(str(path), key, str(value or ""), quote_mode="always")


def capture(kind, name, source, catalog):
    validate_name(name)
    env = dotenv_values(safe_path(source))
    if any(not env.get(k) for k in REQUIRED[kind]):
        raise ValueError("필수 모델 설정이 빠졌습니다")
    if (
        kind == "embedding"
        and env.get("EMBEDDING_BACKEND") == "elice-serverless"
        and not env.get("EMBEDDING_API_KEY")
    ):
        raise ValueError("Serverless 임베딩 키가 없습니다")
    data = read_catalog(catalog)
    if name in data[kind]:
        raise ValueError("이미 등록된 이름입니다")
    prefix = "GENERATION_" if kind == "answer" else "EMBEDDING_"
    values = {
        key.removeprefix(prefix).lower(): env.get(key)
        for key in FIELDS[kind]
        if env.get(key)
    }
    if kind == "embedding" and env.get("QA_ACTIVE_INDEX_KEY"):
        values["index_key"] = env["QA_ACTIVE_INDEX_KEY"]
    data[kind][name] = values
    write_catalog(catalog, data)


def select(kind, name, catalog, target):
    validate_name(name)
    profile = read_catalog(catalog)[kind].get(name)
    if not isinstance(profile, dict):
        raise TypeError("모델 프로필이 없습니다")
    prefix = "GENERATION_" if kind == "answer" else "EMBEDDING_"
    values = {
        key: str(profile.get(key.removeprefix(prefix).lower()) or "")
        for key in FIELDS[kind]
    }
    if kind == "embedding":
        values["QA_ACTIVE_INDEX_KEY"] = profile.get("index_key", "")
    if any(not values.get(key) for key in REQUIRED[kind]):
        raise ValueError("모델 프로필의 필수 설정이 빠졌습니다")
    if (
        kind == "embedding"
        and values["EMBEDDING_BACKEND"] == "elice-serverless"
        and not values.get("EMBEDDING_API_KEY")
    ):
        raise ValueError("Serverless 임베딩 키가 필요합니다")
    optional = (
        "GENERATION_TEMPERATURE",
        "GENERATION_MAX_OUTPUT_TOKENS",
        "GENERATION_TOKEN_LIMIT_FIELD",
    )
    unset = [k for k in optional if kind == "answer" and not values.get(k)]
    for key in unset:
        values.pop(key, None)
    save_env(target, values)
    remaining = dotenv_values(safe_path(target))
    for key in unset:
        if key in remaining:
            unset_key(str(safe_path(target)), key)
    if kind == "embedding" and not values.get("QA_ACTIVE_INDEX_KEY"):
        print("이 임베딩의 인덱스를 생성하고 지정해야 합니다.")


def register_judges(catalog, envfile):
    from mlflow.entities.gateway_endpoint import (
        GatewayEndpointModelConfig,
        GatewayModelLinkageType,
    )
    from mlflow.tracking._tracking_service.utils import _get_store

    env = dotenv_values(safe_path(envfile))
    data = read_catalog(catalog)
    if not env.get("MLFLOW_TRACKING_URI"):
        raise ValueError("MLFLOW_TRACKING_URI가 필요합니다")
    for key in (
        "MLFLOW_TRACKING_USERNAME",
        "MLFLOW_TRACKING_PASSWORD",
        "MLFLOW_TRACKING_TOKEN",
    ):
        if env.get(key):
            os.environ[key] = env[key]
    store = _get_store(env["MLFLOW_TRACKING_URI"])
    endpoints = {x.name: x for x in store.list_gateway_endpoints()}
    secrets = {x.secret_name: x for x in store.list_secret_infos()}
    definitions = {x.name: x for x in store.list_gateway_model_definitions()}
    names = [
        name.strip()
        for name in (env.get("EVALUATION_JUDGE_RATERS") or "").split(",")
        if name.strip()
    ]
    if not names or len(set(names)) != len(names):
        raise ValueError("먼저 중복 없는 Judge를 선택하세요")
    for role in names:
        validate_name(role)
        values = data["judge"].get(role)
        if not isinstance(values, dict):
            raise TypeError(f"{role}: Judge 프로필이 올바르지 않습니다")
        endpoint = f"elice-techblog-rag-judge-{role}"
        if endpoint in endpoints:
            print(f"{role}: 기존 endpoint 사용")
            continue
        if any(not values.get(k) for k in ("model", "base_url", "api_key")):
            raise ValueError(
                f"{role}: 새 endpoint 생성에 model, base_url, api_key가 필요합니다"
            )
        secret_name = endpoint + "-connection"
        definition_name = endpoint + "-model"
        # Never silently reuse a possibly different connection or model definition.
        if secret_name in secrets or definition_name in definitions:
            raise ValueError(
                f"{role}: 같은 이름의 연결 또는 모델이 이미 있습니다. MLflow에서 확인하세요"
            )
        secret = store.create_gateway_secret(
            secret_name,
            {"api_key": values["api_key"]},
            provider="openai",
            auth_config={"api_base": values["base_url"]},
        )
        definition = store.create_gateway_model_definition(
            definition_name, secret.secret_id, "openai", values["model"]
        )
        store.create_gateway_endpoint(
            endpoint,
            [
                GatewayEndpointModelConfig(
                    definition.model_definition_id, GatewayModelLinkageType.PRIMARY
                )
            ],
        )
        print(f"{role}: endpoint 등록 완료")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", default=".env.dev")
    parser.add_argument("--catalog", default=".env.models.dev.json")
    sub = parser.add_subparsers(dest="command", required=True)
    for command in ("capture", "select"):
        p = sub.add_parser(command)
        p.add_argument("kind", choices=FIELDS)
        p.add_argument("name")
    p = sub.add_parser("select-judges")
    p.add_argument("names", nargs="+")
    sub.add_parser("list")
    sub.add_parser("register-judges")
    args = parser.parse_args()
    try:
        if args.command == "capture":
            capture(args.kind, args.name, args.env_file, args.catalog)
            print(f"{args.kind}/{args.name} 등록 완료")
        elif args.command == "select":
            select(args.kind, args.name, args.catalog, args.env_file)
            print(f"{args.kind}/{args.name} 선택 완료. 서비스와 worker를 재생성하세요.")
        elif args.command == "select-judges":
            data = read_catalog(args.catalog)["judge"]
            if len(set(args.names)) != len(args.names) or any(
                validate_name(n) not in data for n in args.names
            ):
                raise ValueError("Judge 이름이 중복되었거나 등록되지 않았습니다")
            save_env(args.env_file, {"EVALUATION_JUDGE_RATERS": ",".join(args.names)})
            print("Judge 선택 완료. Gateway와 scorer를 등록하고 서비스를 재시작하세요.")
        elif args.command == "list":
            for kind, entries in read_catalog(args.catalog).items():
                for name in sorted(entries):
                    print(f"{kind}/{name}")
        else:
            register_judges(args.catalog, args.env_file)
    except (ValueError, TypeError, OSError) as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return 1
    except Exception:  # noqa: BLE001 - avoid exposing provider secrets in errors
        print(
            "오류: MLflow 연결 또는 등록에 실패했습니다. 서버 설정과 접근 권한을 확인하세요.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
