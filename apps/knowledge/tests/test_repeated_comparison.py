from copy import deepcopy

import pytest

from elice_knowledge.corpus import digest
from elice_knowledge.repeated_comparison import build


def snapshot(run_id, model, rates, change, failures):
    profile = {"generation_model": model}
    records = [{"id": "q1", "inputs": {"question": "질문"}}]
    rater = "model:chatgpt"
    profile_digest = "judge-profile"
    agreements = [{"agreement_run_id": f"{run_id}-{i}"} for i in range(len(rates))]
    level = {
        "record_id": "q1",
        "rater": rater,
        "dimension": "correctness",
        "boundary": "sufficient",
        "mean_rate": sum(rates) / len(rates),
        "observed_runs": len(rates),
    }
    model_level = {
        **level,
        "observed_model_runs": len(rates),
    }
    change_row = {
        "rater": rater,
        "dimension": "correctness",
        "boundary": "sufficient",
        "mean_absolute_change": change,
        "observed_pairs": 1,
        "possible_pairs": 1,
    }
    report = {
        "repeatability_contract": "repeatability-v1",
        "dataset_digest": "dataset",
        "rubric_digest": "rubric",
        "kind": "gold",
        "qa_profile_digest": digest(profile),
        "summaries": [
            {
                "rater": rater,
                "profile_digest": profile_digest,
                "agreement_run_id": item["agreement_run_id"],
                "generation_failed_records": failed,
                "record_count": 1,
            }
            for item, failed in zip(agreements, failures, strict=True)
        ],
        "agreements": agreements,
        "boundary_levels": [level],
        "model_boundary_levels": [model_level],
        "boundary_changes": {"by_dimension": [change_row]},
        "model_boundary_changes": {"by_dimension": [change_row]},
        "resource_levels": [
            {
                "record_id": "q1",
                "metric": "qa_duration_ms",
                "mean": 1000,
                "observed_runs": 2,
                "possible_runs": 2,
            }
        ],
    }
    report["digest"] = digest(report)
    return {
        "run_id": run_id,
        "report": report,
        "inputs": {
            "agreements": [
                {
                    "bundle": {"records": records, "qa_profile": profile},
                    "grading_runs": [{"rater": rater}],
                }
                for _ in rates
            ]
        },
    }


def test_comparison_uses_repeatability_question_rates_and_stability():
    a = snapshot("a", "terra", [1, 0.5], 0.5, [0, 0])
    b = snapshot("b", "qwen", [0.5, 0], 0.5, [0, 1])
    result = build(a, b)
    quality = next(row for row in result["quality"] if row["rater"] == "model:chatgpt")
    assert (quality["baseline_mean"], quality["candidate_mean"]) == (0.75, 0.25)
    assert quality["paired_questions"] == 1
    assert result["stability"][0]["baseline"] == 0.5
    assert result["resources"][0]["baseline_mean"] == 1000
    assert result["failures"]["candidate"]["failed"] == 1
    assert result["protocol"]["grading_round_pairing"] is False


def test_comparison_rejects_mismatched_judge_repetitions():
    a = snapshot("a", "terra", [1, 0.5], 0.5, [0, 0])
    b = snapshot("b", "qwen", [0.5, 0], 0.5, [0, 1])
    b = deepcopy(b)
    b["inputs"]["agreements"][0]["grading_runs"].append({"rater": "model:chatgpt"})
    with pytest.raises(ValueError, match="comparison_grading_repetitions_mismatch"):
        build(a, b)


def test_comparison_rejects_previously_aggregated_random_baseline():
    a = snapshot("a", "terra", [1, 0.5], 0.5, [0, 0])
    b = snapshot("b", "qwen", [0.5, 0], 0.5, [0, 1])
    a["report"]["summaries"].append({"rater": "baseline:uniform-random"})
    with pytest.raises(ValueError, match="comparison_contains_baseline_rater"):
        build(a, b)
