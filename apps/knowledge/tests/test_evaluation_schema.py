"""Shared ordinal grading manifests and isolated PostgreSQL/MLflow registry behavior."""

from copy import deepcopy
from threading import Barrier

import pytest
from test_chat_service import service as service  # noqa: F401
from test_human_review import RUN, finish_round
from test_human_review import review as review

from elice_knowledge.corpus import digest
from elice_knowledge.evaluation_schema import human_profile, model_profile, validate_grading
from elice_knowledge.grading import Grading
from elice_knowledge.judge_runs import evaluate_models
from elice_knowledge.judges import DIMENSIONS, inapplicable, offline_instructions
from elice_knowledge.ragas_metrics import DIAGNOSTICS, payload
from elice_knowledge.rubrics import load_rubric


def specs(bundle, raters=("chatgpt", "claude", "gemini"), rubric=None):
    rubric = rubric or load_rubric(bundle["kind"])
    return [
        dict(
            name=f"judge-{d}-{r}",
            dimension=d,
            rater=r,
            version=1,
            model="gateway:/" + r,
            instructions=offline_instructions(bundle["kind"], d, rubric),
        )
        for r in raters
        for d in DIMENSIONS[bundle["kind"]]
    ]


def fake_judgments(settings, run_id, records, cases, selected, **kwargs):
    expected = {r["id"]: r for r in records}
    rows = []
    for c in cases:
        if c["error"]:
            continue
        judges = {}
        for s in selected:
            na = inapplicable(expected[c["record_id"]], c, s["dimension"]) or (
                s["dimension"] == "groundedness" and c["result"]["status"] != "answered"
            )
            judges[s["name"]] = dict(
                dimension=s["dimension"],
                rater=s["rater"],
                version=s["version"],
                score=None if na else 1,
                status="not_applicable" if na else "scored",
                rationale="공통 rubric 판단",
            )
        measurements = []
        if kwargs.get("diagnostics"):
            for name in DIAGNOSTICS:
                na = payload(expected[c["record_id"]], c, name) is None
                measurements.append(
                    dict(
                        metric=name,
                        score=None if na else 0.5,
                        status="not_applicable" if na else "scored",
                        rationale="진단",
                    )
                )
        rows.append(dict(record_id=c["record_id"], judges=judges, measurements=measurements))
    return {}, rows


def artifact(review, run_id, path="grading-result.json"):
    return next(
        data for run, data, name in reversed(review.writes) if run == run_id and name == path
    )


def test_three_models_and_repeats_are_separate_shared_rubric_runs(review, monkeypatch):
    monkeypatch.setattr("elice_knowledge.judge_runs.evaluate_answers", fake_judgments)
    before = deepcopy(review.bundle)
    runs = evaluate_models(review.service, review.bundle, specs(review.bundle), repeat=2)
    assert len(runs) == 6 and len({r["run_id"] for r in runs}) == 6
    assert {r["state"] for r in runs} == {"FINISHED"}
    assert {r["round"] for r in runs} == {1, 2}
    for run in runs:
        result = artifact(review, run["run_id"])
        assert result["schema"] == "evaluation-v2" and result["contract"] == "rubric-v1"
        assert result["complete"] and len(result["ratings"]) == 8
        assert result["measurements"] == []
        assert "rubric.correctness.mean" in run["metrics"]
        assert not any("pass_rate" in k for k in run["metrics"])
        assert all(r["score"] is None or r["score"] == 1 for r in result["ratings"])
    assert review.bundle == before
    assert all(run != RUN for run, _, _ in review.writes)


def test_models_and_rounds_start_concurrently(review, monkeypatch):
    barrier = Barrier(4, timeout=15)

    def concurrent_judgments(*args, **kwargs):
        barrier.wait()
        return fake_judgments(*args, **kwargs)

    monkeypatch.setattr("elice_knowledge.judge_runs.evaluate_answers", concurrent_judgments)
    runs = evaluate_models(
        review.service, review.bundle, specs(review.bundle, raters=("chatgpt", "claude")), repeat=2
    )
    assert len(runs) == 4
    assert all(run["state"] == "FINISHED" for run in runs)
    assert {(run["rater_id"], run["round"]) for run in runs} == {
        ("model:chatgpt", 1),
        ("model:chatgpt", 2),
        ("model:claude", 1),
        ("model:claude", 2),
    }


def test_diagnostics_are_separate_from_ordinal_scores(review, monkeypatch):
    monkeypatch.setattr("elice_knowledge.judge_runs.evaluate_answers", fake_judgments)
    run = evaluate_models(
        review.service, review.bundle, specs(review.bundle), raters=["chatgpt"], diagnostics=True
    )[0]
    result = artifact(review, run["run_id"])
    assert len(result["measurements"]) == len(DIAGNOSTICS) * 2
    assert any(m["score"] == 0.5 for m in result["measurements"])
    assert {r["score"] for r in result["ratings"] if r["status"] == "scored"} == {1}
    assert "ragas.factual_precision.mean" in run["metrics"]
    assert "rubric.correctness.mean" in run["metrics"]


def test_human_and_model_publish_same_rubric_metric_names(review, monkeypatch):
    finish_round(review.admin, 1, 1)
    human = review.admin.get(review.path + "/result").json()
    monkeypatch.setattr("elice_knowledge.judge_runs.evaluate_answers", fake_judgments)
    run = evaluate_models(review.service, review.bundle, specs(review.bundle), raters=["claude"])[0]
    model = artifact(review, run["run_id"])
    assert human["rubric_digest"] == model["rubric_digest"]
    assert human["rubric"] == model["rubric"]
    human_keys = {
        k
        for rid, k, _, _ in review.tracking.metrics
        if rid
        == review.tracking.runs[
            review.admin.get("/api/reviews").json()["items"][0]["grading_run_id"]
        ].info.run_id
    }
    assert human_keys == set(run["metrics"])


def test_failures_are_error_not_low_scores_and_source_is_unchanged(review, monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("provider detail")

    monkeypatch.setattr("elice_knowledge.judge_runs.evaluate_answers", broken)
    run = evaluate_models(review.service, review.bundle, specs(review.bundle), raters=["gemini"])[0]
    assert run["state"] == "FAILED"
    result = artifact(review, run["run_id"])
    assert {r["status"] for r in result["ratings"]} == {"error"}
    assert {r["score"] for r in result["ratings"]} == {None}
    assert "provider detail" not in str(result)
    assert review.tracking.get_run(RUN).info.status == "FINISHED"


def test_profile_and_manifest_reject_mismatched_rubric_and_score(review):
    finish_round(review.admin, 1)
    result = review.admin.get(review.path + "/result").json()
    validate_grading(review.bundle, result)
    for bad in [True, 3, -1, 0.5, "2"]:
        changed = deepcopy(result)
        changed["ratings"][0]["score"] = bad
        with pytest.raises(ValueError):
            validate_grading(review.bundle, changed)
    changed = deepcopy(result)
    changed["rubric"]["dimensions"]["correctness"]["levels"]["2"] = "tampered"
    with pytest.raises(ValueError, match="rubric"):
        validate_grading(review.bundle, changed)
    changed = deepcopy(result)
    changed["ratings"][0]["answer_digest"] = "0" * 64
    with pytest.raises(ValueError, match="rating"):
        validate_grading(review.bundle, changed)
    changed = deepcopy(result)
    changed["ratings"].append(changed["ratings"][0])
    with pytest.raises(ValueError, match="rating"):
        validate_grading(review.bundle, changed)


def test_rubric_revision_and_model_options_change_profile_hash(review):
    baseline = load_rubric("gold")
    alternate = deepcopy(baseline)
    alternate["id"] = "gold-ordinal-experiment"
    alternate["dimensions"]["correctness"]["levels"]["1"] = "변경된 기준"
    a = model_profile(review.bundle, specs(review.bundle, ("chatgpt",)), rubric=baseline)
    b = model_profile(
        review.bundle, specs(review.bundle, ("chatgpt",), alternate), rubric=alternate
    )
    assert digest(a) != digest(b)
    assert (
        human_profile(review.bundle, baseline)["rubric_digest"]
        != human_profile(review.bundle, alternate)["rubric_digest"]
    )
    with pytest.raises(ValueError, match="profile"):
        model_profile(review.bundle, specs(review.bundle, ("chatgpt",)), rubric=alternate)
    grade = Grading(review.service).allocate(review.bundle, "model:chatgpt", a)
    assert Grading(review.service).get(grade["id"])["profile"] == a
