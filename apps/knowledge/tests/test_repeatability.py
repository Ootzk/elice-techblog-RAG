from copy import deepcopy

import pytest
from test_comparison import frozen, refresh

from elice_knowledge.corpus import digest
from elice_knowledge.repeatability import (
    boundary_changes,
    boundary_levels,
    build,
    dimension_average_levels,
    model_boundary_changes,
    model_boundary_levels,
    question_average_levels,
    resource_levels,
)


def test_resource_levels_exclude_failed_and_missing_observations():
    snapshots = [
        {
            "bundle": {
                "cases": [
                    {
                        "record_id": "q1",
                        "error": None,
                        "duration_ms": 1000,
                        "result": {"usage": {"prompt_tokens": 100}},
                    }
                ]
            }
        },
        {
            "bundle": {
                "cases": [
                    {"record_id": "q1", "error": "timeout", "duration_ms": 9000, "result": None}
                ]
            }
        },
    ]
    rows = {row["metric"]: row for row in resource_levels(snapshots, ["q1"])}
    assert rows["qa_duration_ms"]["mean"] == 1000
    assert rows["input_tokens"]["observed_runs"] == 1
    assert rows["cached_input_tokens"]["mean"] is None


def test_boundary_levels_weight_answers_runs_equally_and_keep_raters_separate():
    def scored(count, valid):
        return {
            "valid": valid,
            "boundaries": {"sufficient": {"rate": count / valid if valid else None}},
        }

    rows = {
        ("q1", "model:chatgpt", "correctness"): {
            "A1": scored(1, 1),
            "A2": scored(0, 3),
            "A3": scored(0, 0),
        },
        ("q1", "human:reviewer", "correctness"): {"A1": scored(0, 1)},
    }
    levels = boundary_levels(
        rows,
        ["A1", "A2", "A3"],
        ["q1"],
        [("model:chatgpt", "correctness"), ("human:reviewer", "correctness")],
        ("sufficient",),
    )
    model, human = levels
    assert model["mean_rate"] == 0.5  # pooled grading would incorrectly give 0.25
    assert (model["observed_runs"], model["possible_runs"], model["valid_gradings"]) == (
        2,
        3,
        4,
    )
    assert human["mean_rate"] == 0
    assert human["observed_runs"] == 1


def test_model_boundary_mean_weights_models_equally_and_excludes_human():
    levels = [
        dict(
            record_id="q1",
            rater=rater,
            dimension="correctness",
            boundary="sufficient",
            mean_rate=rate,
            observed_runs=runs,
            possible_runs=3,
            valid_gradings=gradings,
        )
        for rater, rate, runs, gradings in (
            ("model:chatgpt", 1.0, 3, 9),
            ("model:claude", 0.0, 1, 1),
            ("human:reviewer", 1.0, 3, 3),
        )
    ]
    result = model_boundary_levels(levels, ["q1"], ["correctness"], ["sufficient"])[0]
    assert result["mean_rate"] == 0.5
    assert (result["observed_models"], result["possible_models"]) == (2, 2)
    assert (
        result["observed_model_runs"],
        result["possible_model_runs"],
        result["valid_gradings"],
    ) == (4, 6, 10)


def test_question_average_skips_unrated_questions_and_weights_questions_equally():
    levels = [
        dict(
            record_id=record_id,
            rater="model:chatgpt",
            dimension="correctness",
            boundary="sufficient",
            mean_rate=rate,
            valid_gradings=valid,
        )
        for record_id, rate, valid in (("q1", 1.0, 9), ("q2", 0.0, 1), ("q3", None, 0))
    ]
    result = question_average_levels(
        levels, [], ["q1", "q2", "q3"], ["correctness"], ["sufficient"]
    )
    model = next(item for item in result if item["rater"] == "model:chatgpt")
    assert model["mean_rate"] == 0.5
    assert (model["observed_questions"], model["possible_questions"]) == (2, 3)
    assert model["valid_gradings"] == 10


def test_dimension_average_uses_valid_dimensions_and_matches_bottom_row_columns():
    levels = [
        dict(
            record_id="q1",
            rater="model:chatgpt",
            dimension=dimension,
            boundary="sufficient",
            mean_rate=rate,
            valid_gradings=valid,
        )
        for dimension, rate, valid in (
            ("correctness", 1.0, 3),
            ("completeness", 0.0, 3),
            ("groundedness", None, 0),
        )
    ]
    questions = [
        dict(
            rater="model:chatgpt",
            dimension=dimension,
            boundary="sufficient",
            mean_rate=rate,
            valid_gradings=valid,
        )
        for dimension, rate, valid in (
            ("correctness", 0.75, 6),
            ("completeness", 0.25, 6),
            ("groundedness", None, 0),
        )
    ]
    result = dimension_average_levels(
        levels,
        [],
        questions,
        ["q1"],
        ["correctness", "completeness", "groundedness"],
        ["sufficient"],
    )
    model = [item for item in result if item["rater"] == "model:chatgpt"]
    assert [item["mean_rate"] for item in model] == [0.5, 0.5]
    assert [(item["observed_dimensions"], item["possible_dimensions"]) for item in model] == [
        (2, 3),
        (2, 3),
    ]


def test_model_change_mean_excludes_human_and_preserves_pair_coverage():
    changes = {
        "by_rater": [{"rater": rater} for rater in ("model:chatgpt", "model:claude", "human:x")],
        "by_dimension": [
            dict(
                rater=rater,
                dimension="correctness",
                boundary="sufficient",
                mean_absolute_change=value,
                observed_pairs=pairs,
                possible_pairs=3,
            )
            for rater, value, pairs in (
                ("model:chatgpt", 0.0, 3),
                ("model:claude", 1.0, 1),
                ("human:x", 1.0, 3),
            )
        ],
    }
    result = model_boundary_changes(changes, ["correctness"], ["sufficient"])
    item = result["by_dimension"][0]
    assert item["mean_absolute_change"] == 0.5
    assert (item["observed_models"], item["observed_pairs"], item["possible_pairs"]) == (2, 4, 6)
    assert result["overall"][0]["mean_absolute_change"] == 0.5


def test_boundary_changes_keep_question_pairs_and_equal_question_weight():
    def scored(partial, sufficient):
        return {
            "valid": 3,
            "boundaries": {
                "partial_or_better": {"rate": partial},
                "sufficient": {"rate": sufficient},
            },
        }

    rows = {
        ("q1", "model:chatgpt", "correctness"): {
            "A1": scored(1, 1),
            "A2": scored(1, 0),
            "A3": scored(1, 1),
        },
        ("q2", "model:chatgpt", "correctness"): {
            "A1": scored(0, 0),
            "A2": scored(1, 1),
            "A3": {"valid": 0},
        },
    }
    result = boundary_changes(
        rows,
        ["A1", "A2", "A3"],
        ["q1", "q2"],
        [("model:chatgpt", "correctness")],
        ("partial_or_better", "sufficient"),
    )
    by_boundary = {item["boundary"]: item for item in result["overall"]}
    assert by_boundary["partial_or_better"]["mean_absolute_change"] == 0.5
    assert by_boundary["sufficient"]["mean_absolute_change"] == pytest.approx(5 / 6)
    assert (
        by_boundary["sufficient"]["observed_pairs"],
        by_boundary["sufficient"]["possible_pairs"],
    ) == (4, 6)
    # q1 contributes 2/3 and q2 contributes 1, despite q2 having fewer valid pairs.


def test_boundary_changes_detect_swapped_question_outcomes():
    def scored(rate):
        return {"valid": 1, "boundaries": {"satisfied": {"rate": rate}}}

    rows = {
        ("q1", "model:chatgpt", "behavior"): {"A1": scored(1), "A2": scored(0)},
        ("q2", "model:chatgpt", "behavior"): {"A1": scored(0), "A2": scored(1)},
    }
    result = boundary_changes(
        rows,
        ["A1", "A2"],
        ["q1", "q2"],
        [("model:chatgpt", "behavior")],
        ("satisfied",),
    )
    assert result["overall"][0]["mean_absolute_change"] == 1


def test_boundary_counts_preserve_all_judge_rounds_without_pairing():
    first = frozen("A")
    second = frozen("B")
    for snapshot, identity in ((first, "agreement-A"), (second, "agreement-B")):
        snapshot["agreement_run_id"] = identity
        original = next(g for g in snapshot["gradings"] if g["rater_id"] == "model:chatgpt")
        repeated = deepcopy(original)
        repeated["round"] = "2"
        for rating in repeated["ratings"]:
            rating["round"] = "2"
        snapshot["gradings"].append(repeated)
        refresh(snapshot)
    target = next(
        row
        for row in first["agreement"]["distributions"]
        if row["record_id"] == "0"
        and row["rater"] == "model:chatgpt"
        and row["dimension"] == "correctness"
    )
    assert target["score_counts"] == {"0": 0, "1": 0, "2": 2}
    assert target["boundaries"]["sufficient"] == {"count": 2, "denominator": 2, "rate": 1.0}
    report = build([first, second])
    assert report["repeatability_contract"] == "repeatability-v1"
    assert {row["boundary"] for row in report["boundary_changes"]["overall"]} == {
        "partial_or_better",
        "sufficient",
    }
    assert "cases" not in report and "summary_ranges" not in report
    total = next(
        row
        for row in report["summaries"]
        if row["agreement_run_id"] == "agreement-A"
        and row["rater"] == "model:chatgpt"
        and row["dimension"] == "correctness"
    )
    assert total["score_counts"] == {"0": 4, "1": 0, "2": 4}
    assert total["boundaries"]["partial_or_better"] == {"count": 4, "denominator": 8, "rate": 0.5}
    assert total["missing_records"] == 0


def test_repeatability_rejects_changed_conditions_and_reused_answers():
    first, second = frozen("A"), frozen("B")
    first["agreement_run_id"], second["agreement_run_id"] = "agreement-A", "agreement-B"
    second["bundle"]["qa_profile"]["top_k"] = 8
    second["bundle"]["digest"] = digest(
        {k: v for k, v in second["bundle"].items() if k != "digest"}
    )
    second["agreement"]["source_digest"] = second["bundle"]["digest"]
    with pytest.raises(ValueError, match="condition_mismatch"):
        build([first, second])
    second = frozen("B")
    second["agreement_run_id"] = "agreement-B"
    second["bundle"]["source_run_id"] = first["bundle"]["source_run_id"]
    with pytest.raises(ValueError):
        build([first, second])


def test_different_grading_round_counts_are_kept_with_distinct_denominators():
    first, second = frozen("A"), frozen("B")
    first["agreement_run_id"], second["agreement_run_id"] = "agreement-A", "agreement-B"
    original = next(g for g in first["gradings"] if g["rater_id"] == "model:chatgpt")
    repeated = deepcopy(original)
    repeated["round"] = "2"
    for rating in repeated["ratings"]:
        rating["round"] = "2"
    first["gradings"].append(repeated)
    refresh(first)
    report = build([first, second])
    chatgpt = [
        row
        for row in report["summaries"]
        if row["rater"] == "model:chatgpt" and row["dimension"] == "correctness"
    ]
    assert [row["attempts"] for row in chatgpt] == [8, 4]
    assert [row["boundaries"]["sufficient"]["denominator"] for row in chatgpt] == [8, 4]
