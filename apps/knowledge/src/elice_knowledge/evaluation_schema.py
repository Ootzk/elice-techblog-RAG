"""Shared rubric-based grading with explicit ordinal scores and diagnostic measurements."""

import math
import re
from numbers import Real

from elice_knowledge.corpus import digest
from elice_knowledge.evaluation_bundle import SCHEMA as ANSWERS_SCHEMA
from elice_knowledge.evaluation_bundle import answer_digest, validate_bundle
from elice_knowledge.judges import DIMENSIONS, inapplicable, offline_instructions
from elice_knowledge.rubrics import CONTRACT, snapshot, validate_rubric

SCHEMA = "evaluation-v2"


def tags(kind, **values):
    return {
        "evaluation.schema": ANSWERS_SCHEMA if kind == "answers" else SCHEMA,
        "evaluation.kind": kind,
        **values,
    }


def human_profile(bundle, rubric=None):
    rubric = snapshot(bundle, rubric)
    return {
        "type": "human",
        "rubric": rubric,
        "rubric_digest": digest(rubric),
        "contract": CONTRACT,
    }


def model_profile(bundle, specs, *, rubric=None, diagnostics=False):
    rubric = snapshot(bundle, rubric)
    if (
        not specs
        or len({s["rater"] for s in specs}) != 1
        or {s["dimension"] for s in specs} != set(DIMENSIONS[bundle["kind"]])
        or len(specs) != len(DIMENSIONS[bundle["kind"]])
        or len({s["model"] for s in specs}) != 1
        or any(
            s["instructions"] != offline_instructions(bundle["kind"], s["dimension"], rubric)
            for s in specs
        )
    ):
        raise ValueError("rater_profile_mismatch")
    from elice_knowledge.ragas_metrics import contract

    return {
        "ragas": contract(),
        "type": "model",
        "rubric": rubric,
        "rubric_digest": digest(rubric),
        "diagnostics": diagnostics,
        "scorers": sorted(specs, key=lambda s: s["dimension"]),
        "contract": CONTRACT,
    }


def validate_rating(rating, scale, *, draft=False):
    status, score = rating.get("status"), rating.get("score")
    if draft and status == "pending" and score is None:
        return
    if status not in {"scored", "not_applicable", "error"}:
        raise ValueError("invalid_rating_status")
    if (status == "scored" and (type(score) is not int or score not in scale)) or (
        status != "scored" and score is not None
    ):
        raise ValueError("invalid_rating_score")


def validate_grading(bundle, result):
    validate_bundle(bundle)
    if (
        result.get("schema") != SCHEMA
        or result.get("contract") != CONTRACT
        or result.get("source_run_id") != bundle["source_run_id"]
        or result.get("source_bundle_digest") != bundle["digest"]
    ):
        raise ValueError("grading_source_mismatch")
    rubric = validate_rubric(result.get("rubric", {}), bundle["kind"])
    if result.get("rubric_digest") != digest(rubric):
        raise ValueError("grading_rubric_mismatch")
    kind = result.get("rater_type")
    if kind not in {"human", "model"} or not result.get("rater_id", "").startswith(kind + ":"):
        raise ValueError("grading_rater_mismatch")
    if type(result.get("round")) is not int or result["round"] < 1:
        raise ValueError("grading_round_invalid")
    if (
        type(result.get("complete")) is not bool
        or not isinstance(result.get("ratings"), list)
        or not re.fullmatch(r"[a-f0-9]{64}", result.get("rater_profile_digest", ""))
    ):
        raise ValueError("grading_manifest_invalid")
    records = {r["id"]: r for r in bundle["records"]}
    cases = {c["record_id"]: c for c in bundle["cases"] if not c["error"]}
    expected = {(key, d) for key in cases for d in DIMENSIONS[bundle["kind"]]}
    seen = set()
    for row in result["ratings"]:
        key = (row["record_id"], row["dimension"])
        validate_rating(row, rubric["scale"])
        if (
            key not in expected
            or key in seen
            or row["answer_digest"] != answer_digest(records[key[0]], cases[key[0]])
            or row["rater"] != result["rater_id"]
            or row["round"] != str(result["round"])
            or row["protocol"] != result["rater_profile_digest"]
        ):
            raise ValueError("grading_rating_mismatch")
        seen.add(key)
        if (
            row["status"] != "error"
            and inapplicable(records[key[0]], cases[key[0]], key[1], rubric=rubric)
            and row["status"] != "not_applicable"
        ) or (key[1] in {"behavior", "relevance"} and row["status"] == "not_applicable"):
            raise ValueError("grading_applicability_mismatch")
    if result["complete"] and seen != expected:
        raise ValueError("grading_incomplete")
    from elice_knowledge.ragas_metrics import DIAGNOSTICS

    measured = set()
    for cell in result.get("measurements", []):
        key, value = (cell.get("record_id"), cell.get("metric")), cell.get("score")
        if (
            kind != "model"
            or key[0] not in cases
            or key[1] not in DIAGNOSTICS
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
    if result.get("measurement_contract") != "ragas-diagnostics-v1":
        raise ValueError("unsupported_grading_metrics")
    if (
        result["complete"]
        and result.get("diagnostics")
        and measured != {(key, metric) for key in cases for metric in DIAGNOSTICS}
    ):
        raise ValueError("grading_measurements_incomplete")
    if not result.get("diagnostics") and measured:
        raise ValueError("unexpected_diagnostics")
    return result


def grading_result(bundle, grading, ratings, measurements=()):
    profile = grading["profile"]
    expected = sum(not c["error"] for c in bundle["cases"]) * len(DIMENSIONS[bundle["kind"]])
    return validate_grading(
        bundle,
        {
            "schema": SCHEMA,
            "contract": CONTRACT,
            "source_run_id": bundle["source_run_id"],
            "source_bundle_digest": bundle["digest"],
            "rater_type": grading["rater_type"],
            "rater_id": grading["rater_id"],
            "rater_profile_digest": grading["profile_digest"],
            "round": grading["round"],
            "rubric": profile["rubric"],
            "rubric_digest": profile["rubric_digest"],
            "complete": len(ratings) == expected,
            "ratings": ratings,
            "diagnostics": profile.get("diagnostics", False),
            "measurement_contract": "ragas-diagnostics-v1",
            "measurements": list(measurements),
        },
    )


def metrics(bundle, result):
    validate_grading(bundle, result)
    count = sum(not c["error"] for c in bundle["cases"])
    values = {"source_failed_cases": len(bundle["cases"]) - count}
    for dimension in DIMENSIONS[bundle["kind"]]:
        rows = [r for r in result["ratings"] if r["dimension"] == dimension]
        scores = [r["score"] for r in rows if r["status"] == "scored"]
        prefix = "rubric." + dimension + "."
        values.update(
            {
                prefix + "scored_cases": len(scores),
                prefix + "not_applicable_cases": sum(r["status"] == "not_applicable" for r in rows),
                prefix + "error_cases": sum(r["status"] == "error" for r in rows),
                prefix + "pending_cases": count - len(rows),
            }
        )
        for score in result["rubric"]["scale"]:
            values[prefix + f"score_{score}_count"] = scores.count(score)
        if scores:
            values[prefix + "mean"] = sum(scores) / len(scores)
    return values


def profile_digest(profile):
    return digest(profile)
