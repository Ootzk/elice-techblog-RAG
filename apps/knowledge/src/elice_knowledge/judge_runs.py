"""Independent model rounds of shared-rubric ratings against frozen answers."""

import os
from concurrent.futures import ThreadPoolExecutor
from itertools import groupby

from elice_knowledge.evaluation_bundle import answer_digest, validate_bundle
from elice_knowledge.evaluation_schema import model_profile
from elice_knowledge.grading import Grading
from elice_knowledge.judges import DIMENSIONS, evaluate_answers
from elice_knowledge.rubrics import snapshot


def evaluate_models(store, bundle, specs, *, repeat=1, raters=None, rubric=None, diagnostics=False):
    if type(repeat) is not int or not 1 <= repeat <= 100:
        raise ValueError("repeat_must_be_between_1_and_100")
    validate_bundle(bundle)
    if not any(not c["error"] for c in bundle["cases"]):
        raise ValueError("no_gradable_answers")
    rubric = snapshot(bundle, rubric)
    if diagnostics and bundle["kind"] != "gold":
        raise ValueError("diagnostics_require_gold")
    selected = [s for s in specs if raters is None or s["rater"] in raters]
    groups = {
        rater: list(items)
        for rater, items in groupby(
            sorted(selected, key=lambda s: s["rater"]), key=lambda s: s["rater"]
        )
    }
    if not groups or (raters is not None and set(groups) != set(raters)):
        raise ValueError("unknown_model_rater")
    profiles = {
        key: model_profile(bundle, group, rubric=rubric, diagnostics=diagnostics)
        for key, group in groups.items()
    }
    grading_store = Grading(store)
    records = {r["id"]: r for r in bundle["records"]}
    cases = {c["record_id"]: c for c in bundle["cases"] if not c["error"]}

    def grade_one(rater, group):
        grading = grading_store.allocate(bundle, "model:" + rater, profiles[rater])
        remote = None
        try:
            remote = grading_store.ensure_mlflow(grading["id"])
            _, judgments = evaluate_answers(
                store.settings,
                remote,
                bundle["records"],
                bundle["cases"],
                group,
                rubric=rubric,
                diagnostics=diagnostics,
                raise_on_error=False,
            )
            by_id = {row["record_id"]: row for row in judgments}
            if set(by_id) != set(cases) or len(by_id) != len(judgments):
                raise ValueError("grading_record_mismatch")
            for key, case in cases.items():
                cells = list(by_id[key]["judges"].values())
                if (
                    len(cells) != len(DIMENSIONS[bundle["kind"]])
                    or {c["dimension"] for c in cells} != set(DIMENSIONS[bundle["kind"]])
                    or any(c["rater"] != rater for c in cells)
                ):
                    raise ValueError("grading_rater_mismatch")
                ratings = [
                    dict(
                        record_id=key,
                        answer_digest=answer_digest(records[key], case),
                        dimension=c["dimension"],
                        rater=grading["rater_id"],
                        round=str(grading["round"]),
                        protocol=grading["profile_digest"],
                        score=c["score"],
                        status=c["status"],
                        rationale=c.get("rationale") or "",
                    )
                    for c in cells
                ]
                grading_store.write_case(grading["id"], key, ratings, by_id[key]["measurements"])
            published = grading_store.publish(grading["id"])
            state, values = published["state"], published["metrics"]
        except Exception:
            import logging

            logging.getLogger(__name__).exception("Grading execution failed: %s", grading["id"])
            from elice_knowledge.ragas_metrics import DIAGNOSTICS

            with store.connection() as conn:
                existing = {
                    r["record_id"]
                    for r in conn.execute(
                        "SELECT record_id FROM grading_cases WHERE grading_id=%s",
                        (grading["id"],),
                    ).fetchall()
                }
            for key, case in cases.items():
                if key in existing:
                    continue
                ratings = [
                    dict(
                        record_id=key,
                        answer_digest=answer_digest(records[key], case),
                        dimension=d,
                        rater=grading["rater_id"],
                        round=str(grading["round"]),
                        protocol=grading["profile_digest"],
                        score=None,
                        status="error",
                        rationale="Judge evaluation failed; inspect execution trace",
                    )
                    for d in DIMENSIONS[bundle["kind"]]
                ]
                grading_store.write_case(
                    grading["id"],
                    key,
                    ratings,
                    [
                        dict(
                            metric=d,
                            score=None,
                            status="error",
                            rationale="Diagnostic execution failed",
                        )
                        for d in DIAGNOSTICS
                    ]
                    if diagnostics
                    else [],
                )
            try:
                recovered = grading_store.publish(grading["id"])
                remote, state = recovered["run_id"], recovered["state"]
            except Exception:
                with store.connection() as conn:
                    conn.execute(
                        "UPDATE grading_runs SET state='FAILED' WHERE id=%s", (grading["id"],)
                    )
                state = "FAILED"
            values = {}
        return dict(
            grading_id=str(grading["id"]),
            run_id=remote,
            rater_id=grading["rater_id"],
            round=grading["round"],
            state=state,
            metrics=values,
        )

    tasks = [(rater, group) for _ in range(repeat) for rater, group in groups.items()]
    # Each grading has its own MLflow run; nested scorers must not multiply requests.
    variables = ("MLFLOW_GENAI_EVAL_MAX_WORKERS", "MLFLOW_GENAI_EVAL_MAX_SCORER_WORKERS")
    added = []
    if len(tasks) > 1:
        for variable in variables:
            if variable not in os.environ:
                os.environ[variable] = "1"
                added.append(variable)
    try:
        with ThreadPoolExecutor(max_workers=min(len(tasks), 32)) as executor:
            futures = [executor.submit(grade_one, rater, group) for rater, group in tasks]
            return [future.result() for future in futures]
    finally:
        for variable in added:
            os.environ.pop(variable, None)
