"""Nominal reliability using known coefficients and explicit missing/round policies."""

from copy import deepcopy

import pytest
from test_agreement import bundle

from elice_knowledge.agreement import analyze, pairwise, review_packet
from elice_knowledge.corpus import digest
from elice_knowledge.reliability import panel, report_metrics


def selected(raters=None):
    ratings = raters or (
        ("human:alice", ["pass", "pass", "fail", "pass"]),
        ("model:chatgpt", ["pass", "fail", "fail", "pass"]),
        ("model:claude", ["fail", "fail", "fail", "pass"]),
    )
    return [
        (
            ("correctness", name, digest(name), "1"),
            {str(i): value for i, value in enumerate(values)},
        )
        for name, values in ratings
    ]


def test_known_complete_panel_coefficients():
    result = panel(selected(), list("0123"), scope="all")
    assert result["fleiss_kappa"] == pytest.approx(1 / 3)
    assert result["krippendorff_alpha"] == pytest.approx(7 / 18)
    assert result["complete_binary_cases"] == result["pairable_cases"] == 4
    assert result["subjects"] == 3 and result["role"] == "supplemental"


def test_incomplete_panel_uses_distinct_case_sets_and_reports_exclusions():
    rows = selected()
    extras = (
        ["pass", "error", "pass", None],
        ["not_applicable", "pass", "error", None],
        ["fail", "pass", "not_applicable", "pass"],
    )
    for (_, ratings), values in zip(rows, extras, strict=True):
        ratings.update({str(i + 4): value for i, value in enumerate(values)})
    result = panel(rows, list("01234567"), scope="all")
    assert result["fleiss_kappa"] == pytest.approx(1 / 3)
    assert result["krippendorff_alpha"] == pytest.approx(2 / 7)
    assert result["complete_binary_cases"] == 4 and result["pairable_cases"] == 6
    assert result["fleiss_excluded_cases"] == 4 and result["alpha_excluded_cases"] == 2
    assert (
        result["error_ratings"]
        == result["not_applicable_ratings"]
        == result["missing_ratings"]
        == 2
    )


def test_panel_never_counts_repeat_or_revision_as_an_independent_subject():
    rows = selected()
    key, ratings = rows[0]
    rows.append(((key[0], key[1], key[2], "2"), dict(ratings)))
    result = panel(rows, list("0123"), scope="all")
    assert result["subjects"] == 3 and result["selected_gradings"] == 4
    assert result["fleiss_kappa"] is result["krippendorff_alpha"] is None
    assert result["fleiss_undefined_reason"] == "multiple_gradings_per_subject"
    assert result["ambiguous_subjects"] == ["human:alice"]
    rows[-1] = ((key[0], key[1], digest("changed profile"), "1"), ratings)
    assert (
        panel(rows, list("0123"), scope="all")["alpha_undefined_reason"]
        == "multiple_gradings_per_subject"
    )


def test_degenerate_panels_are_unavailable_not_zero_or_nan():
    rows = selected([(r, ["pass", "pass"]) for r in ("human:a", "model:b", "model:c")])
    result = panel(rows, ["0", "1"], scope="all")
    assert result["fleiss_kappa"] is result["krippendorff_alpha"] is None
    assert result["alpha_undefined_reason"] == "constant_labels"
    assert panel(rows, ["0"], scope="all")["alpha_undefined_reason"] == "insufficient_cases"
    assert (
        panel(rows[:2], ["0", "1"], scope="all")["alpha_undefined_reason"] == "insufficient_raters"
    )
    empty = panel([], ["0"], scope="all")
    assert empty["fleiss_kappa"] is None and empty["subjects"] == 0
    unanimous = selected([(r, ["pass", "fail"]) for r in ("human:a", "model:b", "model:c")])
    assert panel(unanimous, ["0", "1"], scope="all")["fleiss_kappa"] == 1


def source_with_four_cases():
    source = bundle()
    source["records"] = [{**deepcopy(source["records"][0]), "id": str(i)} for i in range(4)]
    source["cases"] = [{**deepcopy(source["cases"][0]), "record_id": str(i)} for i in range(4)]
    source["digest"] = digest({k: v for k, v in source.items() if k != "digest"})
    return source


def test_analysis_prioritizes_human_alignment_and_separates_model_only_panel():
    source = source_with_four_cases()
    entries = []
    for name, values in (
        ("human:alice", ["pass", "pass", "fail", "pass"]),
        ("model:chatgpt", ["pass", "fail", "fail", "pass"]),
        ("model:claude", ["fail", "fail", "fail", "pass"]),
        ("model:gemini", ["pass", "pass", "fail", "pass"]),
    ):
        _, labels = review_packet(source, "unused")
        for row in labels:
            row.update(rater=name, protocol=digest(name), label=values[int(row["record_id"])])
        entries += labels
    report = analyze(source, entries)
    assert report["analysis_protocol"]["cohen"] == "ragas.DiscreteMetric.get_correlation"
    assert len([r for r in report["comparisons"] if r["purpose"] == "human_alignment"]) == 12
    assert len(report["panels"]) == 8  # all and models_only, independently for four dimensions.
    assert {r["scope"] for r in report["panels"]} == {"all", "models_only"}
    metrics = report_metrics(report)
    assert any(k.endswith(".fleiss_kappa") for k in metrics)
    assert any(k.endswith(".krippendorff_alpha") for k in metrics)
    repeat = [dict(r, round="2") for r in entries if r["rater"] == "human:alice"]
    repeated = analyze(source, entries + repeat)
    assert all(r["fleiss_kappa"] is None for r in repeated["panels"] if r["scope"] == "all")
    assert all(
        r["fleiss_kappa"] is not None for r in repeated["panels"] if r["scope"] == "models_only"
    )


def test_pairwise_negative_kappa_and_missing_items_are_preserved():
    result = pairwise({"a": "pass", "b": "fail", "missing": "pass"}, {"a": "fail", "b": "pass"})
    assert result["cohen_kappa"] == -1
    assert result["unpaired_items"] == 1


def test_cli_logs_panel_coefficients_and_protocol_without_model_calls(monkeypatch, tmp_path):
    import json
    import math
    from types import SimpleNamespace

    from test_evaluation_tools import load_tool

    source = source_with_four_cases()
    tool = load_tool("review")
    logged, artifacts = {}, []

    def download(run_id, filename, directory):
        assert run_id == source["source_run_id"] and filename == "evaluation-bundle.json"
        path = tmp_path / "bundle.json"
        path.write_text(json.dumps(source))
        return str(path)

    tracking = SimpleNamespace(
        download_artifacts=download,
        create_run=lambda *a, **kw: SimpleNamespace(info=SimpleNamespace(run_id="analysis")),
        log_dict=lambda *a: artifacts.append(a),
        log_metric=lambda run, key, value: logged.update({key: value}),
        set_terminated=lambda *a: None,
    )
    monkeypatch.setattr(tool, "configure", lambda *a: "experiment")
    monkeypatch.setattr(tool, "client", lambda *a: tracking)
    argv = [
        "review",
        "analyze",
        "--source-run-id",
        source["source_run_id"],
        "--output-dir",
        str(tmp_path / "report"),
        "--log",
    ]
    for i, ((_, _, _, _), values) in enumerate(selected()):
        name = f"human:reviewer-{i}"
        _, labels = review_packet(source, "unused")
        profile = digest(name)
        for row in labels:
            row.update(rater=name, protocol=profile, label=values[row["record_id"]])
        manifest = dict(
            schema=source["schema"],
            source_run_id=source["source_run_id"],
            source_bundle_digest=source["digest"],
            rater_type="human",
            rater_id=name,
            rater_profile_digest=profile,
            round=1,
            complete=True,
            labels=labels,
        )
        path = tmp_path / f"reviewer-{i}.json"
        path.write_text(json.dumps(manifest))
        argv += ["--grading-file", str(path)]
    monkeypatch.setattr("sys.argv", argv)
    tool.main()
    assert all(math.isfinite(v) for v in logged.values())
    fleiss = [v for k, v in logged.items() if k.endswith(".fleiss_kappa")]
    alpha = [v for k, v in logged.items() if k.endswith(".krippendorff_alpha")]
    assert len(fleiss) == len(alpha) == 4
    assert fleiss == pytest.approx([1 / 3] * 4)
    assert alpha == pytest.approx([7 / 18] * 4)
    assert any(
        a[2] == "agreement.json" and a[1]["analysis_protocol"]["versions"] for a in artifacts
    )


def test_fully_unscored_subject_is_not_counted_as_a_third_effective_rater():
    rows = selected(
        [
            ("human:alice", ["not_applicable", "not_applicable"]),
            ("model:chatgpt", ["pass", "fail"]),
            ("model:claude", ["fail", "fail"]),
        ]
    )
    result = panel(rows, ["0", "1"], scope="all")
    assert result["subjects"] == 3 and result["alpha_effective_subjects"] == 2
    assert result["krippendorff_alpha"] is None
    assert result["alpha_undefined_reason"] == "insufficient_effective_raters"
    assert result["rater_coverage"][0]["alpha_binary_ratings"] == 0
