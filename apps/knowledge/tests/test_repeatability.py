from copy import deepcopy

import pytest
from test_comparison import frozen, refresh

from elice_knowledge.corpus import digest
from elice_knowledge.repeatability import build


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
