"""Canonical manifests and real PostgreSQL grading registry; no paid model calls."""

import json
from copy import deepcopy
from pathlib import Path

import pytest
from test_chat_service import service as service  # noqa: F401
from test_evaluation_tools import load_tool
from test_human_review import RUN, finish_round  # noqa: F401
from test_human_review import review as review

from elice_knowledge.corpus import digest
from elice_knowledge.evaluation_schema import (
    grading_result,
    metrics,
    model_profile,
    validate_grading,
)
from elice_knowledge.grading import Grading
from elice_knowledge.judge_runs import evaluate_models
from elice_knowledge.judges import DIMENSIONS, inapplicable


def specs(bundle, raters=("chatgpt", "claude", "gemini")):
    return [
        {
            "name": f"qa-gold-{dimension}-{rater}",
            "dimension": dimension,
            "rater": rater,
            "version": 1,
            "model": "gateway:/" + rater,
            "instructions": bundle["rubrics"][dimension],
        }
        for rater in raters
        for dimension in DIMENSIONS[bundle["kind"]]
    ]


def fake_judgments(settings, run_id, records, cases, selected, **kwargs):
    expected = {r["id"]: r for r in records}
    return {}, [
        {
            "record_id": c["record_id"],
            "measurements": [
                dict(
                    metric=d,
                    score=None
                    if (
                        d in {"correctness", "completeness"}
                        and inapplicable(expected[c["record_id"]], c, d)
                    )
                    or (
                        d.endswith(("recall", "precision"))
                        and not expected[c["record_id"]]["expectations"].get("answerable")
                    )
                    else 1.0,
                    status="not_applicable"
                    if (
                        d in {"correctness", "completeness"}
                        and inapplicable(expected[c["record_id"]], c, d)
                    )
                    or (
                        d.endswith(("recall", "precision"))
                        and not expected[c["record_id"]]["expectations"].get("answerable")
                    )
                    else "scored",
                    rationale="fake",
                )
                for d in [
                    *[s["dimension"] for s in selected],
                    "retrieved_context_recall",
                    "retrieved_context_precision",
                    "generation_context_recall",
                    "generation_context_precision",
                ]
            ],
            "judges": {
                s["name"]: {
                    "dimension": s["dimension"],
                    "rater": s["rater"],
                    "version": s["version"],
                    "status": "not_applicable"
                    if inapplicable(expected[c["record_id"]], c, s["dimension"])
                    else "pass",
                    "rationale": "근거 확인",
                }
                for s in selected
            },
        }
        for c in cases
        if not c["error"]
    ]


def test_three_models_two_trials_create_six_independent_child_runs(review, monkeypatch):
    seen = []

    def judge(*args, **kwargs):
        seen.append({s["rater"] for s in args[4]})
        return fake_judgments(*args, **kwargs)

    monkeypatch.setattr("elice_knowledge.judge_runs.evaluate_answers", judge)
    results = evaluate_models(review.service, review.bundle, specs(review.bundle), repeat=2)
    assert len(results) == 6 and len({r["run_id"] for r in results}) == 6
    assert all(len(raters) == 1 for raters in seen)
    for rater in ("chatgpt", "claude", "gemini"):
        assert {r["round"] for r in results if r["rater_id"] == "model:" + rater} == {1, 2}
    for result in results:
        run = review.tracking.runs[result["run_id"]]
        assert run.data.tags["mlflow.parentRunId"] == RUN
        assert run.data.tags["evaluation.kind"] == "grading"
        assert run.info.status == "FINISHED"
        artifacts = [
            data
            for target, data, path in review.writes
            if target == result["run_id"] and path == "grading-result.json"
        ]
        assert len(artifacts) == 1 and artifacts[0]["rater_id"] == result["rater_id"]
        assert len(artifacts[0]["labels"]) == 8
        assert all(r["rater"] == result["rater_id"] for r in artifacts[0]["labels"])
        validate_grading(review.bundle, artifacts[0])


def test_human_and_model_share_metrics_and_completed_manifest(review, monkeypatch):
    finish_round(review.admin, 1)
    synced = review.admin.post(f"/api/reviews/{RUN}/rounds/1/sync").json()
    human = review.admin.get(f"/api/reviews/{RUN}/rounds/1/result").json()
    assert human["complete"] and human["rater_type"] == "human"
    monkeypatch.setattr("elice_knowledge.judge_runs.evaluate_answers", fake_judgments)
    model = evaluate_models(
        review.service, review.bundle, specs(review.bundle), raters=["chatgpt"]
    )[0]
    artifact = next(
        data
        for target, data, path in review.writes
        if target == model["run_id"] and path == "grading-result.json"
    )
    assert metrics(review.bundle, human) == metrics(review.bundle, artifact)
    human_metrics = {
        key for run, key, _, _ in review.tracking.metrics if run == synced["grading_run_id"]
    }
    model_metrics = {key for run, key, _, _ in review.tracking.metrics if run == model["run_id"]}
    assert human_metrics == {k for k in model_metrics if not k.startswith("ragas.")}
    assert "quality.correctness.pass_rate" in human_metrics
    assert "quality.correctness.not_applicable_cases" in human_metrics


def test_model_errors_are_explicit_and_do_not_overwrite_source(review, monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("provider secret")

    monkeypatch.setattr("elice_knowledge.judge_runs.evaluate_answers", broken)
    results = evaluate_models(
        review.service, review.bundle, specs(review.bundle), raters=["claude"]
    )
    assert results[0]["state"] == "FAILED"
    artifact = next(
        data
        for run, data, path in review.writes
        if run == results[0]["run_id"] and path == "grading-result.json"
    )
    assert {r["label"] for r in artifact["labels"]} == {"error"}
    assert "secret" not in str(artifact)
    assert review.tracking.get_run(RUN).info.status == "FINISHED"
    assert all(run != RUN for run, _, _ in review.writes)
    values = metrics(review.bundle, artifact)
    assert "quality.correctness.pass_rate" not in values
    assert values["quality.correctness.error_cases"] == 2


def test_profile_and_manifest_binding_rejects_incompatible_data(review):
    bundle = review.bundle
    bad = specs(bundle, ["chatgpt"])
    bad[0]["instructions"] = "different criterion"
    with pytest.raises(ValueError, match="profile_mismatch"):
        model_profile(bundle, bad)
    store = Grading(review.service)
    row = store.allocate(bundle, "model:chatgpt", model_profile(bundle, specs(bundle, ["chatgpt"])))
    manifest = grading_result(bundle, row, [])
    assert not manifest["complete"]
    assert metrics(bundle, manifest)["quality.groundedness.pending_cases"] == 2
    for key, value in (
        ("schema", "old"),
        ("source_run_id", "other"),
        ("source_bundle_digest", "wrong"),
    ):
        wrong = {**manifest, key: value}
        with pytest.raises(ValueError, match="source_mismatch"):
            validate_grading(bundle, wrong)
    with pytest.raises(ValueError, match="incomplete"):
        validate_grading(bundle, {**manifest, "complete": True})


def test_timeout_after_remote_creation_is_reattached_without_duplicate_run(review):
    tracker = review.tracking
    original = tracker.create_run

    def ambiguous(*args, **kwargs):
        original(*args, **kwargs)
        raise TimeoutError("created remotely but response lost")

    finish_round(review.admin, 1)
    tracker.create_run = ambiguous
    pending = review.admin.post("/api/reviews", json={"run_id": RUN}).json()
    assert pending["round"] == 2 and pending["tracking_pending"]
    count = len(tracker.runs)
    tracker.create_run = original
    resumed = review.admin.post("/api/reviews", json={"run_id": RUN}).json()
    assert resumed["round"] == 2 and resumed["grading_run_id"] and not resumed["tracking_pending"]
    assert len(tracker.runs) == count


def test_same_subject_changed_profile_is_a_revision_not_a_duplicate_round(review):
    from elice_knowledge.agreement import analyze

    finish_round(review.admin, 1)
    first = review.admin.get(f"/api/reviews/{RUN}/rounds/1/result").json()
    second = deepcopy(first)
    second["rater_profile_digest"] = digest({"changed": "rubric"})
    for label in second["labels"]:
        label["protocol"] = second["rater_profile_digest"]
    validate_grading(review.bundle, second)
    comparisons = analyze(review.bundle, first["labels"] + second["labels"])["comparisons"]
    assert comparisons and {r["comparison"] for r in comparisons} == {"judge_revision"}


def test_manifest_rejects_duplicate_or_misbound_labels(review):
    finish_round(review.admin, 1)
    manifest = review.admin.get(f"/api/reviews/{RUN}/rounds/1/result").json()
    for key, value in (
        ("answer_digest", "wrong"),
        ("rater", "human:other"),
        ("round", "2"),
        ("protocol", "wrong"),
    ):
        wrong = deepcopy(manifest)
        wrong["labels"][0][key] = value
        with pytest.raises(ValueError, match="label_mismatch"):
            validate_grading(review.bundle, wrong)
    wrong = deepcopy(manifest)
    wrong["labels"].append(wrong["labels"][0])
    with pytest.raises(ValueError, match="label_mismatch"):
        validate_grading(review.bundle, wrong)


def test_cli_preview_can_plan_ten_repeats_of_one_model_without_paid_work(
    review, monkeypatch, tmp_path, capsys
):
    tool = load_tool("review")
    monkeypatch.setattr(tool, "configure", lambda _: "exp")
    monkeypatch.setattr(tool, "Settings", lambda: review.service.settings)
    monkeypatch.setattr(tool, "client", lambda _: review.tracking)
    monkeypatch.setattr(tool, "frozen_judges", lambda *a: specs(review.bundle))

    def forbidden(*args, **kwargs):
        raise AssertionError("Preview must not grade")

    monkeypatch.setattr(tool, "evaluate_models", forbidden)
    argv = [
        "review",
        "judge",
        "--source-run-id",
        RUN,
        "--output-dir",
        str(tmp_path),
        "--repeat",
        "10",
        "--model-rater",
        "claude",
    ]
    monkeypatch.setattr("sys.argv", argv)
    tool.main()
    plan = json.loads(capsys.readouterr().out)
    assert plan["planned_grading_runs"] == 10
    assert {s["rater"] for s in plan["judges"]} == {"claude"}


def test_cli_logs_analysis_without_inventing_undefined_kappa(review, monkeypatch, tmp_path):
    tool = load_tool("review")
    finish_round(review.admin, 1)
    one = review.admin.post(f"/api/reviews/{RUN}/rounds/1/sync").json()["grading_run_id"]
    review.admin.post("/api/reviews", json={"run_id": RUN})
    finish_round(review.admin, 2)
    two = review.admin.post(f"/api/reviews/{RUN}/rounds/2/sync").json()["grading_run_id"]

    def download(run_id, name, directory):
        data = (
            review.bundle
            if name == "evaluation-bundle.json"
            else next(
                data
                for target, data, path in reversed(review.writes)
                if target == run_id and path == name
            )
        )
        path = Path(directory) / name
        path.write_text(json.dumps(data))
        return str(path)

    monkeypatch.setattr(tool, "configure", lambda _: "exp")
    monkeypatch.setattr(tool, "Settings", lambda: review.service.settings)
    monkeypatch.setattr(tool, "client", lambda _: review.tracking)
    monkeypatch.setattr(review.tracking, "download_artifacts", download)
    monkeypatch.setattr(
        "sys.argv",
        [
            "review",
            "analyze",
            "--source-run-id",
            RUN,
            "--grading-run-id",
            one,
            "--grading-run-id",
            two,
            "--output-dir",
            str(tmp_path),
            "--log",
        ],
    )
    tool.main()
    report = json.loads((tmp_path / "agreement.json").read_text())
    assert report["grading_run_ids"] == [one, two]
    assert all(row["cohen_kappa"] is None for row in report["comparisons"])
    analysis = next(
        r for r in review.tracking.runs.values() if r.data.tags["evaluation.kind"] == "analysis"
    )
    assert analysis.data.tags["source_run_id"] == RUN and analysis.info.status == "FINISHED"
    logged = [name for run, name, _, _ in review.tracking.metrics if run == analysis.info.run_id]
    assert logged and not any(name.endswith("cohen_kappa") for name in logged)


def test_ragas_fractional_scores_and_context_errors_survive_publication(review, monkeypatch):
    def grade(*args, **kwargs):
        metrics, rows = fake_judgments(*args, **kwargs)
        rows[0]["judges"][next(k for k in rows[0]["judges"] if "correctness" in k)]["status"] = (
            "fail"
        )
        for m in rows[0]["measurements"]:
            if m["metric"] == "correctness":
                m["score"] = 0.5
            if m["metric"] == "retrieved_context_recall":
                m.update(score=None, status="error")
        return metrics, rows

    monkeypatch.setattr("elice_knowledge.judge_runs.evaluate_answers", grade)
    result = evaluate_models(review.service, review.bundle, specs(review.bundle, ["chatgpt"]))[0]
    assert result["state"] == "FAILED"
    manifest = next(
        data
        for target, data, path in review.writes
        if target == result["run_id"] and path == "grading-result.json"
    )
    assert manifest["measurement_contract"] == "ragas-v1"
    validate_grading(review.bundle, manifest)
    assert result["metrics"]["ragas.correctness.mean"] == 0.5  # Other case is N/A.
    assert result["metrics"]["ragas.retrieved_context_recall.error_cases"] == 1
    for change in ("out_of_range", "missing", "label"):
        bad = deepcopy(manifest)
        if change == "out_of_range":
            bad["measurements"][0]["score"] = float("nan")
        elif change == "missing":
            bad["measurements"].pop()
        else:
            bad["labels"][0]["label"] = "pass" if bad["labels"][0]["label"] == "fail" else "fail"
        with pytest.raises(ValueError):
            validate_grading(review.bundle, bad)
