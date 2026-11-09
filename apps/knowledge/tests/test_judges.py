from contextlib import nullcontext
from types import SimpleNamespace

import pandas as pd
import pytest

from elice_knowledge.judges import DIMENSIONS, evaluate_answers, evaluation_row, instructions
from elice_knowledge.settings import Settings


def examples():
    records = [
        dict(
            id="r1",
            inputs={"question": "Q"},
            expectations={
                "answerable": True,
                "answer": "reference",
                "required_points": ["mandatory"],
                "unacceptable_claims": ["wrong"],
                "evidence": [{"quote": "truth"}],
            },
        ),
        dict(id="r2", inputs={"question": "unknown"}, expectations={"answerable": False}),
    ]
    cases = [
        dict(
            record_id="r1",
            error=None,
            result=dict(
                status="answered",
                answer="candidate",
                generation_sources=[{"text": "seen"}],
                retrieved_sources=[{"text": "unseen secret"}],
            ),
        ),
        dict(
            record_id="r2",
            error=None,
            result=dict(status="insufficient_evidence", answer="not known", generation_sources=[]),
        ),
    ]
    return records, cases


def test_dimension_inputs_do_not_leak_reference_or_unseen_candidates():
    records, cases = examples()
    for dimension in ("groundedness", "relevance"):
        row = evaluation_row(records[0], cases[0], dimension)
        assert "expectations" not in row
        assert "unseen secret" not in str(row)
    row = evaluation_row(records[0], cases[0], "correctness")
    assert "required_points" not in row["expectations"]
    row = evaluation_row(records[0], cases[0], "completeness")
    assert set(row["expectations"]) == {"answerable", "required_points"}
    assert "{{ expectations }}" not in instructions("groundedness")
    assert "{{ expectations }}" not in instructions("relevance")
    assert "{{ expectations }}" in instructions("correctness")
    del cases[0]["result"]["generation_sources"]
    with pytest.raises(ValueError, match="generation_sources_missing"):
        evaluation_row(records[0], cases[0], "groundedness")


def test_dimension_versions_na_denominators_and_error_artifacts(monkeypatch):
    records, cases = examples()
    specs = [
        dict(name="qa-gold-" + dim + "-chatgpt", dimension=dim, rater="chatgpt", version=2)
        for dim in DIMENSIONS["gold"]
    ]
    calls, artifacts = [], []
    broken = [False]
    monkeypatch.setattr("elice_knowledge.judges.configure", lambda *a: "exp")
    monkeypatch.setattr("mlflow.genai.scorers.get_scorer", lambda **kw: kw)
    monkeypatch.setattr("mlflow.start_run", lambda **kw: nullcontext())

    def evaluate(**kwargs):
        calls.append(kwargs)
        name = kwargs["scorers"][0]["name"]
        return SimpleNamespace(
            tables={
                "eval_results": pd.DataFrame(
                    [
                        {
                            "tags": item["tags"],
                            "trace_id": "trace",
                            name + "/value": None
                            if broken[0]
                            else "not_applicable"
                            if item["tags"]["record_id"] == "r2"
                            else "yes",
                            name + "/rationale": "reason",
                        }
                        for item in kwargs["data"]
                    ]
                )
            }
        )

    monkeypatch.setattr("mlflow.genai.evaluate", evaluate)
    monkeypatch.setattr(
        "mlflow.MlflowClient",
        lambda: SimpleNamespace(
            log_dict=lambda *a: artifacts.append(a), log_metric=lambda *a: None
        ),
    )
    metrics, results = evaluate_answers(Settings(), "run", records, cases, specs)
    assert len(calls) == 4 and len(calls[0]["data"]) == 1
    assert calls[0]["scorers"][0]["version"] == 2
    assert metrics["qa-gold-groundedness-chatgpt_scored_cases"] == 1
    assert metrics["qa-gold-groundedness-chatgpt_not_applicable_cases"] == 1
    assert results[1]["judges"]["qa-gold-correctness-chatgpt"]["value"] is None
    assert artifacts[0][2] == "judge-results.json"
    broken[0] = True
    with pytest.raises(RuntimeError, match="judge_assessment_failed"):
        evaluate_answers(Settings(), "run", records, cases, specs)
    assert any(
        cell["status"] == "error"
        for row in artifacts[-1][1]["cases"]
        for cell in row["judges"].values()
    )
