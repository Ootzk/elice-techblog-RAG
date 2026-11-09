"""Dimension-specific judges with isolated inputs and explicit inapplicability."""

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
        dict(name=judge.name, version=judge.scorer_version, dimension=dimension, rater=rater)
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
    expected = record["expectations"]
    result = case["result"]
    data = {
        "inputs": record["inputs"],
        "outputs": candidate_output(result, dimension),
        "tags": {"record_id": record["id"], "dimension": dimension},
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


def evaluate_answers(settings, run_id, records, cases, specs):
    import mlflow
    from mlflow.genai.scorers import get_scorer

    experiment_id = configure(settings)
    expected = {record["id"]: record for record in records}
    valid = {case["record_id"]: case for case in cases if not case["error"]}
    judgments = {key: {"record_id": key, "judges": {}} for key in valid}
    # Validate every grounding input before any paid evaluation.
    for spec in specs:
        for key, case in valid.items():
            evaluation_row(expected[key], case, spec["dimension"])
    for dimension in dict.fromkeys(spec["dimension"] for spec in specs):
        selected = [spec for spec in specs if spec["dimension"] == dimension]
        eligible = []
        for key, case in valid.items():
            if inapplicable(expected[key], case, dimension):
                for spec in selected:
                    judgments[key]["judges"][spec["name"]] = dict(
                        value=None,
                        status="not_applicable",
                        rationale="문항/응답에 적용되지 않는 평가 차원",
                        version=spec["version"],
                        dimension=dimension,
                        rater=spec["rater"],
                    )
            else:
                eligible.append(evaluation_row(expected[key], case, dimension))
        if not eligible:
            continue
        scorers = [
            get_scorer(name=spec["name"], version=spec["version"], experiment_id=experiment_id)
            for spec in selected
        ]
        with mlflow.start_run(run_id=run_id):
            result = mlflow.genai.evaluate(data=eligible, scorers=scorers)
        rows = result.tables["eval_results"].to_dict(orient="records")
        if {row["tags"]["record_id"] for row in rows} != {
            row["tags"]["record_id"] for row in eligible
        } or len(rows) != len(eligible):
            raise RuntimeError("judge_assessment_record_mismatch")
        for row in rows:
            for spec in selected:
                raw = row.get(spec["name"] + "/value")
                if type(raw).__name__ in {"bool", "bool_"}:
                    raw = "yes" if raw else "no"
                status = {"yes": "pass", "no": "fail", "not_applicable": "not_applicable"}.get(
                    raw if isinstance(raw, str) else "", "error"
                )
                value = True if status == "pass" else False if status == "fail" else None
                judgments[row["tags"]["record_id"]]["judges"][spec["name"]] = dict(
                    value=value,
                    status=status,
                    rationale=row.get(spec["name"] + "/rationale"),
                    version=spec["version"],
                    dimension=dimension,
                    rater=spec["rater"],
                    trace_id=row["trace_id"],
                )
    rows = list(judgments.values())
    metrics = judgment_metrics(rows, specs)
    tracking = mlflow.MlflowClient()
    tracking.log_dict(run_id, {"cases": rows, "specs": specs}, "judge-results.json")
    for name, value in metrics.items():
        tracking.log_metric(run_id, name, value)
    if any(cell["status"] == "error" for row in rows for cell in row["judges"].values()):
        raise RuntimeError("judge_assessment_failed: see judge-results.json")
    return metrics, rows
