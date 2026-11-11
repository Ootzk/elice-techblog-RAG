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
