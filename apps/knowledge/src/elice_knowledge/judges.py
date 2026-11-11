"""Dimension-specific judges with isolated inputs and explicit inapplicability."""

import math
import os
import time
from numbers import Real

from elice_knowledge.rubrics import DIMENSIONS, load_rubric

RATERS = ("chatgpt", "claude", "gemini")

KINDS = tuple(DIMENSIONS)
QUALITY_CONTRACT = "rubric-v1"
ONLINE_FILTER = (
    "metadata.environment = 'dev' AND metadata.evaluation_mode = 'online' "
    "AND metadata.quality_contract = 'rubric-v1' AND trace.status = 'OK'"
)


def instructions(dimension):
    kind = "iron" if dimension == "behavior" else "gold"
    if dimension not in DIMENSIONS[kind]:
        raise ValueError("Unknown judge dimension")
    return offline_instructions(kind, dimension)


def offline_instructions(kind, dimension, rubric=None):
    from elice_knowledge.rubrics import criterion

    rubric = rubric or load_rubric(kind)
    text = criterion(rubric, dimension) + "\n질문: {{ inputs }}\n답변과 허용 문맥: {{ outputs }}"
    if dimension in {"correctness", "completeness", "behavior"}:
        text += "\n문항별 기대 기준: {{ expectations }}"
    return text


def judge_name(kind, dimension, rater, rubric_id=None):
    if kind == "online":
        return f"qa-online-{dimension}-{rater}"
    identity = rubric_id or load_rubric(kind)["id"]
    return f"qa-{identity}-{dimension}-{rater}"


def configure(settings):
    import mlflow

    mlflow.set_tracking_uri(settings.mlflow_tracking_uri.get_secret_value())
    return mlflow.set_experiment(settings.qa_mlflow_experiment_name).experiment_id


def frozen_judges(settings, kind, *, rubric=None):
    from mlflow.genai.scorers import get_scorer

    experiment_id = configure(settings)
    rubric = rubric or load_rubric(kind)
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
            get_scorer(
                name=judge_name(kind, dimension, rater, rubric["id"]), experiment_id=experiment_id
            )
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


def evaluate_answers(
    settings, run_id, records, cases, specs, *, rubric=None, diagnostics=False, raise_on_error=True
):
    import mlflow

    from elice_knowledge.judge_efficiency import Collector
    from elice_knowledge.judge_efficiency import metrics as efficiency_metrics
    from elice_knowledge.ragas_metrics import DIAGNOSTICS, measurement_metrics, payload, scorer

    rubric = rubric or load_rubric(
        "iron" if set(s["dimension"] for s in specs) == {"behavior"} else "gold"
    )
    limits = evaluation_workers()
    configure(settings)
    expected = {record["id"]: record for record in records}
    valid = {case["record_id"]: case for case in cases if not case["error"]}
    dimensions = list(specs)
    if len({s["rater"] for s in specs}) != 1:
        raise ValueError("one_rater_per_grading")
    if diagnostics:
        if rubric["kind"] != "gold":
            raise ValueError("diagnostics_require_gold")
        dimensions += [{**specs[0], "name": d, "dimension": d} for d in DIAGNOSTICS]
    for spec in dimensions:
        for key, case in valid.items():
            payload(expected[key], case, spec["dimension"])
    collector = Collector(records, valid)
    scorers = [scorer(settings, spec, rubric=rubric, collector=collector) for spec in dimensions]
    data = [
        dict(
            inputs=expected[key]["inputs"],
            outputs=case["result"],
            expectations=expected[key]["expectations"],
            tags={"record_id": key},
        )
        for key, case in valid.items()
    ]
    started = time.monotonic()
    try:
        with mlflow.start_run(run_id=run_id):
            result = mlflow.genai.evaluate(data=data, scorers=scorers)
    except Exception:
        mlflow.MlflowClient().log_dict(
            run_id,
            collector.artifact(run_id=run_id, elapsed_ms=(time.monotonic() - started) * 1000),
            "judge-efficiency.json",
        )
        raise
    efficiency = collector.artifact(run_id=run_id, elapsed_ms=(time.monotonic() - started) * 1000)
    evaluated = result.tables["eval_results"].to_dict(orient="records")
    if {row["tags"]["record_id"] for row in evaluated} != set(valid) or len(evaluated) != len(
        valid
    ):
        raise RuntimeError("judge_assessment_record_mismatch")
    by_id = {row["tags"]["record_id"]: row for row in evaluated}
    rows = []
    for key in valid:
        row, judgments, measurements = by_id[key], {}, []
        for spec in dimensions:
            raw = row.get(spec["name"] + "/value")
            diagnostic = spec["dimension"] in DIAGNOSTICS
            numeric = (
                isinstance(raw, Real)
                and not isinstance(raw, bool)
                and math.isfinite(raw)
                and (0 <= raw <= 1 if diagnostic else raw in rubric["scale"] and int(raw) == raw)
            )
            status = (
                "scored" if numeric else "not_applicable" if raw == "not_applicable" else "error"
            )
            score = (float(raw) if diagnostic else int(raw)) if numeric else None
            reason = row.get(spec["name"] + "/rationale")
            if not isinstance(reason, str):
                reason = ""
            if diagnostic:
                measurements.append(
                    dict(metric=spec["dimension"], score=score, status=status, rationale=reason)
                )
            else:
                judgments[spec["name"]] = dict(
                    score=score,
                    status=status,
                    rationale=reason,
                    version=spec["version"],
                    dimension=spec["dimension"],
                    rater=spec["rater"],
                    trace_id=row.get("trace_id"),
                )
        rows.append(dict(record_id=key, judges=judgments, measurements=measurements))
    metrics = measurement_metrics([m for row in rows for m in row["measurements"]])
    mlflow.MlflowClient().log_dict(
        run_id,
        {"cases": rows, "specs": specs, "rubric": rubric, "execution": limits},
        "judge-results.json",
    )
    mlflow.MlflowClient().log_dict(run_id, efficiency, "judge-efficiency.json")
    for name, value in efficiency_metrics(efficiency).items():
        mlflow.MlflowClient().log_metric(run_id, name, value)
    if raise_on_error and any(
        cell["status"] == "error"
        for row in rows
        for cell in [*row["judges"].values(), *row["measurements"]]
    ):
        raise RuntimeError("judge_assessment_failed: see judge-results.json")
    return metrics, rows
