"""Compare two saved repeatability reports without reopening grading observations."""

from collections import defaultdict

from elice_knowledge.corpus import digest

CONTRACT = "comparison-v2"
MODEL_MEAN = "aggregate:models"


def _records(snapshot):
    return snapshot["inputs"]["agreements"][0]["bundle"]["records"]


def _profile(snapshot):
    return snapshot["inputs"]["agreements"][0]["bundle"]["qa_profile"]


def _rater_profiles(report):
    result = defaultdict(set)
    for row in report["summaries"]:
        result[row["rater"]].add(row["profile_digest"])
    if any(len(profiles) != 1 for profiles in result.values()):
        raise ValueError("comparison_rater_profile_varies_within_condition")
    return {rater: next(iter(profiles)) for rater, profiles in result.items()}


def _grading_counts(snapshot):
    counts = defaultdict(list)
    for agreement in snapshot["inputs"]["agreements"]:
        per_rater = defaultdict(int)
        for grading in agreement.get("grading_runs", []):
            per_rater[grading["rater"]] += 1
        for rater in _rater_profiles(snapshot["report"]):
            counts[rater].append(per_rater[rater])
    return dict(counts)


def _index(rows, *, model=False):
    return {
        (
            row["record_id"],
            MODEL_MEAN if model else row["rater"],
            row["dimension"],
            row["boundary"],
        ): row
        for row in rows
    }


def _quality(left, right, record_ids):
    a = {**_index(left["boundary_levels"]), **_index(left["model_boundary_levels"], model=True)}
    b = {**_index(right["boundary_levels"]), **_index(right["model_boundary_levels"], model=True)}
    result = []
    for rater, dimension, boundary in sorted({key[1:] for key in a} | {key[1:] for key in b}):
        cases = []
        for record_id in record_ids:
            x = a.get((record_id, rater, dimension, boundary))
            y = b.get((record_id, rater, dimension, boundary))
            av = x["mean_rate"] if x else None
            bv = y["mean_rate"] if y else None
            cases.append(
                dict(
                    record_id=record_id,
                    baseline=av,
                    candidate=bv,
                    delta=bv - av if av is not None and bv is not None else None,
                    baseline_answers=x.get("observed_runs", x.get("observed_model_runs"))
                    if x
                    else 0,
                    candidate_answers=y.get("observed_runs", y.get("observed_model_runs"))
                    if y
                    else 0,
                )
            )
        paired = [case for case in cases if case["delta"] is not None]
        result.append(
            dict(
                rater=rater,
                dimension=dimension,
                boundary=boundary,
                baseline_mean=sum(case["baseline"] for case in paired) / len(paired)
                if paired
                else None,
                candidate_mean=sum(case["candidate"] for case in paired) / len(paired)
                if paired
                else None,
                delta_mean=sum(case["delta"] for case in paired) / len(paired) if paired else None,
                paired_questions=len(paired),
                possible_questions=len(record_ids),
                cases=cases,
            )
        )
    return result


def _stability(report, rater, dimension, boundary):
    rows = (
        report["model_boundary_changes"]["by_dimension"]
        if rater == MODEL_MEAN
        else report["boundary_changes"]["by_dimension"]
    )
    return next(
        (
            row
            for row in rows
            if row["dimension"] == dimension
            and row["boundary"] == boundary
            and (rater == MODEL_MEAN or row["rater"] == rater)
        ),
        None,
    )


def _changes(left, right, quality):
    rows = []
    for item in quality:
        key = (item["rater"], item["dimension"], item["boundary"])
        a = _stability(left, *key)
        b = _stability(right, *key)
        av = a["mean_absolute_change"] if a else None
        bv = b["mean_absolute_change"] if b else None
        rows.append(
            dict(
                rater=key[0],
                dimension=key[1],
                boundary=key[2],
                baseline=av,
                candidate=bv,
                delta=bv - av if av is not None and bv is not None else None,
                baseline_pairs=a["observed_pairs"] if a else 0,
                baseline_possible_pairs=a["possible_pairs"] if a else 0,
                candidate_pairs=b["observed_pairs"] if b else 0,
                candidate_possible_pairs=b["possible_pairs"] if b else 0,
            )
        )
    return rows


def _failures(snapshot):
    report = snapshot["report"]
    rows = {}
    for row in report["summaries"]:
        rows[row["agreement_run_id"]] = (row["generation_failed_records"], row["record_count"])
    return dict(
        failed=sum(failed for failed, _ in rows.values()),
        total=sum(total for _, total in rows.values()),
        by_answers=[
            dict(
                agreement_run_id=agreement_id,
                failed=rows[agreement_id][0],
                total=rows[agreement_id][1],
            )
            for agreement_id in [x["agreement_run_id"] for x in report["agreements"]]
        ],
    )


def _resources(left, right, record_ids):
    indexes = [
        {(row["record_id"], row["metric"]): row for row in report["resource_levels"]}
        for report in (left, right)
    ]
    metrics = sorted({key[1] for index in indexes for key in index})
    result = []
    for metric in metrics:
        cases = []
        for record_id in record_ids:
            a, b = (index.get((record_id, metric), {}) for index in indexes)
            cases.append(
                {
                    "record_id": record_id,
                    "baseline": a.get("mean"),
                    "candidate": b.get("mean"),
                    "baseline_runs": a.get("observed_runs", 0),
                    "candidate_runs": b.get("observed_runs", 0),
                }
            )
        paired = [
            row for row in cases if row["baseline"] is not None and row["candidate"] is not None
        ]
        result.append(
            {
                "metric": metric,
                "baseline_mean": sum(row["baseline"] for row in paired) / len(paired)
                if paired
                else None,
                "candidate_mean": sum(row["candidate"] for row in paired) / len(paired)
                if paired
                else None,
                "paired_questions": len(paired),
                "possible_questions": len(record_ids),
                "baseline_observations": sum(row["baseline_runs"] for row in paired),
                "candidate_observations": sum(row["candidate_runs"] for row in paired),
                "cases": cases,
            }
        )
    return result


def build(baseline, candidate):
    """Use repeatability-level question rates and changes; never average raw grades."""
    a, b = baseline["report"], candidate["report"]
    if not all(isinstance(report.get("resource_levels"), list) for report in (a, b)):
        raise ValueError("comparison_requires_repeatability_resources")
    if (
        a["repeatability_contract"] != "repeatability-v1"
        or b["repeatability_contract"] != "repeatability-v1"
    ):
        raise ValueError("comparison_requires_repeatability_v1")
    if baseline["run_id"] == candidate["run_id"]:
        raise ValueError("comparison_requires_distinct_repeatability_runs")
    if (a["dataset_digest"], a["rubric_digest"], a["kind"]) != (
        b["dataset_digest"],
        b["rubric_digest"],
        b["kind"],
    ):
        raise ValueError("comparison_dataset_or_rubric_mismatch")
    ar, br = _records(baseline), _records(candidate)
    if ar != br:
        raise ValueError("comparison_question_or_expectation_changed")
    profiles_a, profiles_b = _rater_profiles(a), _rater_profiles(b)
    if profiles_a != profiles_b:
        raise ValueError("comparison_rater_profile_mismatch")
    counts_a, counts_b = _grading_counts(baseline), _grading_counts(candidate)
    if counts_a != counts_b:
        raise ValueError("comparison_grading_repetitions_mismatch")
    ap, bp = _profile(baseline), _profile(candidate)
    if (
        a["qa_profile_digest"] == b["qa_profile_digest"]
        or digest(ap) != a["qa_profile_digest"]
        or digest(bp) != b["qa_profile_digest"]
    ):
        raise ValueError("comparison_requires_distinct_recorded_conditions")
    record_ids = [record["id"] for record in ar]
    quality = _quality(a, b, record_ids)
    result = dict(
        schema="evaluation-v2",
        comparison_contract=CONTRACT,
        baseline=dict(
            repeatability_run_id=baseline["run_id"],
            repeatability_digest=a["digest"],
            answers_runs=len(a["agreements"]),
        ),
        candidate=dict(
            repeatability_run_id=candidate["run_id"],
            repeatability_digest=b["digest"],
            answers_runs=len(b["agreements"]),
        ),
        dataset_digest=a["dataset_digest"],
        rubric_digest=a["rubric_digest"],
        kind=a["kind"],
        record_ids=record_ids,
        rater_profiles=profiles_a,
        grading_counts=counts_a,
        qa_profile_changes=[key for key in sorted(set(ap) | set(bp)) if ap.get(key) != bp.get(key)],
        quality=quality,
        stability=_changes(a, b, quality),
        resources=_resources(a, b, record_ids),
        failures=dict(baseline=_failures(baseline), candidate=_failures(candidate)),
        protocol=dict(
            quality="paired_question_macro_mean_of_repeatability_boundary_rates",
            stability="saved_question_paired_mean_absolute_boundary_rate_change",
            grading_round_pairing=False,
            significance_test=False,
            quality_unit="rate_fraction",
            stability_unit="rate_fraction",
        ),
    )
    result["digest"] = digest(result)
    return result


def metrics(report):
    result = {
        "comparison.answers.baseline": report["baseline"]["answers_runs"],
        "comparison.answers.candidate": report["candidate"]["answers_runs"],
        "comparison.failures.baseline": report["failures"]["baseline"]["failed"],
        "comparison.failures.candidate": report["failures"]["candidate"]["failed"],
    }
    for row in report["quality"]:
        if row["rater"] == MODEL_MEAN and row["delta_mean"] is not None:
            result[f"comparison.models.{row['boundary']}.{row['dimension']}.delta"] = row[
                "delta_mean"
            ]
    return result
