from contextlib import nullcontext
from threading import Barrier, Lock
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


def model_specs():
    return [
        dict(
            name=d,
            dimension=d,
            rater="chatgpt",
            version=2,
            model="gateway:/fake",
            instructions=instructions(d),
        )
        for d in DIMENSIONS["gold"]
    ]


def test_ragas_inputs_numeric_scores_na_and_deterministic_order(monkeypatch):
    from elice_knowledge.ragas_metrics import payload

    records, cases = examples()
    calls, artifacts = [], []
    broken = [False]
    monkeypatch.setattr("elice_knowledge.judges.configure", lambda *a: "exp")
    monkeypatch.setattr("mlflow.start_run", lambda **kw: nullcontext())

    async def metric(settings, model, dimension, data, rubric):
        calls.append((dimension, data))
        return (None if broken[0] else 0.75 if dimension == "completeness" else 1.0), "reason", None

    def evaluate(**kwargs):
        rows = []
        for item in kwargs["data"]:
            row = {"tags": item["tags"], "trace_id": "trace"}
            for judge in kwargs["scorers"]:
                result = judge.run(**{k: item[k] for k in ("inputs", "outputs", "expectations")})
                row[judge.name + "/value"] = result.value
                row[judge.name + "/rationale"] = result.rationale
            rows.append(row)
        return SimpleNamespace(tables={"eval_results": pd.DataFrame(rows[::-1])})

    monkeypatch.setattr("elice_knowledge.ragas_metrics.metric_score", metric)
    monkeypatch.setattr("mlflow.genai.evaluate", evaluate)
    monkeypatch.setattr(
        "mlflow.MlflowClient", lambda: SimpleNamespace(log_dict=lambda *a: artifacts.append(a))
    )
    _, rows = evaluate_answers(Settings(), "run", records, cases, model_specs())
    assert [r["record_id"] for r in rows] == ["r1", "r2"]
    assert rows[0]["judges"]["completeness"]["status"] == "fail"
    assert rows[0]["measurements"][1]["score"] == 0.75
    assert rows[1]["judges"]["correctness"]["status"] == "not_applicable"
    assert rows[1]["judges"]["groundedness"]["status"] == "not_applicable"
    for dimension, data in calls:
        if dimension in {"groundedness", "relevance"}:
            assert "reference" not in data and "reference" not in str(data)
            assert "unseen secret" not in str(data)
    assert payload(records[0], cases[0], "generation_context_recall")["retrieved_contexts"] == [
        "seen"
    ]
    assert payload(records[0], cases[0], "retrieved_context_recall")["retrieved_contexts"] == [
        "unseen secret"
    ]
    assert artifacts[0][2] == "judge-results.json"


@pytest.mark.parametrize(("record_workers", "scorer_workers"), [(1, 4), (2, 2)])
def test_real_mlflow_pool_runs_ragas_metrics_in_one_run(
    monkeypatch, tmp_path, record_workers, scorer_workers
):
    import mlflow

    previous_uri = mlflow.get_tracking_uri()
    monkeypatch.setenv("MLFLOW_TRACKING_URI", previous_uri)
    monkeypatch.setenv("MLFLOW_ALLOW_FILE_STORE", "true")
    mlflow.set_tracking_uri((tmp_path / "mlruns").as_uri())
    monkeypatch.setenv("MLFLOW_GENAI_EVAL_MAX_WORKERS", str(record_workers))
    monkeypatch.setenv("MLFLOW_GENAI_EVAL_MAX_SCORER_WORKERS", str(scorer_workers))
    monkeypatch.setenv("MLFLOW_GENAI_EVAL_ENABLE_SCORER_TRACING", "false")
    experiment = mlflow.create_experiment(
        "parallel-test", artifact_location=str(tmp_path / "artifacts")
    )
    monkeypatch.setattr("elice_knowledge.judges.configure", lambda *a: experiment)
    tracking = mlflow.MlflowClient()
    run = tracking.create_run(experiment)
    barrier, lock = Barrier(4), Lock()
    active, peak, calls = 0, 0, []

    async def metric(settings, model, dimension, data, rubric):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
            calls.append(dimension)
        try:
            barrier.wait(timeout=15)
            if dimension == "relevance":
                raise RuntimeError("fake scorer failure")
            return 1.0, "fake judgment", None
        finally:
            with lock:
                active -= 1

    monkeypatch.setattr("elice_knowledge.ragas_metrics.metric_score", metric)
    records, cases = examples()
    records = [records[0], {**records[0], "id": "r3", "inputs": {"question": "Q2"}}]
    cases = [cases[0], {**cases[0], "record_id": "r3"}]
    try:
        _, results = evaluate_answers(
            Settings(mlflow_tracking_uri=None),
            run.info.run_id,
            records,
            cases,
            model_specs(),
            raise_on_error=False,
        )
        assert peak == 4 and active == 0 and len(calls) == 16
        assert results[0]["judges"]["relevance"]["status"] == "error"
        assert results[0]["judges"]["correctness"]["status"] == "pass"
        assert len(results[0]["measurements"]) == 8
        assert len(tracking.search_runs([experiment])) == 1
        assert mlflow.active_run() is None
    finally:
        mlflow.set_tracking_uri(previous_uri)


def test_concurrency_limits_fail_before_model_work(monkeypatch):
    monkeypatch.setenv("MLFLOW_GENAI_EVAL_MAX_WORKERS", "0")
    monkeypatch.setattr("elice_knowledge.judges.configure", lambda *a: pytest.fail("too late"))
    records, cases = examples()
    with pytest.raises(ValueError, match="MLFLOW_GENAI_EVAL_MAX_WORKERS"):
        evaluate_answers(Settings(), "run", records, cases, model_specs())


def test_all_inputs_validated_before_paid_work(monkeypatch):
    records, cases = examples()
    del cases[0]["result"]["generation_sources"]
    monkeypatch.setattr("elice_knowledge.judges.configure", lambda *a: "exp")
    monkeypatch.setattr("elice_knowledge.ragas_metrics.scorer", lambda *a: pytest.fail("too soon"))
    with pytest.raises(ValueError, match="generation_sources_missing"):
        evaluate_answers(Settings(), "run", records, cases, model_specs())
