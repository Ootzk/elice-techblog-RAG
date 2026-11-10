"""Dimension-specific judges with isolated inputs and explicit inapplicability."""

import math
import os
from numbers import Real
from pathlib import Path

RATERS = ("chatgpt", "claude", "gemini")
DIMENSIONS = {
    "gold": ("correctness", "completeness", "groundedness", "relevance"),
    "iron": ("behavior",),
    "online": ("groundedness", "relevance"),
}
KINDS = tuple(DIMENSIONS)
QUALITY_CONTRACT = "dimension-v1"
ONLINE_FILTER = (
    "metadata.environment = 'dev' AND metadata.evaluation_mode = 'online' "
    "AND metadata.quality_contract = 'dimension-v1' AND trace.status = 'OK'"
)


def instructions(dimension):
    filename = "iron" if dimension == "behavior" else dimension
    if dimension not in {d for values in DIMENSIONS.values() for d in values}:
        raise ValueError("Unknown judge dimension")
    return (Path(__file__).parent / "data" / "judges" / f"{filename}.txt").read_text()


def judge_name(kind, dimension, rater):
    return f"qa-iron-{rater}" if kind == "iron" else f"qa-{kind}-{dimension}-{rater}"


def configure(settings):
    import mlflow

    mlflow.set_tracking_uri(settings.mlflow_tracking_uri.get_secret_value())
    return mlflow.set_experiment(settings.qa_mlflow_experiment_name).experiment_id


def frozen_judges(settings, kind):
    from mlflow.genai.scorers import get_scorer

    experiment_id = configure(settings)
    return [
        dict(
            name=judge.name,
            version=judge.scorer_version,
            dimension=dimension,
            rater=rater,
            instructions=judge.instructions,
            model=judge.model,
        )
        for dimension in DIMENSIONS[kind]
        for rater in RATERS
        for judge in [
            get_scorer(name=judge_name(kind, dimension, rater), experiment_id=experiment_id)
        ]
    ]


def candidate_output(result, dimension):
    output = {key: result.get(key) for key in ("status", "answer", "reason", "citations")}
    if dimension == "groundedness":
        if "generation_sources" not in result:
            raise ValueError("generation_sources_missing: regenerate QA before judging grounding")
        output["generation_sources"] = result["generation_sources"]
    if dimension == "behavior":
        output["agent_steps"] = result.get("agent_steps", [])
    return output


def evaluation_row(record, case, dimension):
    return {
        **dimension_payload(record, case, dimension),
        "tags": {"record_id": record["id"], "dimension": dimension},
    }


def dimension_payload(record, case, dimension):
    expected = record["expectations"]
    result = case["result"]
    data = {
        "inputs": record["inputs"],
        "outputs": candidate_output(result, dimension),
    }
    if dimension == "correctness":
        data["expectations"] = {
            key: expected.get(key)
            for key in ("answerable", "answer", "unacceptable_claims", "evidence")
        }
    elif dimension == "completeness":
        data["expectations"] = {key: expected.get(key) for key in ("answerable", "required_points")}
    elif dimension == "behavior":
        data["expectations"] = expected
    return data


def inapplicable(record, case, dimension):
    expected = record["expectations"]
    if dimension == "correctness":
        return not expected.get("answerable") or case["result"]["status"] != "answered"
    if dimension == "completeness":
        return not expected.get("answerable") or not expected.get("required_points")
    return False


def evaluation_workers():
    """Use MLflow's native pools rather than multiplying them with another pool."""
    limits = {}
    for variable, default, maximum in (
        ("MLFLOW_GENAI_EVAL_MAX_WORKERS", 2, 32),
        ("MLFLOW_GENAI_EVAL_MAX_SCORER_WORKERS", 4, 16),
    ):
        value = int(os.environ.setdefault(variable, str(default)))
        if not 1 <= value <= maximum:
            raise ValueError(f"{variable} must be between 1 and {maximum}")
        limits[variable] = value
    return limits


def evaluate_answers(settings, run_id, records, cases, specs, *, raise_on_error=True):
    import mlflow

    from elice_knowledge.ragas_metrics import (
        CONTEXT_METRICS,
        PASS_THRESHOLD,
        measurement_metrics,
        payload,
        scorer,
    )

    limits = evaluation_workers()
    configure(settings)
    expected = {record["id"]: record for record in records}
    valid = {case["record_id"]: case for case in cases if not case["error"]}
    dimensions = list(specs)
    if len({s["rater"] for s in specs}) != 1:
        raise ValueError("one_rater_per_grading")
    if {s["dimension"] for s in specs} == set(DIMENSIONS["gold"]):
        dimensions += [{**specs[0], "name": d, "dimension": d} for d in CONTEXT_METRICS]
    # Validate every dimension before submitting any paid work.
    for spec in dimensions:
        for key, case in valid.items():
            payload(expected[key], case, spec["dimension"])
    scorers = [scorer(settings, spec) for spec in dimensions]
    data = [
        dict(
            inputs=expected[key]["inputs"],
            outputs=case["result"],
            expectations=expected[key]["expectations"],
            tags={"record_id": key},
        )
        for key, case in valid.items()
    ]
    # One SDK call binds every worker to the same grading run. The SDK fans out
    # records and scorers, captures per-cell failures, then joins their results.
    with mlflow.start_run(run_id=run_id):
        result = mlflow.genai.evaluate(data=data, scorers=scorers)
    evaluated = result.tables["eval_results"].to_dict(orient="records")
    if {row["tags"]["record_id"] for row in evaluated} != set(valid) or len(evaluated) != len(
        valid
    ):
        raise RuntimeError("judge_assessment_record_mismatch")
    by_id = {row["tags"]["record_id"]: row for row in evaluated}
    rows = []
    # Preserve dataset order regardless of completion order in the SDK pools.
    for key in valid:
        row = by_id[key]
        judgments = {}
        measurements = []
        for spec in dimensions:
            raw = row.get(spec["name"] + "/value")
            numeric = isinstance(raw, Real) and math.isfinite(raw) and 0 <= raw <= 1
            status = (
                "pass"
                if numeric and raw >= PASS_THRESHOLD
                else "fail"
                if numeric
                else "not_applicable"
                if raw == "not_applicable"
                else "error"
            )
            value = True if status == "pass" else False if status == "fail" else None
            measurements.append(
                dict(
                    metric=spec["dimension"],
                    score=float(raw) if numeric else None,
                    status="scored" if numeric else status,
                    rationale=row.get(spec["name"] + "/rationale") or "",
                )
            )
            if spec["dimension"] in CONTEXT_METRICS:
                continue
            judgments[spec["name"]] = dict(
                value=value,
                status=status,
                rationale=row.get(spec["name"] + "/rationale"),
                version=spec["version"],
                dimension=spec["dimension"],
                rater=spec["rater"],
                trace_id=row.get("trace_id"),
            )
        rows.append(dict(record_id=key, judges=judgments, measurements=measurements))
    metrics = measurement_metrics([m for row in rows for m in row["measurements"]])
    tracking = mlflow.MlflowClient()
    tracking.log_dict(
        run_id, {"cases": rows, "specs": specs, "execution": limits}, "judge-results.json"
    )
    # Shared grading publication owns canonical quality.* metrics. SDK-native metrics are separate.
    if raise_on_error and any(
        cell["status"] == "error" for row in rows for cell in row["measurements"]
    ):
        raise RuntimeError("judge_assessment_failed: see judge-results.json")
    return metrics, rows
