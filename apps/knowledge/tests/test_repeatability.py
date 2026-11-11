from copy import deepcopy

import pytest
from test_comparison import frozen, refresh

from elice_knowledge.corpus import digest
from elice_knowledge.repeatability import boundary_changes, build


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
    for snapshot, identity in ((first, "analysis-A"), (second, "analysis-B")):
        snapshot["analysis_run_id"] = identity
        original = next(g for g in snapshot["gradings"] if g["rater_id"] == "model:chatgpt")
        repeated = deepcopy(original)
        repeated["round"] = "2"
        for rating in repeated["ratings"]:
            rating["round"] = "2"
        snapshot["gradings"].append(repeated)
        refresh(snapshot)
    target = next(
        row
        for row in first["analysis"]["distributions"]
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
        if row["analysis_run_id"] == "analysis-A"
        and row["rater"] == "model:chatgpt"
        and row["dimension"] == "correctness"
    )
    assert total["score_counts"] == {"0": 4, "1": 0, "2": 4}
    assert total["boundaries"]["partial_or_better"] == {"count": 4, "denominator": 8, "rate": 0.5}
    assert total["missing_records"] == 0


def test_repeatability_rejects_changed_conditions_and_reused_answers():
    first, second = frozen("A"), frozen("B")
    first["analysis_run_id"], second["analysis_run_id"] = "analysis-A", "analysis-B"
    second["bundle"]["qa_profile"]["top_k"] = 8
    second["bundle"]["digest"] = digest(
        {k: v for k, v in second["bundle"].items() if k != "digest"}
    )
    second["analysis"]["source_digest"] = second["bundle"]["digest"]
    with pytest.raises(ValueError, match="condition_mismatch"):
        build([first, second])
    second = frozen("B")
    second["analysis_run_id"] = "analysis-B"
    second["bundle"]["source_run_id"] = first["bundle"]["source_run_id"]
    with pytest.raises(ValueError):
        build([first, second])


def test_different_grading_round_counts_are_kept_with_distinct_denominators():
    first, second = frozen("A"), frozen("B")
    first["analysis_run_id"], second["analysis_run_id"] = "analysis-A", "analysis-B"
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
