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
    ),
    "embedding": (
        "EMBEDDING_BACKEND",
        "EMBEDDING_BASE_URL",
        "EMBEDDING_MODEL",
        "EMBEDDING_MODEL_REVISION",
        "EMBEDDING_API_KEY",
        "EMBEDDING_DIMENSIONS",
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


def catalog_path(path):
    path = Path(path).resolve()
    try:
        path.relative_to(ROOT)
    except ValueError:
        raise ValueError("모델 파일은 저장소 안에 있어야 합니다") from None
    return path


def validate_catalog(data):
    if not isinstance(data, dict) or set(data) - {"answer", "embedding", "judge"}:
        raise ValueError("모델 파일의 항목이 올바르지 않습니다")
    for kind in ("answer", "embedding", "judge"):
        profiles = data.get(kind, {})
        if not isinstance(profiles, dict):
            raise TypeError("모델 목록은 객체여야 합니다")
        for name, profile in profiles.items():
            validate_name(name)
            if not isinstance(profile, dict):
                raise TypeError("모델 프로필은 객체여야 합니다")
            allowed = {
                "model",
                "backend",
                "revision",
                "dimensions",
                "base_url",
            }
            if set(profile) - allowed:
                raise ValueError(
                    "모델 JSON에 지원하지 않는 항목이나 인증정보가 있습니다"
                )
            from urllib.parse import urlsplit

            if "base_url" in profile:
                value = profile["base_url"]
                url = urlsplit(value) if isinstance(value, str) else None
                if (
                    not url
                    or url.scheme not in {"http", "https"}
                    or not url.hostname
                    or url.username
                    or url.password
                    or url.query
                    or url.fragment
                ):
                    raise ValueError(
                        "모델 주소는 인증정보나 쿼리 없는 HTTP(S) URL이어야 합니다"
                    )


def read_catalog(path):
    path = catalog_path(path)
    if not path.exists():
        return {"answer": {}, "embedding": {}, "judge": {}}
    data = json.loads(path.read_text())
    validate_catalog(data)
    if not isinstance(data, dict) or any(
        not isinstance(data.get(k, {}), dict) for k in ("answer", "embedding", "judge")
    ):
        raise ValueError("모델 카탈로그 JSON 형식이 올바르지 않습니다")
    return {k: data.get(k, {}) for k in ("answer", "embedding", "judge")}


def resolve_profile(data, kind, name, env):
    value = data[kind].get(name)
    if not isinstance(value, dict):
        raise TypeError("모델 프로필이 없습니다")
    profile = dict(value)
    if "api_key" in profile or "api_key_ref" in profile:
        raise ValueError("모델 JSON에 인증정보를 저장할 수 없습니다")
    profile["api_key"] = env.get("ELICE_API_KEY") or ""
    return profile


def write_catalog(path, data):
    import tempfile

    validate_catalog(data)
    path = catalog_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix=".env.model-catalog-")
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
    selected = env.get(
        "ANSWER_MODEL_PROFILE" if kind == "answer" else "EMBEDDING_MODEL_PROFILE"
    )
    if selected:
        source_catalog = read_catalog(env.get("MODEL_CATALOG_FILE") or "models.json")
        existing = source_catalog[kind].get(selected)
        if not isinstance(existing, dict):
            raise ValueError("선택된 모델 프로필이 없습니다")
        data = read_catalog(catalog)
        if name in data[kind]:
            raise ValueError("이미 등록된 이름입니다")
        data[kind][name] = dict(existing)
        write_catalog(catalog, data)
        return
    if any(
        not (env.get(k) or (k.endswith("_API_KEY") and env.get("ELICE_API_KEY")))
        for k in REQUIRED[kind]
    ):
        raise ValueError("필수 모델 설정이 빠졌습니다")
    if (
        kind == "embedding"
        and env.get("EMBEDDING_BACKEND") == "elice-serverless"
        and not (env.get("ELICE_API_KEY") or env.get("EMBEDDING_API_KEY"))
    ):
        raise ValueError("Serverless 임베딩 키가 없습니다")
    data = read_catalog(catalog)
    if name in data[kind]:
        raise ValueError("이미 등록된 이름입니다")
    prefix = "GENERATION_" if kind == "answer" else "EMBEDDING_"
    values = {
        key.removeprefix(prefix).lower(): env.get(key)
        for key in FIELDS[kind]
        if env.get(key) and not key.endswith("_API_KEY")
    }
    data[kind][name] = values
    write_catalog(catalog, data)


def select(kind, name, catalog, target):
    validate_name(name)
    profile = resolve_profile(
        read_catalog(catalog), kind, name, dotenv_values(safe_path(target))
    )
    prefix = "GENERATION_" if kind == "answer" else "EMBEDDING_"
    values = {
        key: str(profile.get(key.removeprefix(prefix).lower()) or "")
        for key in FIELDS[kind]
    }
    if any(not values.get(key) for key in REQUIRED[kind]):
        raise ValueError("모델 프로필의 필수 설정이 빠졌습니다")
    if (
        kind == "embedding"
        and values["EMBEDDING_BACKEND"] == "elice-serverless"
        and not values.get("EMBEDDING_API_KEY")
    ):
        raise ValueError("Serverless 임베딩 키가 필요합니다")
    selected = "ANSWER_MODEL_PROFILE" if kind == "answer" else "EMBEDDING_MODEL_PROFILE"
    catalog_file = str(catalog_path(catalog).relative_to(ROOT))
    save_env(target, {selected: name, "MODEL_CATALOG_FILE": catalog_file})
    remaining = dotenv_values(safe_path(target))
    for key in FIELDS[kind]:
        if key in remaining:
            unset_key(str(safe_path(target)), key)


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
    store = _get_store(
        env.get("RAG_HOST_MLFLOW_TRACKING_URI") or env["MLFLOW_TRACKING_URI"]
    )
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
        values = resolve_profile(data, "judge", role, env)
        endpoint = f"elice-techblog-rag-judge-{role}"
        if endpoint in endpoints:
            existing = endpoints[endpoint]
            mappings = existing.model_mappings
            if len(mappings) != 1 or mappings[0].model_definition is None:
                raise ValueError(f"{role}: 기존 endpoint 구성이 단일 모델이 아닙니다")
            definition = mappings[0].model_definition
            secret = next(
                (
                    item
                    for item in secrets.values()
                    if item.secret_id == definition.secret_id
                ),
                None,
            )
            if values.get("model") and values["model"] != definition.model_name:
                raise ValueError(f"{role}: 기존 endpoint의 모델이 프로필과 다릅니다")
            if values.get("base_url") and (
                secret is None
                or (secret.auth_config or {}).get("api_base", "").rstrip("/")
                != values["base_url"].rstrip("/")
            ):
                raise ValueError(f"{role}: 기존 endpoint의 주소가 프로필과 다릅니다")
            print(f"{role}: 기존 endpoint 사용 (키 일치 여부는 확인할 수 없음)")
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


def rotate_judge_key(catalog, envfile, names=None):
    """Explicitly replace selected judge connections after checking shared use."""
    from mlflow.tracking._tracking_service.utils import _get_store

    env = dotenv_values(safe_path(envfile))
    data = read_catalog(catalog)
    key = env.get("ELICE_API_KEY")
    if not key or not env.get("MLFLOW_TRACKING_URI"):
        raise ValueError("ELICE_API_KEY와 MLFLOW_TRACKING_URI가 필요합니다")
    selected = names or [
        n.strip()
        for n in (env.get("EVALUATION_JUDGE_RATERS") or "").split(",")
        if n.strip()
    ]
    if not selected or len(set(selected)) != len(selected):
        raise ValueError("중복 없는 Judge를 선택하세요")
    for name in selected:
        validate_name(name)
    for field in (
        "MLFLOW_TRACKING_USERNAME",
        "MLFLOW_TRACKING_PASSWORD",
        "MLFLOW_TRACKING_TOKEN",
    ):
        if env.get(field):
            os.environ[field] = env[field]
    store = _get_store(
        env.get("RAG_HOST_MLFLOW_TRACKING_URI") or env["MLFLOW_TRACKING_URI"]
    )
    endpoints = {x.name: x for x in store.list_gateway_endpoints()}
    secrets = {x.secret_id: x for x in store.list_secret_infos()}
    definitions = store.list_gateway_model_definitions()
    chosen_definitions = set()
    chosen_secrets = set()
    for name in selected:
        expected = resolve_profile(data, "judge", name, env)
        endpoint = endpoints.get(f"elice-techblog-rag-judge-{name}")
        if endpoint is None or len(endpoint.model_mappings) != 1:
            raise ValueError(f"{name}: 단일 모델 endpoint를 찾을 수 없습니다")
        definition = endpoint.model_mappings[0].model_definition
        if definition is None or definition.secret_id not in secrets:
            raise ValueError(f"{name}: 모델 연결을 찾을 수 없습니다")
        secret = secrets[definition.secret_id]
        if definition.model_name != expected.get("model") or (
            secret.auth_config or {}
        ).get("api_base", "").rstrip("/") != (expected.get("base_url") or "").rstrip(
            "/"
        ):
            raise ValueError(f"{name}: 모델 또는 주소가 카탈로그와 다릅니다")
        chosen_definitions.add(definition.model_definition_id)
        chosen_secrets.add(definition.secret_id)
    if any(
        d.secret_id in chosen_secrets
        and d.model_definition_id not in chosen_definitions
        for d in definitions
    ):
        raise ValueError(
            "선택하지 않은 모델이 같은 연결을 사용합니다. 함께 선택하거나 MLflow에서 확인하세요"
        )
    for secret_id in sorted(chosen_secrets):
        store.update_gateway_secret(secret_id, secret_value={"api_key": key})
    print(
        f"Judge {len(selected)}개의 연결 {len(chosen_secrets)}개에 환경 파일의 공용 키를 적용했습니다."
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", default=".env.dev")
    parser.add_argument("--catalog", default="models.json")
    sub = parser.add_subparsers(dest="command", required=True)
    for command in ("capture", "select"):
        p = sub.add_parser(command)
        p.add_argument("kind", choices=FIELDS)
        p.add_argument("name")
    p = sub.add_parser("select-judges")
    p.add_argument("names", nargs="+")
    sub.add_parser("list")
    sub.add_parser("register-judges")
    rotate = sub.add_parser("rotate-judge-key")
    rotate.add_argument(
        "--judge", action="append", help="갱신할 Judge 이름; 생략하면 선택된 전체"
    )
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
        elif args.command == "rotate-judge-key":
            rotate_judge_key(args.catalog, args.env_file, args.judge)
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
