"""Descriptive rubric-boundary stability across independent answers runs."""

from collections import defaultdict

from elice_knowledge.corpus import digest
from elice_knowledge.evaluation_bundle import validate_bundle
from elice_knowledge.evaluation_schema import SCHEMA

CONTRACT = "repeatability-v1"


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
        summaries=summaries,
    )
    report["digest"] = digest(report)
    return report
