"""Freeze MLflow Dataset inputs, execute shared QA, and record scores and Judge verdicts."""

from uuid import uuid4

from psycopg.types.json import Jsonb

from elice_knowledge.corpus import digest
from elice_knowledge.evaluation_bundle import SCHEMA, create_bundle
from elice_knowledge.evaluation_schema import tags
from elice_knowledge.execution import execute
from elice_knowledge.experiment_conditions import SCHEMA as CONDITIONS_SCHEMA
from elice_knowledge.experiment_conditions import mlflow_parameters
from elice_knowledge.judge_runs import evaluate_models
from elice_knowledge.judges import frozen_judges
from elice_knowledge.performance import summarize as performance_summary
from elice_knowledge.qa import QARequest
from elice_knowledge.ragas_metrics import contract
from elice_knowledge.rubrics import load_rubric
from elice_knowledge.service_store import ServiceError
from elice_knowledge.tracking import client as tracking_client

SCORER = {
    "name": "rubric-v1",
    "ragas": contract(),
    "quality": "shared-rubric-ordinal-grading",
    "answerability": "answered-status-vs-expected",
    "version": 2,
}
SCORER_KEY = digest(SCORER)


def dataset_snapshot(settings, dataset_id):
    import mlflow

    if not settings.mlflow_enabled or not settings.mlflow_tracking_uri:
        raise ServiceError("evaluation_tracking_not_configured", 503)
    # EvaluationDataset.records lazily uses MLflow's active tracking store.
    mlflow.set_tracking_uri(settings.mlflow_tracking_uri.get_secret_value())
    dataset = tracking_client(settings).get_dataset(dataset_id)
    records = []
    for record in dataset.records:
        # Only inputs are validated as QA input. Expectations never enter QA.
        request = QARequest(index_key="0" * 64, **record.inputs)
        expected = record.expectations or {}
        iron = bool(expected.get("expected_behavior"))
        if iron and not all(expected.get(k) for k in ("required_points", "failure_conditions")):
            raise ServiceError("dataset_requires_iron_rubric", 422)
        if not iron and type(expected.get("answerable")) is not bool:
            raise ServiceError("dataset_requires_answerable_expectation", 422)
        evidence = expected.get("evidence", [])
        if (
            not isinstance(evidence, list)
            or any(
                not isinstance(e, dict) or not e.get("url") or not str(e.get("quote", "")).strip()
                for e in evidence
            )
            or (expected.get("answerable") and not evidence)
        ):
            raise ServiceError("dataset_requires_evidence_quotes", 422)
        records.append(
            {
                "id": str(record.dataset_record_id),
                "inputs": {
                    "question": request.question,
                    "history": [m.model_dump() for m in request.history],
                },
                "expectations": expected,
            }
        )
    records.sort(key=lambda item: item["id"])
    if not records:
        raise ServiceError("empty_dataset", 422)
    if len({r["id"] for r in records}) != len(records):
        raise ServiceError("duplicate_dataset_record", 422)
    kinds = {"iron" if r["expectations"].get("expected_behavior") else "gold" for r in records}
    if len(kinds) != 1:
        raise ServiceError("mixed_dataset_rubrics", 422)
    return {
        "kind": kinds.pop(),
        "dataset_id": dataset_id,
        "name": dataset.name,
        "digest": digest(records),
        "records": records,
    }


def enqueue_evaluation(store, request):
    profile = store.profile(request.qa_profile_key)
    # Before/After comparisons must never silently mix in bundled fallback prompts.
    if any(p.get("source") != "registry" for p in (profile.get("prompts") or {None: {}}).values()):
        raise ServiceError("evaluation_requires_registry_prompts", 409)
    if profile.get("experiment_conditions", {}).get("schema") != CONDITIONS_SCHEMA:
        raise ServiceError("evaluation_requires_experiment_conditions", 409)
    if request.scorer_profile_key != SCORER_KEY:
        raise ServiceError("unknown_scorer_profile", 422)
    snapshot = dataset_snapshot(store.settings, request.dataset_id)
    if request.dataset_digest != snapshot["digest"]:
        raise ServiceError("dataset_digest_changed", 409)
    records = snapshot["records"]
    if request.record_ids is not None:
        wanted = set(request.record_ids)
        if not wanted or not wanted.issubset({r["id"] for r in records}):
            raise ServiceError("invalid_record_selection", 422)
        records = [r for r in records if r["id"] in wanted]
    if len(records) > 200:
        raise ServiceError("evaluation_too_large", 422)
    kind = snapshot.get("kind", "gold")
    judges = frozen_judges(store.settings, kind) if store.settings.evaluation_judges_enabled else []
    if kind == "iron" and not judges:
        raise ServiceError("iron_requires_judges", 422)
    job_id = uuid4()
    with store.connection() as conn:
        conn.execute(
            "INSERT INTO qa_jobs(id,kind,subject_id,payload) VALUES (%s,'evaluation',%s,%s)",
            (
                job_id,
                job_id,
                Jsonb(
                    {
                        "profile_key": request.qa_profile_key,
                        "evaluation_schema": SCHEMA,
                        "scorer_key": SCORER_KEY,
                        "dataset_kind": kind,
                        "judges": judges,
                        "rubric": load_rubric(kind),
                    }
                ),
            ),
        )
        conn.execute(
            """INSERT INTO evaluation_runs(id,dataset_id,dataset_digest,profile_key,records)
            VALUES (%s,%s,%s,%s,%s)""",
            (
                job_id,
                request.dataset_id,
                request.dataset_digest,
                request.qa_profile_key,
                Jsonb(records),
            ),
        )
    return str(job_id)


def score(result, expectations):
    answerable = result["status"] == "answered"
    values = {}
    if "answerable" in expectations:
        values["answerability_correct"] = float(answerable == expectations["answerable"])
    return values


def run_evaluation(store, job):
    if job["payload"].get("evaluation_schema") != SCHEMA:
        raise ServiceError("unsupported_evaluation_schema", 409)
    with store.connection() as conn:
        evaluation = conn.execute(
            "SELECT * FROM evaluation_runs WHERE id=%s", (job["id"],)
        ).fetchone()
    tracking = tracking_client(store.settings)
    experiment = tracking.get_experiment_by_name(store.settings.qa_mlflow_experiment_name)
    exp_id = (
        experiment.experiment_id
        if experiment
        else tracking.create_experiment(store.settings.qa_mlflow_experiment_name)
    )
    run_id = tracking.create_run(
        exp_id, tags=tags("answers", **{"mlflow.runName": "dataset-qa", "activity": "evaluation"})
    ).info.run_id
    with store.connection() as conn:
        conn.execute("UPDATE evaluation_runs SET mlflow_run_id=%s WHERE id=%s", (run_id, job["id"]))
    source_finished = False
    try:
        for key, value in {
            "dataset_id": evaluation["dataset_id"],
            "dataset_digest": evaluation["dataset_digest"],
            "qa_profile_key": evaluation["profile_key"],
            "scorer_key": SCORER_KEY,
            "history_mode": "provided",
        }.items():
            tracking.log_param(run_id, key, value)
        profile = store.profile(evaluation["profile_key"])
        conditions = profile["experiment_conditions"]
        tracking.log_dict(run_id, conditions, "experiment-conditions.json")
        for name, value in mlflow_parameters(conditions).items():
            tracking.log_param(run_id, name, value)
        judge_specs = job["payload"].get("judges", [])
        tracking.log_dict(run_id, judge_specs, "judge-versions.json")
        tracking.set_tag(run_id, "dataset_kind", job["payload"].get("dataset_kind", "gold"))
        tracking.log_dict(run_id, profile, "qa-profile.json")
        for name, prompt in profile["prompts"].items():
            tracking.log_param(run_id, f"prompt.{name}.uri", prompt["uri"])
            tracking.log_param(run_id, f"prompt.{name}.digest", prompt["digest"])
            # Server-local lineage; the digest identifies the prompt across servers.
            tracking.link_prompt_version_to_run(run_id, prompt["uri"])
        cases = []
        for record in evaluation["records"]:
            store.heartbeat(job, f"evaluating:{len(cases)}/{len(evaluation['records'])}")
            output = execute(store, evaluation["profile_key"], **record["inputs"], mode="offline")
            scores = score(output["result"], record["expectations"]) if not output["error"] else {}
            case = {"record_id": record["id"], **output, "scores": scores}
            with store.connection() as conn:
                conn.execute(
                    """INSERT INTO evaluation_cases
                    (evaluation_id,record_id,run_id,status,result,scores,error)
                    VALUES (%s,%s,%s,%s,%s,%s,%s)""",
                    (
                        job["id"],
                        record["id"],
                        output["run_id"],
                        "failed" if output["error"] else "completed",
                        Jsonb(output),
                        Jsonb(scores),
                        output["error"],
                    ),
                )
            cases.append(case)
        failed = sum(bool(c["error"]) for c in cases)
        bundle = create_bundle(run_id, evaluation, profile, cases)
        tracking.log_dict(run_id, bundle, "evaluation-bundle.json")
        tracking.set_tag(run_id, "source_bundle_digest", bundle["digest"])
        summary = {
            **performance_summary(cases),
            "cases": len(cases),
            "completed": len(cases) - failed,
            "failed": failed,
            "failure_rate": failed / len(cases),
            "mlflow_run_id": run_id,
        }
        for name in {k for c in cases for k in c["scores"]}:
            values = [c["scores"][name] for c in cases if name in c["scores"]]
            summary[name] = sum(values) / len(values)
            summary[name + "_scored_cases"] = len(values)
        tracking.log_dict(run_id, {"summary": summary, "cases": cases}, "evaluation-results.json")
        for name, value in summary.items():
            if isinstance(value, (int, float)):
                tracking.log_metric(run_id, name, value)
        tracking.set_terminated(run_id, "FAILED" if failed else "FINISHED")
        source_finished = True
        summary["grading_runs"] = evaluate_models(store, bundle, judge_specs) if judge_specs else []
        with store.connection() as conn:
            conn.execute(
                "UPDATE evaluation_runs SET summary=%s WHERE id=%s", (Jsonb(summary), job["id"])
            )
        if any(row["state"] == "FAILED" for row in summary["grading_runs"]):
            raise RuntimeError("model_grading_failed")
        return summary
    except Exception:
        if not source_finished:
            tracking.set_terminated(run_id, "FAILED")
        raise
