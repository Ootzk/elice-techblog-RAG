"""Portable frozen answers and expectations for rejudging and blind human review."""

from copy import deepcopy

from elice_knowledge.corpus import digest

SCHEMA = "evaluation-v1"


def create_bundle(run_id, evaluation, profile, cases):
    kind = "iron" if evaluation["records"][0]["expectations"].get("expected_behavior") else "gold"
    bundle = {
        "schema": SCHEMA,
        "source_run_id": run_id,
        "dataset_id": evaluation["dataset_id"],
        "dataset_digest": evaluation["dataset_digest"],
        "qa_profile": profile,
        "kind": kind,
        "records": evaluation["records"],
        "cases": cases,
    }
    bundle = deepcopy(bundle)
    bundle["digest"] = digest(bundle)
    return bundle


def validate_bundle(bundle):
    if bundle.get("schema") != SCHEMA:
        raise ValueError("unsupported_evaluation_schema")
    if bundle["digest"] != digest({key: value for key, value in bundle.items() if key != "digest"}):
        raise ValueError("evaluation_bundle_digest_mismatch")
    records = {row["id"] for row in bundle["records"]}
    cases = [row["record_id"] for row in bundle["cases"]]
    if (
        len(records) != len(bundle["records"])
        or len(set(cases)) != len(cases)
        or set(cases) != records
    ):
        raise ValueError("evaluation_bundle_record_mismatch")
    if bundle.get("kind") not in {"gold", "iron"}:
        raise ValueError("evaluation_bundle_rubric_mismatch")
    return bundle


def answer_digest(record, case):
    return digest({"inputs": record["inputs"], "result": case["result"]})
