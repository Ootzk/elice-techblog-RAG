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


def test_dimension_versions_na_denominators_and_error_artifacts(monkeypatch):
    records, cases = examples()
    specs = [
        dict(name="qa-gold-" + dim + "-chatgpt", dimension=dim, rater="chatgpt", version=2)
        for dim in DIMENSIONS["gold"]
    ]
    calls, artifacts = [], []
    broken = [False]
    monkeypatch.setattr("elice_knowledge.judges.configure", lambda *a: "exp")
    pinned, forwarded = [], []

    def get_scorer(**kwargs):
        pinned.append(kwargs)

        def run(**row):
            forwarded.append((kwargs["name"], row))
            from mlflow.entities import Feedback

            return Feedback(value=None if broken[0] else "yes", rationale="reason")

        return SimpleNamespace(run=run)

    monkeypatch.setattr("mlflow.genai.scorers.get_scorer", get_scorer)
    monkeypatch.setattr("mlflow.start_run", lambda **kw: nullcontext())

    def evaluate(**kwargs):
        calls.append(kwargs)
        rows = []
        for item in kwargs["data"]:
            row = {"tags": item["tags"], "trace_id": "trace"}
            for judge in kwargs["scorers"]:
                feedback = judge.run(**{k: item[k] for k in ("inputs", "outputs", "expectations")})
                row[judge.name + "/value"] = feedback.value
                row[judge.name + "/rationale"] = feedback.rationale
            rows.append(row)
        return SimpleNamespace(tables={"eval_results": pd.DataFrame(rows[::-1])})

    monkeypatch.setattr("mlflow.genai.evaluate", evaluate)
    monkeypatch.setattr(
        "mlflow.MlflowClient",
        lambda: SimpleNamespace(
            log_dict=lambda *a: artifacts.append(a), log_metric=lambda *a: None
        ),
    )
    metrics, results = evaluate_answers(Settings(), "run", records, cases, specs)
    assert len(calls) == 1 and len(calls[0]["data"]) == 2
    assert len(calls[0]["scorers"]) == 4
    assert all(item["version"] == 2 for item in pinned)
    assert [item["record_id"] for item in results] == ["r1", "r2"]
    assert all("unseen secret" not in str(row) for _, row in forwarded)
    for name, row in forwarded:
        if "groundedness" in name or "relevance" in name:
            assert row["expectations"] is None
    assert metrics["qa-gold-groundedness-chatgpt_scored_cases"] == 2
    assert metrics["qa-gold-groundedness-chatgpt_not_applicable_cases"] == 0
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


@pytest.mark.parametrize(("record_workers", "scorer_workers"), [(1, 4), (2, 2)])
def test_native_sdk_parallel_dimensions_share_one_run_and_isolate_errors(
    monkeypatch, tmp_path, record_workers, scorer_workers
):
    """Exercise the real MLflow pools/aggregation with local metadata and fake judges."""
    import mlflow
    from mlflow.entities import Feedback

    previous_uri = mlflow.get_tracking_uri()
    monkeypatch.setenv("MLFLOW_TRACKING_URI", previous_uri)
    monkeypatch.setenv("MLFLOW_ALLOW_FILE_STORE", "true")
    # File store is test-only: production uses the configured remote SQL-backed server.
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
    by_question, peaks = {}, {}

    def get_scorer(**kwargs):
        def invoke(**row):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
                calls.append((kwargs, row))
                question = row["inputs"]["question"]
                by_question[question] = by_question.get(question, 0) + 1
                peaks[question] = max(peaks.get(question, 0), by_question[question])
            try:
                if question in {"Q", "Q2"}:
                    barrier.wait(timeout=15)
                    if question == "Q" and "relevance" in kwargs["name"]:
                        raise RuntimeError("fake scorer failure")
                return Feedback(value="yes", rationale="fake judgement")
            finally:
                with lock:
                    active -= 1
                    by_question[question] -= 1

        return SimpleNamespace(run=invoke)

    monkeypatch.setattr("mlflow.genai.scorers.get_scorer", get_scorer)
    records, cases = examples()
    records.insert(1, {**records[0], "id": "r3", "inputs": {"question": "Q2"}})
    cases.insert(1, {**cases[0], "record_id": "r3"})
    specs = [dict(name=d, dimension=d, rater="chatgpt", version=7) for d in DIMENSIONS["gold"]]
    try:
        _, results = evaluate_answers(
            Settings(mlflow_tracking_uri=None),
            run.info.run_id,
            records,
            cases,
            specs,
            raise_on_error=False,
        )
        assert peak == 4 and active == 0
        assert max(peaks.values()) <= scorer_workers
        assert len(calls) == 10  # N/A correctness/completeness never invoke a model.
        assert all(kw["version"] == 7 for kw, _ in calls)
        assert results[0]["judges"]["relevance"]["status"] == "error"
        assert results[0]["judges"]["correctness"]["status"] == "pass"
        assert results[2]["judges"]["correctness"]["status"] == "not_applicable"
        assert results[1]["judges"]["relevance"]["status"] == "pass"
        assert results[2]["judges"]["relevance"]["status"] == "pass"
        assert len(tracking.search_runs([experiment])) == 1
        assert mlflow.active_run() is None
    finally:
        mlflow.set_tracking_uri(previous_uri)


def test_concurrency_limits_fail_before_model_work(monkeypatch):
    monkeypatch.setenv("MLFLOW_GENAI_EVAL_MAX_WORKERS", "0")
    monkeypatch.setattr("elice_knowledge.judges.configure", lambda *a: pytest.fail("too late"))
    records, cases = examples()
    with pytest.raises(ValueError, match="MLFLOW_GENAI_EVAL_MAX_WORKERS"):
        evaluate_answers(Settings(), "run", records, cases, [])


def test_all_dimension_inputs_are_validated_before_loading_judges(monkeypatch):
    records, cases = examples()
    del cases[0]["result"]["generation_sources"]
    specs = [dict(dimension=d) for d in DIMENSIONS["gold"]]
    monkeypatch.setattr("elice_knowledge.judges.configure", lambda *a: "exp")
    monkeypatch.setattr(
        "mlflow.genai.scorers.get_scorer", lambda **kw: pytest.fail("judge loaded too soon")
    )
    with pytest.raises(ValueError, match="generation_sources_missing"):
        evaluate_answers(Settings(), "run", records, cases, specs)
