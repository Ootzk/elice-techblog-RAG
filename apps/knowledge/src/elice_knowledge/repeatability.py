"""Descriptive rubric-boundary stability across independent answers runs."""

from collections import defaultdict
from itertools import combinations

from elice_knowledge.corpus import digest
from elice_knowledge.evaluation_bundle import validate_bundle
from elice_knowledge.evaluation_schema import SCHEMA

CONTRACT = "repeatability-v1"
MODEL_MEAN = "aggregate:models"


def boundary_levels(rows, run_ids, record_ids, rater_dimensions, boundary_names):
    """Average per-answers-run boundary rates for each question and evaluator."""
    result = []
    for record_id in record_ids:
        for rater, dimension in rater_dimensions:
            distributions = rows.get((record_id, rater, dimension), {})
            for boundary in boundary_names:
                valid = [
                    distributions[run_id]
                    for run_id in run_ids
                    if run_id in distributions and distributions[run_id]["valid"]
                ]
                result.append(
                    dict(
                        record_id=record_id,
                        rater=rater,
                        dimension=dimension,
                        boundary=boundary,
                        mean_rate=(
                            sum(item["boundaries"][boundary]["rate"] for item in valid) / len(valid)
                            if valid
                            else None
                        ),
                        observed_runs=len(valid),
                        possible_runs=len(run_ids),
                        valid_gradings=sum(item["valid"] for item in valid),
                    )
                )
    return result


def model_boundary_levels(levels, record_ids, dimensions, boundary_names):
    """Macro-average available model rates after each model's answers-run average."""
    model_ids = {item["rater"] for item in levels if item["rater"].startswith("model:")}
    possible_runs = max((item["possible_runs"] for item in levels), default=0)
    indexed = {
        (item["record_id"], item["rater"], item["dimension"], item["boundary"]): item
        for item in levels
    }
    result = []
    for record_id in record_ids:
        for dimension in dimensions:
            for boundary in boundary_names:
                observed = [
                    indexed[(record_id, rater, dimension, boundary)]
                    for rater in sorted(model_ids)
                    if (record_id, rater, dimension, boundary) in indexed
                    and indexed[(record_id, rater, dimension, boundary)]["mean_rate"] is not None
                ]
                result.append(
                    dict(
                        record_id=record_id,
                        dimension=dimension,
                        boundary=boundary,
                        mean_rate=(
                            sum(item["mean_rate"] for item in observed) / len(observed)
                            if observed
                            else None
                        ),
                        observed_models=len(observed),
                        possible_models=len(model_ids),
                        observed_model_runs=sum(item["observed_runs"] for item in observed),
                        possible_model_runs=len(model_ids) * possible_runs,
                        valid_gradings=sum(item["valid_gradings"] for item in observed),
                    )
                )
    return result


def question_average_levels(levels, model_levels, record_ids, dimensions, boundary_names):
    """Macro-average available questions after answers and model aggregation."""
    raters = sorted({item["rater"] for item in levels}) + [MODEL_MEAN]
    combined = [*levels, *(dict(item, rater=MODEL_MEAN) for item in model_levels)]
    indexed = {
        (item["record_id"], item["rater"], item["dimension"], item["boundary"]): item
        for item in combined
    }
    result = []
    for rater in raters:
        for dimension in dimensions:
            for boundary in boundary_names:
                observed = [
                    indexed[(record_id, rater, dimension, boundary)]
                    for record_id in record_ids
                    if (record_id, rater, dimension, boundary) in indexed
                    and indexed[(record_id, rater, dimension, boundary)]["mean_rate"] is not None
                ]
                result.append(
                    dict(
                        rater=rater,
                        dimension=dimension,
                        boundary=boundary,
                        mean_rate=(
                            sum(item["mean_rate"] for item in observed) / len(observed)
                            if observed
                            else None
                        ),
                        observed_questions=len(observed),
                        possible_questions=len(record_ids),
                        valid_gradings=sum(item["valid_gradings"] for item in observed),
                    )
                )
    return result


def dimension_average_levels(
    levels, model_levels, question_averages, record_ids, dimensions, boundary_names
):
    """Macro-average available rubric dimensions for each question and the question-average row."""
    raters = sorted({item["rater"] for item in question_averages})
    combined = [
        *levels,
        *(dict(item, rater=MODEL_MEAN) for item in model_levels),
        *(dict(item, record_id=None) for item in question_averages),
    ]
    indexed = {
        (item["record_id"], item["rater"], item["dimension"], item["boundary"]): item
        for item in combined
    }
    result = []
    for record_id in (*record_ids, None):
        for rater in raters:
            for boundary in boundary_names:
                observed = [
                    indexed[(record_id, rater, dimension, boundary)]
                    for dimension in dimensions
                    if (record_id, rater, dimension, boundary) in indexed
                    and indexed[(record_id, rater, dimension, boundary)]["mean_rate"] is not None
                ]
                result.append(
                    dict(
                        record_id=record_id,
                        rater=rater,
                        boundary=boundary,
                        mean_rate=(
                            sum(item["mean_rate"] for item in observed) / len(observed)
                            if observed
                            else None
                        ),
                        observed_dimensions=len(observed),
                        possible_dimensions=len(dimensions),
                        valid_gradings=sum(item["valid_gradings"] for item in observed),
                    )
                )
    return result


def boundary_changes(rows, run_ids, record_ids, rater_dimensions, boundary_names):
    """Compare matching questions across answers runs, then macro-average each level."""
    run_pairs = list(combinations(run_ids, 2))
    by_question = []
    by_dimension = []
    by_rater = []
    overall = []

    def average(values):
        return sum(values) / len(values) if values else None

    for boundary in boundary_names:
        for rater, dimension in rater_dimensions:
            question_rows = []
            for record_id in record_ids:
                distributions = rows.get((record_id, rater, dimension), {})
                differences = []
                for left_id, right_id in run_pairs:
                    left, right = distributions.get(left_id), distributions.get(right_id)
                    if left is None or right is None or not left["valid"] or not right["valid"]:
                        continue
                    differences.append(
                        abs(
                            left["boundaries"][boundary]["rate"]
                            - right["boundaries"][boundary]["rate"]
                        )
                    )
                question_rows.append(
                    dict(
                        boundary=boundary,
                        rater=rater,
                        dimension=dimension,
                        record_id=record_id,
                        mean_absolute_change=average(differences),
                        observed_pairs=len(differences),
                        possible_pairs=len(run_pairs),
                    )
                )
            by_question.extend(question_rows)
            available = [
                item["mean_absolute_change"]
                for item in question_rows
                if item["mean_absolute_change"] is not None
            ]
            by_dimension.append(
                dict(
                    boundary=boundary,
                    rater=rater,
                    dimension=dimension,
                    mean_absolute_change=average(available),
                    observed_questions=len(available),
                    possible_questions=len(record_ids),
                    observed_pairs=sum(item["observed_pairs"] for item in question_rows),
                    possible_pairs=len(record_ids) * len(run_pairs),
                )
            )
        for rater in sorted(
            {item["rater"] for item in by_dimension if item["boundary"] == boundary}
        ):
            selected = [
                item
                for item in by_dimension
                if item["boundary"] == boundary and item["rater"] == rater
            ]
            available = [
                item["mean_absolute_change"]
                for item in selected
                if item["mean_absolute_change"] is not None
            ]
            by_rater.append(
                dict(
                    boundary=boundary,
                    rater=rater,
                    mean_absolute_change=average(available),
                    observed_pairs=sum(item["observed_pairs"] for item in selected),
                    possible_pairs=sum(item["possible_pairs"] for item in selected),
                )
            )
        selected = [item for item in by_rater if item["boundary"] == boundary]
        available = [
            item["mean_absolute_change"]
            for item in selected
            if item["mean_absolute_change"] is not None
        ]
        overall.append(
            dict(
                boundary=boundary,
                mean_absolute_change=average(available),
                observed_pairs=sum(item["observed_pairs"] for item in selected),
                possible_pairs=sum(item["possible_pairs"] for item in selected),
            )
        )
    return dict(
        method="question_paired_mean_absolute_boundary_rate_change",
        unit="rate_fraction",
        weighting="equal_answers_pairs_within_question_then_questions_dimensions_raters",
        overall=overall,
        by_rater=by_rater,
        by_dimension=by_dimension,
        by_question=by_question,
    )


def model_boundary_changes(changes, dimensions, boundary_names):
    """Macro-average saved question-paired changes across model raters only."""
    model_ids = {
        item["rater"] for item in changes["by_rater"] if item["rater"].startswith("model:")
    }
    indexed = {
        (item["rater"], item["dimension"], item["boundary"]): item
        for item in changes["by_dimension"]
    }
    by_dimension, overall = [], []
    for boundary in boundary_names:
        for dimension in dimensions:
            observed = [
                indexed[(rater, dimension, boundary)]
                for rater in sorted(model_ids)
                if (rater, dimension, boundary) in indexed
                and indexed[(rater, dimension, boundary)]["mean_absolute_change"] is not None
            ]
            by_dimension.append(
                dict(
                    boundary=boundary,
                    dimension=dimension,
                    mean_absolute_change=(
                        sum(item["mean_absolute_change"] for item in observed) / len(observed)
                        if observed
                        else None
                    ),
                    observed_models=len(observed),
                    possible_models=len(model_ids),
                    observed_pairs=sum(item["observed_pairs"] for item in observed),
                    possible_pairs=sum(item["possible_pairs"] for item in observed),
                )
            )
        available = [
            item
            for item in by_dimension
            if item["boundary"] == boundary and item["mean_absolute_change"] is not None
        ]
        overall.append(
            dict(
                boundary=boundary,
                mean_absolute_change=(
                    sum(item["mean_absolute_change"] for item in available) / len(available)
                    if available
                    else None
                ),
                observed_dimensions=len(available),
                possible_dimensions=len(dimensions),
                observed_pairs=sum(item["observed_pairs"] for item in available),
                possible_pairs=sum(
                    item["possible_pairs"] for item in by_dimension if item["boundary"] == boundary
                ),
            )
        )
    return dict(by_dimension=by_dimension, overall=overall)


def build(snapshots):
    if len(snapshots) < 2:
        raise ValueError("repeatability_requires_two_analyses")
    ids = [s["analysis_run_id"] for s in snapshots]
    if len(ids) != len(set(ids)):
        raise ValueError("repeatability_duplicate_analysis")
    first = snapshots[0]["bundle"]
    validate_bundle(first)
    answer_ids = []
    profiles = {}
    rows = defaultdict(dict)
    for snapshot in snapshots:
        bundle, analysis = snapshot["bundle"], snapshot["analysis"]
        validate_bundle(bundle)
        if (
            analysis.get("schema") != SCHEMA
            or analysis.get("source_run_id") != bundle["source_run_id"]
            or analysis.get("source_digest") != bundle["digest"]
            or analysis.get("kind") != bundle["kind"]
            or not isinstance(analysis.get("distributions"), list)
            or analysis.get("rubric_digest") != digest(analysis.get("rubric"))
        ):
            raise ValueError("repeatability_invalid_analysis")
        if (
            any(
                bundle[key] != first[key]
                for key in ("dataset_id", "dataset_digest", "kind", "records", "qa_profile")
            )
            or analysis["rubric_digest"] != snapshots[0]["analysis"]["rubric_digest"]
        ):
            raise ValueError("repeatability_condition_mismatch")
        answer_id = bundle["source_run_id"]
        if answer_id in answer_ids:
            raise ValueError("repeatability_requires_distinct_answers")
        answer_ids.append(answer_id)
        eligible = {case["record_id"] for case in bundle["cases"] if not case["error"]}
        scale = {str(score) for score in analysis["rubric"]["scale"]}
        for item in analysis["distributions"]:
            if item["record_id"] not in eligible or set(item["score_counts"]) != scale:
                raise ValueError("repeatability_invalid_distribution")
            rater, profile = item["rater"], item["profile_digest"]
            if rater in profiles and profiles[rater] != profile:
                raise ValueError("repeatability_rater_profile_changed")
            profiles[rater] = profile
            key = (item["record_id"], rater, item["dimension"])
            if snapshot["analysis_run_id"] in rows[key]:
                raise ValueError("repeatability_duplicate_distribution")
            if item["attempts"] != item["valid"] + sum(item["status_counts"].values()):
                raise ValueError("repeatability_invalid_counts")
            if item["valid"] != sum(item["score_counts"].values()):
                raise ValueError("repeatability_invalid_counts")
            expected = (
                {
                    "partial_or_better": sum(
                        count for score, count in item["score_counts"].items() if int(score) >= 1
                    ),
                    "sufficient": item["score_counts"]["2"],
                }
                if bundle["kind"] == "gold"
                else {"satisfied": item["score_counts"]["1"]}
            )
            if set(item["boundaries"]) != set(expected):
                raise ValueError("repeatability_invalid_boundary")
            for name, boundary in item["boundaries"].items():
                if (
                    boundary["denominator"] != item["valid"]
                    or boundary["rate"]
                    != (boundary["count"] / item["valid"] if item["valid"] else None)
                    or boundary["count"] != expected[name]
                ):
                    raise ValueError("repeatability_invalid_boundary")
            rows[key][snapshot["analysis_run_id"]] = item
    if not rows:
        raise ValueError("repeatability_no_distributions")
    record_ids = [record["id"] for record in first["records"]]
    rater_dimensions = sorted({(rater, dimension) for _, rater, dimension in rows})
    boundary_names = (
        ("partial_or_better", "sufficient") if first["kind"] == "gold" else ("satisfied",)
    )
    changes = boundary_changes(rows, ids, record_ids, rater_dimensions, boundary_names)
    levels = boundary_levels(rows, ids, record_ids, rater_dimensions, boundary_names)
    dimensions = sorted({dimension for _, dimension in rater_dimensions})
    model_levels = model_boundary_levels(levels, record_ids, dimensions, boundary_names)
    question_averages = question_average_levels(
        levels, model_levels, record_ids, dimensions, boundary_names
    )
    summaries = []
    for snapshot in snapshots:
        run_id = snapshot["analysis_run_id"]
        failed = {case["record_id"] for case in snapshot["bundle"]["cases"] if case["error"]}
        for rater, dimension in rater_dimensions:
            counts = {str(score): 0 for score in snapshot["analysis"]["rubric"]["scale"]}
            statuses = {status: 0 for status in ("not_applicable", "error", "pending")}
            missing = 0
            attempts = 0
            for record_id in record_ids:
                item = rows.get((record_id, rater, dimension), {}).get(run_id)
                if item is None:
                    missing += 1
                    continue
                attempts += item["attempts"]
                for score, count in item["score_counts"].items():
                    counts[score] += count
                for status, count in item["status_counts"].items():
                    statuses[status] += count
            valid = sum(counts.values())
            boundary_counts = (
                {
                    "partial_or_better": counts["1"] + counts["2"],
                    "sufficient": counts["2"],
                }
                if first["kind"] == "gold"
                else {"satisfied": counts["1"]}
            )
            summaries.append(
                dict(
                    analysis_run_id=run_id,
                    rater=rater,
                    profile_digest=profiles[rater],
                    dimension=dimension,
                    record_count=len(record_ids),
                    missing_records=missing,
                    generation_failed_records=len(failed),
                    attempts=attempts,
                    valid=valid,
                    score_counts=counts,
                    status_counts=statuses,
                    boundaries={
                        name: {
                            "count": count,
                            "denominator": valid,
                            "rate": count / valid if valid else None,
                        }
                        for name, count in boundary_counts.items()
                    },
                )
            )
    report = dict(
        schema=SCHEMA,
        repeatability_contract=CONTRACT,
        kind=first["kind"],
        dataset_id=first["dataset_id"],
        dataset_digest=first["dataset_digest"],
        qa_profile_digest=digest(first["qa_profile"]),
        rubric_digest=snapshots[0]["analysis"]["rubric_digest"],
        analyses=[
            dict(
                analysis_run_id=s["analysis_run_id"],
                source_run_id=s["bundle"]["source_run_id"],
                source_bundle_digest=s["bundle"]["digest"],
                analysis_digest=digest(s["analysis"]),
            )
            for s in snapshots
        ],
        protocol=dict(
            unit="answers_run/rater/profile/dimension",
            aggregate_rate="pooled_score_counts_over_valid_grading_observations_per_answers_run",
            round_pairing=False,
            significance_test=False,
        ),
        boundary_changes=changes,
        boundary_levels=levels,
        model_boundary_levels=model_levels,
        question_average_levels=question_averages,
        dimension_average_levels=dimension_average_levels(
            levels, model_levels, question_averages, record_ids, dimensions, boundary_names
        ),
        model_boundary_changes=model_boundary_changes(changes, dimensions, boundary_names),
        summaries=summaries,
    )
    report["digest"] = digest(report)
    return report
