"""Frozen A/B comparisons with no model calls and no remote writes."""

import json
import math
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_evaluation_tools import load_tool
from test_reliability import source_with_four_cases

from elice_knowledge.agreement import build_agreement
from elice_knowledge.comparison import compare, comparison_metrics, scalar, validate_snapshot
from elice_knowledge.corpus import digest
from elice_knowledge.evaluation_bundle import answer_digest
from elice_knowledge.evaluation_schema import SCHEMA
from elice_knowledge.judges import DIMENSIONS
from elice_knowledge.ragas_metrics import DIAGNOSTICS
from elice_knowledge.rubrics import load_rubric


def refresh(snapshot):
    report = build_agreement(
        snapshot["bundle"],
        [r for g in snapshot["gradings"] for r in g["ratings"]],
        snapshot["gradings"][0]["rubric"],
    )
    snapshot["agreement"] = {**report, "schema": SCHEMA, "grading_run_ids": []}
    return snapshot


def test_agreement_comparison_excludes_random_baseline():
    snapshot = frozen(
        "A", patterns={"human:alice": [2, 2, 0, 2], "baseline:uniform-random": [0, 1, 2, 0]}
    )
    assert set(validate_snapshot(snapshot)) == {"human:alice"}


def frozen(name, *, patterns=None, source=None, failed=(), ids=None):
    bundle = deepcopy(source) if source is not None else source_with_four_cases()
    if source is None:
        bundle["source_run_id"] = "answers-" + name
        bundle["qa_profile"] = {"qa_agent_max_tool_calls": 4, "top_k": 5}
        if ids is not None:
            bundle["records"] = [r for r in bundle["records"] if r["id"] in ids]
            bundle["cases"] = [c for c in bundle["cases"] if c["record_id"] in ids]
        for c in bundle["cases"]:
            c["duration_ms"] = 100
            c["result"].update(
                llm_call_count=2,
                tool_call_count=1,
                usage={"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
            )
            if c["record_id"] in failed:
                c.update(error="generation_failed", result=None)
        bundle["digest"] = digest({k: v for k, v in bundle.items() if k != "digest"})
    patterns = patterns or {
        "human:alice": [2, 2, 0, 2],
        "model:chatgpt": [2, 0, 0, 2],
        "model:claude": [0, 0, 0, 2],
    }
    records = {r["id"]: r for r in bundle["records"]}
    grades = []
    for rater, values in patterns.items():
        kind = rater.split(":")[0]
        profile = digest(rater)
        labels, measurements = [], []
        for case in bundle["cases"]:
            if case["error"]:
                continue
            key = case["record_id"]
            for dimension in DIMENSIONS["gold"]:
                labels.append(
                    dict(
                        record_id=key,
                        answer_digest=answer_digest(records[key], case),
                        dimension=dimension,
                        rater=rater,
                        round="1",
                        protocol=profile,
                        score=values[int(key)] if type(values[int(key)]) is int else None,
                        status="scored" if type(values[int(key)]) is int else values[int(key)],
                        rationale="fixture",
                    )
                )
            if kind == "model":
                for dimension in DIAGNOSTICS:
                    measurements.append(
                        dict(
                            record_id=key,
                            metric=dimension,
                            score=1.0,
                            status="scored",
                            rationale="diagnostic fixture",
                        )
                    )
        grade = dict(
            schema=SCHEMA,
            source_run_id=bundle["source_run_id"],
            source_bundle_digest=bundle["digest"],
            rater_type=kind,
            rater_id=rater,
            rater_profile_digest=profile,
            round=1,
            complete=True,
            ratings=labels,
            contract="rubric-v1",
            rubric=load_rubric("gold"),
            rubric_digest=digest(load_rubric("gold")),
            diagnostics=kind == "model",
            measurement_contract="ragas-diagnostics-v1",
            measurements=measurements,
        )
        grades.append(grade)
    return refresh(
        dict(
            agreement_run_id="agreement-" + name, experiment_id="9", bundle=bundle, gradings=grades
        )
    )


def run(left, right, **kwargs):
    return compare(
        left,
        right,
        purpose=kwargs.pop("purpose", "system"),
        hypothesis="Compare tool budgets",
        resamples=100,
        seed=7,
        **kwargs,
    )


def test_paired_quality_resources_intervals_and_content_digest():
    a = frozen("a")
    patterns = {
        "human:alice": [2, 2, 0, 2],
        "model:chatgpt": [2, 2, 0, 2],
        "model:claude": [0, 0, 0, 2],
    }
    source = deepcopy(a["bundle"])
    source["source_run_id"] = "answers-b"
    source["qa_profile"]["qa_agent_max_tool_calls"] = 6
    for c in source["cases"]:
        c["duration_ms"] = 80
        c["result"]["usage"]["prompt_tokens"] = 80
    source["digest"] = digest({k: v for k, v in source.items() if k != "digest"})
    b = frozen("b", source=source, patterns=patterns)
    report = run(a, b)
    row = next(
        r
        for r in report["quality"]
        if r["rater"] == "model:chatgpt" and r["metric"] == "rubric.correctness.mean"
    )
    assert row["delta_mean"] == 0.5 and row["paired_cases"] == 4
    assert row["counts"] == {"improved": 1, "regressed": 0, "tie": 3}
    assert row["confidence_interval"] is not None
    assert (
        next(r for r in report["resources"] if r["metric"] == "qa_duration_ms")["delta_mean"] == -20
    )
    assert (
        next(r for r in report["resources"] if r["metric"] == "cached_input_tokens")["paired_cases"]
        == 0
    )
    assert report["configuration_changes"] == [
        dict(path="qa_profile.qa_agent_max_tool_calls", baseline=4, candidate=6)
    ]
    assert report["digest"] == digest({k: v for k, v in report.items() if k != "digest"})
    assert run(a, b) == report
    assert all(math.isfinite(v) for v in comparison_metrics(report).values())
    assert any("ragas.factual_precision.mean.delta_mean" in k for k in comparison_metrics(report))
    json.dumps(report, allow_nan=False)


def test_missing_data_and_generation_failures_do_not_disappear_into_means():
    a, b = frozen("a", failed=["0"]), frozen("b", ids=["0", "1", "2"])
    report = run(a, b)
    assert report["coverage"]["baseline_only"] == ["3"]
    assert report["coverage"]["shared_record_ids"] == ["0", "1", "2"]
    failure = next(r for r in report["resources"] if r["metric"] == "failure_rate")
    assert failure["delta_mean"] == pytest.approx(-1 / 3)
    quality = report["quality"][0]
    assert quality["paired_cases"] == 2 and quality["excluded_cases"] == 1
    assert quality["cases"][0]["baseline_reason"] == "generation_error"
    assert len(report["reliability"]["pairs"][0]["paired_record_ids"]) == 2


@pytest.mark.parametrize(
    "change,match",
    [
        ("dataset", "dataset_or_rubric"),
        ("panel", "rater_panel"),
        ("profile", "rater_profile"),
        ("question", "question_or_expectation"),
        ("duplicate", "one_grading"),
        ("tampered", "inputs_or_protocol"),
    ],
)
def test_incompatible_or_stale_inputs_are_rejected(change, match):
    a, b = frozen("a"), frozen("b")
    if change in {"dataset", "question"}:
        source = deepcopy(b["bundle"])
        if change == "dataset":
            source["dataset_digest"] = "another-version"
        else:
            source["records"][0]["inputs"]["question"] = "changed"
        source["digest"] = digest({k: v for k, v in source.items() if k != "digest"})
        b = frozen("b", source=source)
    elif change == "panel":
        b["gradings"].pop()
        refresh(b)
    elif change == "profile":
        g = b["gradings"][1]
        g["rater_profile_digest"] = digest("changed")
        for row in g["ratings"]:
            row["protocol"] = g["rater_profile_digest"]
        refresh(b)
    elif change == "duplicate":
        b["gradings"].append(deepcopy(b["gradings"][0]))
    else:
        b["agreement"]["comparisons"][0]["cohen_kappa"] = 999
    with pytest.raises(ValueError, match=match):
        run(a, b)


def test_judge_comparison_freezes_answers_and_human_votes_and_labels_score_shifts():
    a = frozen("a")
    b = deepcopy(a)
    b["agreement_run_id"] = "agreement-b"
    g = b["gradings"][1]
    g["rater_profile_digest"] = digest("judge-v2")
    for row in g["ratings"]:
        row["protocol"] = g["rater_profile_digest"]
    refresh(b)
    report = run(a, b, purpose="judge")
    assert all(r["interpretation"] == "score_shift" for r in report["quality"])
    with pytest.raises(ValueError, match="rater_profile"):
        run(a, b)
    with pytest.raises(ValueError, match="same_frozen_answers"):
        run(a, frozen("other"), purpose="judge")
    human = b["gradings"][0]
    human["ratings"][0]["score"] = 0
    refresh(b)
    with pytest.raises(ValueError, match="human_reference_changed"):
        run(a, b, purpose="judge")


def test_bootstrap_constant_effect_small_sample_and_nonfinite_measurement():
    row = scalar("x", {"a": 0, "b": 0}, {"a": 1, "b": 1}, ["a", "b"], resamples=100)
    assert row["confidence_interval"] == [1, 1]
    row = scalar("x", {"a": 1}, {"a": 2}, ["a"])
    assert (
        row["confidence_interval"] is None
        and row["interval_unavailable_reason"] == "insufficient_paired_cases"
    )
    row = scalar("x", {"a": float("nan")}, {"a": 2}, ["a"])
    json.dumps(row, allow_nan=False)
    assert row["paired_cases"] == 0


def test_cli_local_preview_and_independent_comparison_run(tmp_path, monkeypatch, capsys):
    tool = load_tool("compare")
    a, b = frozen("a"), frozen("b")
    by_agreement = {s["agreement_run_id"]: s for s in (a, b)}
    writes, metrics, created, states = [], [], [], []

    def get_run(key):
        if key in by_agreement:
            source = by_agreement[key]["bundle"]
            tags = {
                "evaluation.kind": "agreement",
                "evaluation.schema": SCHEMA,
                "source_run_id": source["source_run_id"],
                "source_bundle_digest": source["digest"],
            }
        else:
            tags = {"evaluation.kind": "answers", "evaluation.schema": "evaluation-v1"}
        return SimpleNamespace(
            info=SimpleNamespace(status="FINISHED", experiment_id="9"),
            data=SimpleNamespace(tags=tags),
        )

    def download(key, name, directory):
        if name == "evaluation-bundle.json":
            data = next(s["bundle"] for s in (a, b) if s["bundle"]["source_run_id"] == key)
        elif name == "agreement.json":
            data = by_agreement[key]["agreement"]
        else:
            data = {"results": by_agreement[key]["gradings"]}
        path = Path(directory) / name
        path.write_text(json.dumps(data))
        return str(path)

    def create(exp, tags):
        created.append((exp, tags))
        return SimpleNamespace(info=SimpleNamespace(run_id="comparison-run"))

    tracking = SimpleNamespace(
        get_run=get_run,
        download_artifacts=download,
        create_run=create,
        log_dict=lambda *x: writes.append(x),
        log_metric=lambda *x: metrics.append(x),
        log_param=lambda *x: None,
        set_terminated=lambda *x: states.append(x),
    )
    monkeypatch.setattr(tool, "client", lambda *x: tracking)
    argv = [
        "compare",
        "--baseline-agreement-run-id",
        a["agreement_run_id"],
        "--candidate-agreement-run-id",
        b["agreement_run_id"],
        "--purpose",
        "system",
        "--hypothesis",
        "same conditions, repeated generation",
        "--bootstrap-resamples",
        "100",
    ]
    monkeypatch.setattr("sys.argv", argv + ["--output-dir", str(tmp_path / "local")])
    tool.main()
    assert not created and not writes
    monkeypatch.setattr("sys.argv", argv + ["--output-dir", str(tmp_path / "logged"), "--log"])
    tool.main()
    assert created[0][1]["evaluation.kind"] == "comparison"
    assert "mlflow.parentRunId" not in created[0][1]
    assert all(x[0] == "comparison-run" for x in writes + metrics)
    assert states == [("comparison-run", "FINISHED")]
    assert (tmp_path / "logged" / "comparison-run.json").exists()
    with pytest.raises(SystemExit):
        tool.main()
    assert not list((tmp_path / "logged").glob(".mlflow-*"))


def test_reliability_uses_common_observers_and_keeps_original_exclusions_visible():
    a, b = frozen("a"), frozen("b")
    grade = b["gradings"][1]
    for row in grade["ratings"]:
        if row["record_id"] == "0" and row["dimension"] == "groundedness":
            row.update(score=None, status="not_applicable")
    for row in grade["measurements"]:
        if row["record_id"] == "0" and row["metric"] == "groundedness":
            row.update(status="not_applicable", score=None)
    refresh(b)
    report = run(a, b)
    pair = next(
        p
        for p in report["reliability"]["pairs"]
        if p["dimension"] == "groundedness" and p["raters"] == ["human:alice", "model:chatgpt"]
    )
    assert pair["paired_record_ids"] == ["1", "2", "3"]
    assert "applicability_agreement" not in pair["baseline"]
    support = next(
        p
        for p in report["reliability"]["support"]
        if p["dimension"] == "groundedness"
        and p["rater"] == "model:chatgpt"
        and p["record_id"] == "0"
    )
    assert (
        support["candidate"] == "not_applicable"
        and support["baseline"] == 2
        and not support["included"]
    )
    panel = next(p for p in report["reliability"]["panels"] if p["dimension"] == "groundedness")
    assert panel["baseline"]["complete_cases"] == panel["candidate"]["complete_cases"] == 3
    assert panel["baseline"]["pairable_cases"] == panel["candidate"]["pairable_cases"] == 4


def test_no_overlap_and_same_answers_in_system_mode_are_rejected():
    with pytest.raises(ValueError, match="no_shared_records"):
        run(frozen("a", ids=["0"]), frozen("b", ids=["1"]))
    a = frozen("a")
    b = deepcopy(a)
    b["agreement_run_id"] = "another-agreement"
    with pytest.raises(ValueError, match="distinct_answers"):
        run(a, b)


def test_comparison_logging_failure_marks_only_its_own_run_failed(tmp_path, monkeypatch):
    tool = load_tool("compare")
    snapshots = {"a": frozen("a"), "b": frozen("b")}
    states = []

    def fail(*args):
        raise RuntimeError("artifact storage unavailable")

    tracking = SimpleNamespace(
        create_run=lambda *a, **kw: SimpleNamespace(
            info=SimpleNamespace(run_id="comparison-failed")
        ),
        log_dict=fail,
        set_terminated=lambda *a: states.append(a),
    )
    monkeypatch.setattr(tool, "client", lambda *a: tracking)
    monkeypatch.setattr(tool, "snapshot", lambda client, key, directory: snapshots[key])
    monkeypatch.setattr(
        "sys.argv",
        [
            "compare",
            "--baseline-agreement-run-id",
            "a",
            "--candidate-agreement-run-id",
            "b",
            "--purpose",
            "system",
            "--hypothesis",
            "test",
            "--bootstrap-resamples",
            "0",
            "--output-dir",
            str(tmp_path),
            "--log",
        ],
    )
    with pytest.raises(RuntimeError, match="artifact storage"):
        tool.main()
    assert states == [("comparison-failed", "FAILED")]
    assert (tmp_path / "comparison-result.json").exists()


def test_snapshot_loads_and_checks_the_original_grading_profiles(tmp_path):
    tool = load_tool("compare")
    data = frozen("a")
    profiles = {}
    for g in data["gradings"]:
        profile = {"type": g["rater_type"], "name": g["rater_id"]}
        key = digest(profile)
        g["rater_profile_digest"] = key
        for label in g["ratings"]:
            label["protocol"] = key
        profiles[key] = profile
    refresh(data)
    grade_ids = {f"grading-{i}": g for i, g in enumerate(data["gradings"])}
    data["agreement"]["grading_run_ids"] = list(grade_ids)

    def get_run(key):
        if key in grade_ids:
            g = grade_ids[key]
            tags = {
                "evaluation.kind": "grading",
                "source_run_id": data["bundle"]["source_run_id"],
                "source_bundle_digest": data["bundle"]["digest"],
                "rater_profile_digest": g["rater_profile_digest"],
            }
        elif key == data["agreement_run_id"]:
            tags = {
                "evaluation.kind": "agreement",
                "evaluation.schema": SCHEMA,
                "source_run_id": data["bundle"]["source_run_id"],
                "source_bundle_digest": data["bundle"]["digest"],
            }
        else:
            tags = {"evaluation.kind": "answers", "evaluation.schema": "evaluation-v1"}
        return SimpleNamespace(
            info=SimpleNamespace(status="FINISHED", experiment_id="9"),
            data=SimpleNamespace(tags=tags),
        )

    def download(key, name, directory):
        content = (
            profiles[grade_ids[key]["rater_profile_digest"]]
            if name == "rater-profile.json"
            else (
                data["bundle"]
                if name == "evaluation-bundle.json"
                else data["agreement"]
                if name == "agreement.json"
                else {"results": data["gradings"]}
            )
        )
        path = Path(directory) / name
        path.write_text(json.dumps(content))
        return str(path)

    tracking = SimpleNamespace(get_run=get_run, download_artifacts=download)
    snap = tool.snapshot(tracking, data["agreement_run_id"], tmp_path)
    assert snap["profiles"] == profiles
    first_key = next(iter(profiles))
    profiles[first_key]["name"] = "tampered"
    with pytest.raises(ValueError, match="grading_profile_mismatch"):
        tool.snapshot(tracking, data["agreement_run_id"], tmp_path)


def test_judge_comparison_saves_profile_changes_without_treating_scores_as_quality_improvement():
    a = frozen("a")
    old = {"model": "gateway:/judge-v1"}
    key = digest(old)
    g = a["gradings"][1]
    g["rater_profile_digest"] = key
    for label in g["ratings"]:
        label["protocol"] = key
    a["profiles"] = {key: old}
    refresh(a)
    b = deepcopy(a)
    b["agreement_run_id"] = "new-agreement"
    new = {"model": "gateway:/judge-v2"}
    key = digest(new)
    g = b["gradings"][1]
    g["rater_profile_digest"] = key
    for label in g["ratings"]:
        label["protocol"] = key
    b["profiles"] = {key: new}
    refresh(b)
    report = run(a, b, purpose="judge")
    assert report["grading_profile_changes"][0]["changes"] == [
        {
            "path": "rater_profile.model",
            "baseline": "gateway:/judge-v1",
            "candidate": "gateway:/judge-v2",
        }
    ]
    assert all("improved" not in row["counts"] for row in report["quality"])
