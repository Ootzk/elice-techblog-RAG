"""Run each model evaluator independently against the same frozen answers."""

from itertools import groupby

from elice_knowledge.evaluation_bundle import answer_digest, validate_bundle
from elice_knowledge.evaluation_schema import model_profile
from elice_knowledge.grading import Grading
from elice_knowledge.judges import DIMENSIONS, evaluate_answers


def evaluate_models(store, bundle, specs, *, repeat=1, raters=None):
    if type(repeat) is not int or not 1 <= repeat <= 100:
        raise ValueError("repeat_must_be_between_1_and_100")
    validate_bundle(bundle)
    if not any(not c["error"] for c in bundle["cases"]):
        raise ValueError("no_gradable_answers")
    selected = [s for s in specs if raters is None or s["rater"] in raters]
    groups = {
        rater: list(items)
        for rater, items in groupby(
            sorted(selected, key=lambda s: s["rater"]), key=lambda s: s["rater"]
        )
    }
    if not groups or (raters is not None and set(groups) != set(raters)):
        raise ValueError("unknown_model_rater")
    # Verify all profiles before any paid request.
    profiles = {key: model_profile(bundle, group) for key, group in groups.items()}
    grading_store = Grading(store)
    records = {r["id"]: r for r in bundle["records"]}
    cases = {c["record_id"]: c for c in bundle["cases"] if not c["error"]}
    results = []
    for _ in range(repeat):
        for rater, group in groups.items():
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
                    raise_on_error=False,
                )
                by_id = {row["record_id"]: row for row in judgments}
                if set(by_id) != set(cases) or len(by_id) != len(judgments):
                    raise ValueError("grading_record_mismatch")
                rows = {}
                for key, case in cases.items():
                    cells = list(by_id[key]["judges"].values())
                    if (
                        len(cells) != len(DIMENSIONS[bundle["kind"]])
                        or {c["dimension"] for c in cells} != set(DIMENSIONS[bundle["kind"]])
                        or any(c["rater"] != rater for c in cells)
                    ):
                        raise ValueError("grading_rater_mismatch")
                    rows[key] = [
                        {
                            "record_id": key,
                            "answer_digest": answer_digest(records[key], case),
                            "dimension": c["dimension"],
                            "rater": grading["rater_id"],
                            "round": str(grading["round"]),
                            "protocol": grading["profile_digest"],
                            "label": c["status"],
                            "rationale": c.get("rationale") or "",
                        }
                        for c in cells
                    ]
                for key, labels in rows.items():
                    grading_store.write_case(grading["id"], key, labels, by_id[key]["measurements"])
                published = grading_store.publish(grading["id"])
                results.append(
                    {
                        "grading_id": str(grading["id"]),
                        "run_id": remote,
                        "rater_id": grading["rater_id"],
                        "round": grading["round"],
                        "state": published["state"],
                        "metrics": published["metrics"],
                    }
                )
            except Exception:
                # Infrastructure failures are explicit errors, not negative or N/A scores.
                existing = {r["record_id"] for r in _cases(store, grading["id"])}
                for key, case in cases.items():
                    if key not in existing:
                        labels = [
                            {
                                "record_id": key,
                                "answer_digest": answer_digest(records[key], case),
                                "dimension": dimension,
                                "rater": grading["rater_id"],
                                "round": str(grading["round"]),
                                "protocol": grading["profile_digest"],
                                "label": "error",
                                "rationale": "Judge evaluation failed; check provider setup",
                            }
                            for dimension in DIMENSIONS[bundle["kind"]]
                        ]
                        from elice_knowledge.ragas_metrics import CONTEXT_METRICS

                        dimensions = list(DIMENSIONS[bundle["kind"]]) + (
                            list(CONTEXT_METRICS) if bundle["kind"] == "gold" else []
                        )
                        grading_store.write_case(
                            grading["id"],
                            key,
                            labels,
                            [
                                dict(
                                    metric=d,
                                    score=None,
                                    status="error",
                                    rationale="Ragas grading failed",
                                )
                                for d in dimensions
                            ],
                        )
                try:
                    recovered = grading_store.publish(grading["id"])
                    remote = recovered["run_id"]
                    state = recovered["state"]
                except Exception:
                    with store.connection() as conn:
                        conn.execute(
                            "UPDATE grading_runs SET state='FAILED' WHERE id=%s", (grading["id"],)
                        )
                    state = "FAILED"
                results.append(
                    {
                        "grading_id": str(grading["id"]),
                        "run_id": remote,
                        "rater_id": grading["rater_id"],
                        "round": grading["round"],
                        "state": state,
                    }
                )
    return results


def _cases(store, grading_id):
    with store.connection() as conn:
        return conn.execute(
            "SELECT record_id FROM grading_cases WHERE grading_id=%s", (grading_id,)
        ).fetchall()
