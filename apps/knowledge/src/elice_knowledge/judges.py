"""Dimension-specific judges with isolated inputs and explicit inapplicability."""

import os
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


def judgment_metrics(judgments, specs):
    metrics = {}
    for spec in specs:
        name = spec["name"]
        values = [row["judges"][name]["value"] for row in judgments]
        scored = [value for value in values if value is not None]
        metrics[name + "_scored_cases"] = len(scored)
        metrics[name + "_not_applicable_cases"] = sum(
            row["judges"][name]["status"] == "not_applicable" for row in judgments
        )
        metrics[name + "_error_cases"] = sum(
            row["judges"][name]["status"] == "error" for row in judgments
        )
        if scored:
            metrics[name] = sum(scored) / len(scored)
    return metrics


def isolated_scorer(judge, spec):
    """Let MLflow parallelize dimensions while keeping each model input isolated."""
    from mlflow.entities import Feedback
    from mlflow.genai.scorers import scorer

    dimension = spec["dimension"]

    @scorer(name=spec["name"], timeout=0)
    def routed(inputs, outputs, expectations):
        record = {"inputs": inputs, "expectations": expectations}
        case = {"result": outputs}
        if inapplicable(record, case, dimension):
            return Feedback(value="not_applicable", rationale="문항/응답에 적용되지 않는 평가 차원")
        row = dimension_payload(record, case, dimension)
        # Do not forward the shared row, gold reference, or unseen retrieval candidates.
        # The pinned registered judge retains its own invocation timeout.
        return judge.run(
            inputs=row["inputs"],
            outputs=row["outputs"],
            expectations=row.get("expectations"),
        )

    return routed


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
    from mlflow.genai.scorers import get_scorer

    limits = evaluation_workers()
    experiment_id = configure(settings)
    expected = {record["id"]: record for record in records}
    valid = {case["record_id"]: case for case in cases if not case["error"]}
    # Validate every dimension before any paid evaluation or worker submission.
    for spec in specs:
        for key, case in valid.items():
            evaluation_row(expected[key], case, spec["dimension"])
    scorers = [
        isolated_scorer(
            get_scorer(name=spec["name"], version=spec["version"], experiment_id=experiment_id),
            spec,
        )
        for spec in specs
    ]
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
        for spec in specs:
            raw = row.get(spec["name"] + "/value")
            if type(raw).__name__ in {"bool", "bool_"}:
                raw = "yes" if raw else "no"
            status = {"yes": "pass", "no": "fail", "not_applicable": "not_applicable"}.get(
                raw if isinstance(raw, str) else "", "error"
            )
            value = True if status == "pass" else False if status == "fail" else None
            judgments[spec["name"]] = dict(
                value=value,
                status=status,
                rationale=row.get(spec["name"] + "/rationale"),
                version=spec["version"],
                dimension=spec["dimension"],
                rater=spec["rater"],
                trace_id=row.get("trace_id"),
            )
        rows.append(dict(record_id=key, judges=judgments))
    metrics = judgment_metrics(rows, specs)
    tracking = mlflow.MlflowClient()
    tracking.log_dict(
        run_id, {"cases": rows, "specs": specs, "execution": limits}, "judge-results.json"
    )
    # Shared grading publication owns canonical quality.* metrics. SDK-native metrics are separate.
    if raise_on_error and any(
        cell["status"] == "error" for row in rows for cell in row["judges"].values()
    ):
        raise RuntimeError("judge_assessment_failed: see judge-results.json")
    return metrics, rows
