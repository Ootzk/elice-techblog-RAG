"""Registered judge definitions reuse centrally configured Gateway credentials."""

from pathlib import Path

RATERS = ("chatgpt", "claude", "gemini")
KINDS = ("gold", "iron", "online")
ONLINE_FILTER = "metadata.environment = 'dev' AND metadata.evaluation_mode = 'online'"


def instructions(kind):
    if kind not in KINDS:
        raise ValueError("Unknown judge kind")
    return (Path(__file__).parent / "data" / "judges" / f"{kind}.txt").read_text()


def configure(settings):
    import mlflow

    mlflow.set_tracking_uri(settings.mlflow_tracking_uri.get_secret_value())
    return mlflow.set_experiment(settings.qa_mlflow_experiment_name).experiment_id


def frozen_judges(settings, kind):
    from mlflow.genai.scorers import get_scorer

    experiment_id = configure(settings)
    return [
        {"name": judge.name, "version": judge.scorer_version}
        for rater in RATERS
        for judge in [get_scorer(name=f"qa-{kind}-{rater}", experiment_id=experiment_id)]
    ]


def evaluate_answers(settings, run_id, records, cases, specs):
    import mlflow
    from mlflow.genai.scorers import get_scorer

    experiment_id = configure(settings)
    scorers = [get_scorer(**spec, experiment_id=experiment_id) for spec in specs]
    expected = {record["id"]: record for record in records}
    valid = [case for case in cases if not case["error"]]
    if not valid:
        return {}, []
    data = [
        {
            "inputs": expected[case["record_id"]]["inputs"],
            "outputs": case["result"],
            "expectations": expected[case["record_id"]]["expectations"],
            "tags": {"record_id": case["record_id"]},
        }
        for case in valid
    ]
    with mlflow.start_run(run_id=run_id):
        result = mlflow.genai.evaluate(data=data, scorers=scorers)
    table = result.tables["eval_results"].to_dict(orient="records")
    # Missing/error judgments are failures, never false verdicts or omitted successes.
    for row in table:
        for spec in specs:
            value = row.get(spec["name"] + "/value")
            if value is None or type(value).__name__ not in {"bool", "bool_"}:
                raise RuntimeError("judge_assessment_failed")
    if len(table) != len(valid):
        raise RuntimeError("judge_assessment_count_mismatch")
    judgments = []
    for row in table:
        judgments.append(
            {
                "record_id": row["tags"]["record_id"],
                "trace_id": row["trace_id"],
                "judges": {
                    spec["name"]: {
                        "value": bool(row[spec["name"] + "/value"]),
                        "rationale": row.get(spec["name"] + "/rationale"),
                        "version": spec["version"],
                    }
                    for spec in specs
                },
            }
        )
    mlflow.MlflowClient().log_dict(run_id, {"cases": judgments}, "judge-results.json")
    return result.metrics, judgments
