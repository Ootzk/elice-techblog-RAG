import argparse
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

path = Path(__file__).parents[1] / "rag.py"
spec = importlib.util.spec_from_file_location("rag_operations", path)
rag = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rag)


def test_setup_checks_model_names_before_changes(monkeypatch):
    writes = []
    stub = SimpleNamespace(
        read_catalog=lambda path: {"answer": {}, "embedding": {}, "judge": {}},
        select=lambda *args: writes.append(args),
    )
    monkeypatch.setattr(rag, "models_module", lambda: stub)
    monkeypatch.setattr(rag, "environment", lambda path: {})
    args = argparse.Namespace(
        catalog="catalog",
        env_file="env",
        experiment="test",
        answer="missing",
        embedding="other",
        judge=["judge"],
    )
    with pytest.raises(ValueError):
        rag.setup(args)
    assert not writes


def test_missing_interactive_values_are_rejected_for_noninteractive_use(monkeypatch):
    stub = SimpleNamespace(
        read_catalog=lambda path: {"answer": {}, "embedding": {}, "judge": {}}
    )
    monkeypatch.setattr(rag, "models_module", lambda: stub)
    monkeypatch.setattr(rag, "environment", lambda path: {})
    monkeypatch.setattr(rag.sys.stdin, "isatty", lambda: False)
    args = argparse.Namespace(
        catalog="catalog",
        env_file="env",
        experiment=None,
        answer=None,
        embedding=None,
        judge=None,
    )
    with pytest.raises(ValueError, match="대화식"):
        rag.setup(args)


def test_evaluate_forwards_options_in_order(monkeypatch):
    captured = []
    monkeypatch.setattr(
        rag.sys,
        "argv",
        ["rag", "evaluate", "--dataset-id", "d-1", "--record-id", "r-1"],
    )
    monkeypatch.setattr(rag, "environment", lambda path: {})
    monkeypatch.setattr(rag, "run", lambda args, env: captured.append(args))
    assert rag.main() == 0
    assert captured == [
        ["tools/evaluation/evaluate.py", "--dataset-id", "d-1", "--record-id", "r-1"]
    ]


def test_service_commands_use_environment_and_keep_shared_services(monkeypatch):
    calls = []
    monkeypatch.setattr(rag, "environment", lambda _: {"APP_ENV": "dev"})
    monkeypatch.setattr(
        rag,
        "models_module",
        lambda: SimpleNamespace(safe_path=lambda p: Path("/work") / p),
    )
    monkeypatch.setattr(
        rag.subprocess, "run", lambda args, **kwargs: calls.append(args)
    )
    rag.manage_service(".env.dev", "init-db")
    rag.manage_service(".env.dev", "start")
    rag.manage_service(".env.dev", "stop")
    assert all("compose.dev.yaml" in call for call in calls)
    assert calls[0][-3:] == ["-d", "--wait", "postgres"]
    assert calls[2][-3:] == ["-m", "elice_knowledge", "init-db"]
    assert calls[-1][-3:] == ["stop", "knowledge", "worker"]
    assert all("down" not in call for call in calls)


@pytest.mark.parametrize(
    "command,script",
    [
        ("judge", "tools/evaluation/judges.py"),
        ("comparison-agreement", "tools/evaluation/compare.py"),
    ],
)
def test_additional_operations_forward_arguments(monkeypatch, command, script):
    captured = []
    monkeypatch.setattr(rag.sys, "argv", ["rag", command, "--help"])
    monkeypatch.setattr(rag, "environment", lambda path: {})
    monkeypatch.setattr(rag, "run", lambda args, env: captured.append(args))
    assert rag.main() == 0
    assert captured == [[script, "--help"]]


def test_agreement_collects_finished_gradings_for_requested_rater(monkeypatch):
    from elice_knowledge import tracking

    source = "a" * 32

    def item(run_id, rater, status, round_number):
        return SimpleNamespace(
            info=SimpleNamespace(run_id=run_id, status=status),
            data=SimpleNamespace(tags={"rater_id": rater, "round": str(round_number)}),
        )

    source_run = SimpleNamespace(
        info=SimpleNamespace(status="FINISHED", experiment_id="1"),
        data=SimpleNamespace(tags={"evaluation.kind": "answers"}),
    )
    runs = [
        item("b" * 32, "model:a", "FINISHED", 1),
        item("c" * 32, "model:b", "FINISHED", 1),
        item("d" * 32, "model:a", "FAILED", 2),
    ]
    tracker = SimpleNamespace(
        get_run=lambda _: source_run, search_runs=lambda *args, **kwargs: runs
    )
    monkeypatch.setattr(tracking, "client", lambda _: tracker)
    monkeypatch.setattr(rag, "environment", lambda _: {})
    calls = []
    monkeypatch.setattr(rag, "run", lambda args, env: calls.append(args))
    args = argparse.Namespace(
        env_file=".env.dev",
        answers_run_id=source,
        output_dir="out",
        rater=["model:a"],
        round=None,
        log=True,
    )
    rag.create_agreement(args)
    assert calls[0].count("--grading-run-id") == 1
    assert (
        "b" * 32 in calls[0] and "c" * 32 not in calls[0] and "d" * 32 not in calls[0]
    )
    assert "--log" in calls[0]


def test_init_environment_generates_private_consistent_dev_config(
    tmp_path, monkeypatch, capsys
):
    import subprocess

    from dotenv import dotenv_values

    real_root = rag.ROOT
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / ".gitignore").write_text(".env*\n")
    (tmp_path / ".env.dev.example").write_text(
        (real_root / ".env.dev.example").read_text()
    )
    models = rag.models_module()
    monkeypatch.setattr(rag, "ROOT", tmp_path)
    monkeypatch.setattr(models, "ROOT", tmp_path)
    monkeypatch.setattr(rag, "models_module", lambda: models)
    monkeypatch.chdir(tmp_path)
    rag.init_environment(".env.dev", "dev")
    env = dotenv_values(tmp_path / ".env.dev")
    assert env["POSTGRES_PASSWORD"] in env["DATABASE_URL"]
    assert env["POSTGRES_PASSWORD"] in env["RAG_HOST_DATABASE_URL"]
    assert len(env["ADMIN_ACCESS_CODE"]) >= 20 and len(env["AUTH_SESSION_SECRET"]) >= 32
    assert (tmp_path / ".env.dev").stat().st_mode & 0o077 == 0
    assert env["ADMIN_ACCESS_CODE"] not in capsys.readouterr().out
    with pytest.raises(ValueError):
        rag.init_environment(".env.dev", "dev")


def test_run_listing_filters_on_kind_and_hides_unrelated_metadata(monkeypatch, capsys):
    import json

    from elice_knowledge import tracking

    run = SimpleNamespace(
        info=SimpleNamespace(run_id="a" * 32, status="FINISHED", start_time=1000),
        data=SimpleNamespace(
            tags={
                "evaluation.kind": "answers",
                "mlflow.runName": "example",
                "private": "should-not-appear",
            }
        ),
    )
    calls = []

    def search(experiment_ids, **kwargs):
        calls.append((experiment_ids, kwargs))
        return [run]

    tracker = SimpleNamespace(
        get_experiment_by_name=lambda _: SimpleNamespace(experiment_id="1"),
        search_runs=search,
    )
    monkeypatch.setattr(tracking, "client", lambda _: tracker)
    monkeypatch.setattr(rag, "environment", lambda _: {})
    rag.list_runs(".env.dev", "answers", 5)
    output = json.loads(capsys.readouterr().out)
    assert output[0]["run_id"] == "a" * 32
    assert "private" not in output[0]
    assert "'answers'" in calls[0][1]["filter_string"]


@pytest.mark.parametrize(
    "arguments,expected",
    [
        (["init-env"], (".env.dev", "dev")),
        (["init-env", "--kind", "prod"], (".env", "prod")),
        (
            ["--env-file", ".env.custom", "init-env", "--kind", "prod"],
            (".env.custom", "prod"),
        ),
    ],
)
def test_init_env_uses_environment_specific_default(monkeypatch, arguments, expected):
    captured = []
    monkeypatch.setattr(rag.sys, "argv", ["rag", *arguments])
    monkeypatch.setattr(
        rag, "init_environment", lambda path, kind: captured.append((path, kind))
    )
    assert rag.main() == 0
    assert captured == [expected]


def test_production_service_initialization_uses_base_compose(monkeypatch):
    calls = []
    monkeypatch.setattr(rag, "environment", lambda _: {"APP_ENV": "prod"})
    monkeypatch.setattr(
        rag, "models_module", lambda: SimpleNamespace(safe_path=lambda p: Path(p))
    )
    monkeypatch.setattr(
        rag.subprocess, "run", lambda args, **kwargs: calls.append(args)
    )
    rag.manage_service(".env", "init-db")
    assert len(calls) == 1
    assert "compose.dev.yaml" not in calls[0]
    assert calls[0][-3:] == ["-m", "elice_knowledge", "init-db"]
