from types import SimpleNamespace

import pandas as pd
import pytest

from elice_knowledge.judges import KINDS, evaluate_answers, instructions
from elice_knowledge.settings import Settings


def test_online_rubric_has_no_reference_and_separate_offline_rubrics():
    assert "{{ expectations }}" not in instructions("online")
    for kind in ("gold", "iron"):
        assert "{{ expectations }}" in instructions(kind)
    assert len({instructions(kind) for kind in KINDS}) == 3


def test_judges_preserve_record_mapping_versions_and_detect_errors(monkeypatch):
    from contextlib import nullcontext

    calls, artifacts = [], []
    specs = [{"name": "qa-gold-chatgpt", "version": 2}]
    records = [{"id": "r1", "inputs": {"question": "Q"}, "expectations": {"answer": "reference"}}]
    cases = [
        {"record_id": "r1", "result": {"answer": "candidate"}, "error": None},
        {"record_id": "failed", "result": None, "error": "generation_failed"},
    ]
    rows = [
        {
            "tags": {"record_id": "r1"},
            "trace_id": "trace",
            "qa-gold-chatgpt/value": False,
            "qa-gold-chatgpt/rationale": "missing facts",
        }
    ]
    monkeypatch.setattr("elice_knowledge.judges.configure", lambda *a: "exp")
    monkeypatch.setattr("mlflow.genai.scorers.get_scorer", lambda **kw: calls.append(kw) or kw)
    monkeypatch.setattr("mlflow.start_run", lambda **kw: nullcontext())

    def evaluate(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            metrics={"qa-gold-chatgpt/mean": 0.0}, tables={"eval_results": pd.DataFrame(rows)}
        )

    monkeypatch.setattr("mlflow.genai.evaluate", evaluate)
    monkeypatch.setattr(
        "mlflow.MlflowClient", lambda: SimpleNamespace(log_dict=lambda *a: artifacts.append(a))
    )
    _, results = evaluate_answers(Settings(), "run", records, cases, specs)
    assert calls[0] == {"name": "qa-gold-chatgpt", "version": 2, "experiment_id": "exp"}
    assert len(calls[1]["data"]) == 1
    assert calls[1]["data"][0]["expectations"] == {"answer": "reference"}
    assert results[0]["record_id"] == "r1"
    assert results[0]["judges"]["qa-gold-chatgpt"]["value"] is False
    assert artifacts[0][2] == "judge-results.json"
    rows[0]["qa-gold-chatgpt/value"] = None
    with pytest.raises(RuntimeError, match="judge_assessment_failed"):
        evaluate_answers(Settings(), "run", records, cases, specs)
