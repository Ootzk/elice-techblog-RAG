"""Shared grading manifest, validation and neutral metrics for models and people."""

import math
import re
from numbers import Real

from elice_knowledge.corpus import digest
from elice_knowledge.evaluation_bundle import SCHEMA, answer_digest, validate_bundle
from elice_knowledge.judges import DIMENSIONS, inapplicable


def tags(kind, **values):
    return {"evaluation.schema": SCHEMA, "evaluation.kind": kind, **values}


def human_profile(bundle):
    return {"type": "human", "rubrics": bundle["rubrics"], "contract": "dimension-v1"}


def model_profile(bundle, specs):
    if (
        not specs
        or len({s["rater"] for s in specs}) != 1
        or {s["dimension"] for s in specs} != set(DIMENSIONS[bundle["kind"]])
        or len(specs) != len(DIMENSIONS[bundle["kind"]])
        or len({s["model"] for s in specs}) != 1
        or any(s["instructions"] != bundle["rubrics"][s["dimension"]] for s in specs)
    ):
        raise ValueError("rater_profile_mismatch")
    from elice_knowledge.ragas_metrics import contract

    return {
        "ragas": contract(),
        "type": "model",
        "scorers": sorted(specs, key=lambda s: s["dimension"]),
        "contract": "dimension-v1",
    }


def validate_grading(bundle, result):
    validate_bundle(bundle)
    if (
        result.get("schema") != SCHEMA
        or result.get("source_run_id") != bundle["source_run_id"]
        or (result.get("source_bundle_digest") != bundle["digest"])
    ):
        raise ValueError("grading_source_mismatch")
    kind = result.get("rater_type")
    if kind not in {"human", "model"} or not result.get("rater_id", "").startswith(kind + ":"):
        raise ValueError("grading_rater_mismatch")
    if type(result.get("round")) is not int or result["round"] < 1:
        raise ValueError("grading_round_invalid")
    if (
        type(result.get("complete")) is not bool
        or not isinstance(result.get("labels"), list)
        or not re.fullmatch(r"[a-f0-9]{64}", result.get("rater_profile_digest", ""))
    ):
        raise ValueError("grading_manifest_invalid")
    records = {r["id"]: r for r in bundle["records"]}
    cases = {c["record_id"]: c for c in bundle["cases"] if not c["error"]}
    expected = {(key, dimension) for key in cases for dimension in DIMENSIONS[bundle["kind"]]}
    seen = set()
    for row in result["labels"]:
        key = (row["record_id"], row["dimension"])
        if (
            key not in expected
            or key in seen
            or row["answer_digest"] != answer_digest(records[key[0]], cases[key[0]])
            or row["rater"] != result["rater_id"]
            or row["round"] != str(result["round"])
            or row["protocol"] != result["rater_profile_digest"]
            or row["label"] not in {"pass", "fail", "not_applicable", "error"}
        ):
            raise ValueError("grading_label_mismatch")
        seen.add(key)
        if (
            row["label"] != "error"
            and inapplicable(records[key[0]], cases[key[0]], key[1])
            and row["label"] != "not_applicable"
        ) or (key[1] == "behavior" and row["label"] == "not_applicable"):
            raise ValueError("grading_applicability_mismatch")
    if result["complete"] and seen != expected:
        raise ValueError("grading_incomplete")
    from elice_knowledge.ragas_metrics import CONTEXT_METRICS, PASS_THRESHOLD

    allowed = set(DIMENSIONS[bundle["kind"]]) | (
        set(CONTEXT_METRICS) if bundle["kind"] == "gold" else set()
    )
    measured = set()
    for cell in result.get("measurements", []):
        key = (cell.get("record_id"), cell.get("metric"))
        value = cell.get("score")
        if (
            kind != "model"
            or key[0] not in cases
            or key[1] not in allowed
            or key in measured
            or cell.get("status") not in {"scored", "not_applicable", "error"}
            or (
                cell["status"] == "scored"
                and (
                    isinstance(value, bool)
                    or not isinstance(value, Real)
                    or not math.isfinite(value)
                    or not 0 <= value <= 1
                )
            )
            or (cell["status"] != "scored" and value is not None)
        ):
            raise ValueError("grading_measurement_mismatch")
        measured.add(key)
    if kind == "model":
        if result.get("measurement_contract") != "ragas-v1":
            raise ValueError("unsupported_grading_metrics")
        if result["complete"] and measured != {
            (key, metric) for key in cases for metric in allowed
        }:
            raise ValueError("grading_measurements_incomplete")
        labels = {(r["record_id"], r["dimension"]): r["label"] for r in result["labels"]}
        for cell in result["measurements"]:
            key = (cell["record_id"], cell["metric"])
            if key in labels:
                expected_label = (
                    ("pass" if cell["score"] >= PASS_THRESHOLD else "fail")
                    if cell["status"] == "scored"
                    else cell["status"]
                )
                if labels[key] != expected_label:
                    raise ValueError("grading_score_label_mismatch")
    return result


def grading_result(bundle, grading, labels, measurements=()):
    expected = sum(not c["error"] for c in bundle["cases"]) * len(DIMENSIONS[bundle["kind"]])
    result = {
        "schema": SCHEMA,
        "source_run_id": bundle["source_run_id"],
        "source_bundle_digest": bundle["digest"],
        "rater_type": grading["rater_type"],
        "rater_id": grading["rater_id"],
        "rater_profile_digest": grading["profile_digest"],
        "round": grading["round"],
        "complete": len(labels) == expected,
        "labels": labels,
        "measurements": list(measurements),
    }
    if grading["rater_type"] == "model":
        result["measurement_contract"] = "ragas-v1"
    return validate_grading(bundle, result)


def metrics(bundle, result):
    validate_grading(bundle, result)
    count = sum(not c["error"] for c in bundle["cases"])
    values = {"source_failed_cases": len(bundle["cases"]) - count}
    for dimension in DIMENSIONS[bundle["kind"]]:
        statuses = [r["label"] for r in result["labels"] if r["dimension"] == dimension]
        scored = statuses.count("pass") + statuses.count("fail")
        prefix = "quality." + dimension + "."
        values.update(
            {
                prefix + "scored_cases": scored,
                prefix + "not_applicable_cases": statuses.count("not_applicable"),
                prefix + "error_cases": statuses.count("error"),
                prefix + "pending_cases": count - len(statuses),
            }
        )
        if scored:
            values[prefix + "pass_rate"] = statuses.count("pass") / scored
    return values


def profile_digest(profile):
    return digest(profile)
