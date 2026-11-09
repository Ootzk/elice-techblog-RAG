"""Register repository Gold/Iron JSONL in MLflow; no model calls or evaluation runs."""

import argparse
import hashlib
import json
from pathlib import Path

import mlflow
from elice_knowledge.qa import QARequest
from elice_knowledge.settings import Settings
from elice_knowledge.tracking import client
from mlflow.exceptions import MlflowException

ROOT = Path(__file__).resolve().parent
BEHAVIORS = {
    "deny_sensitive_information",
    "abstain_insufficient_evidence",
    "maintain_qa_scope",
    "refuse_harmful_assistance",
    "answer_grounded_ignore_injection",
    "answer_grounded",
}


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def load_records(path, kind):
    """Validate the whole file before any remote changes; preserve local IDs in tags."""
    records, ids, inputs = [], set(), set()
    for number, line in enumerate(path.read_text().splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
            source_id = row["id"]
            if (
                not isinstance(source_id, str)
                or not source_id.strip()
                or source_id in ids
            ):
                raise ValueError("missing or duplicate ID")
            request_inputs = row["inputs"]
            if not isinstance(request_inputs, dict) or set(request_inputs) - {
                "question",
                "history",
            }:
                raise ValueError("inputs must contain only question/history")
            request = QARequest(index_key="0" * 64, **request_inputs)
            normalized = {
                "question": request.question,
                "history": [message.model_dump() for message in request.history],
            }
            input_key = canonical(normalized)
            if input_key in inputs:
                raise ValueError("duplicate question/history")
            expected = row["expectations"]
            if not isinstance(expected, dict):
                raise TypeError("expectations must be an object")
            evidence = expected.get("evidence", [])
            if not isinstance(evidence, list) or any(
                not isinstance(e, dict)
                or not isinstance(e.get("url"), str)
                or not e["url"].strip()
                or not isinstance(e.get("quote"), str)
                or not e["quote"].strip()
                for e in evidence
            ):
                raise ValueError("evidence must contain URLs and quotes")
            if kind == "gold":
                if type(expected.get("answerable")) is not bool:
                    raise ValueError("gold requires boolean answerable")
                if expected["answerable"] and not evidence:
                    raise ValueError("answerable gold record requires evidence")
                text_field, criteria_fields = (
                    "answer",
                    ["required_points", "unacceptable_claims"],
                )
            else:
                if expected.get("expected_behavior") not in BEHAVIORS:
                    raise ValueError("unknown iron expected_behavior")
                text_field = "reference_response"
                criteria_fields = ["required_points", "failure_conditions"]
            if (
                not isinstance(expected.get(text_field), str)
                or not expected[text_field].strip()
            ):
                raise ValueError("reference answer is required")
            for field in criteria_fields:
                values = expected.get(field)
                if (
                    not isinstance(values, list)
                    or not values
                    or any(
                        not isinstance(value, str) or not value.strip()
                        for value in values
                    )
                ):
                    raise ValueError(f"{field} must contain nonempty strings")
            metadata = row.get("metadata", {})
            if not isinstance(metadata, dict):
                raise TypeError("metadata must be an object")
            records.append(
                {
                    "inputs": normalized,
                    "expectations": expected,
                    "tags": {
                        **{
                            key: value if isinstance(value, str) else canonical(value)
                            for key, value in metadata.items()
                        },
                        "source_id": source_id,
                    },
                }
            )
            ids.add(source_id)
            inputs.add(input_key)
        except (KeyError, TypeError, ValueError) as exc:
            # Do not echo request/credential values through validation exceptions.
            raise ValueError(f"Invalid {kind} dataset at line {number}") from exc
    if not records:
        raise ValueError(f"Empty {kind} dataset")
    return records


def register(tracking, experiment_id, name, records, tags):
    matches = tracking.search_datasets(filter_string=f"name = '{name}'", max_results=2)
    if len(matches) > 1:
        raise ValueError(
            f"Multiple MLflow datasets named {name}; resolve duplicate names"
        )
    if matches:
        dataset = tracking.get_dataset(matches[0].dataset_id)
        if experiment_id not in dataset.experiment_ids:
            tracking.add_dataset_to_experiments(dataset.dataset_id, [experiment_id])
    else:
        dataset = tracking.create_dataset(name, experiment_id=experiment_id)

    wanted = {canonical(row["inputs"]): row for row in records}
    pending = dict(wanted)
    changed, stale = [], []
    for old in dataset.records:
        key = canonical(old.inputs)
        desired = wanted.get(key)
        if desired is None:
            stale.append(old.dataset_record_id)
        elif old.expectations == desired["expectations"] and all(
            (old.tags or {}).get(k) == v for k, v in desired["tags"].items()
        ):
            pending.pop(key, None)
        else:
            # MLflow merges expectation fields; replace changed records so removed fields disappear.
            changed.append(old.dataset_record_id)
    if changed:
        dataset.delete_records(changed)
    if pending:
        dataset.merge_records(list(pending.values()))
    if stale:
        dataset.delete_records(stale)
    tracking.set_dataset_tags(dataset.dataset_id, tags)
    return {
        "name": name,
        "dataset_id": dataset.dataset_id,
        "record_count": len(records),
        "written": len(pending),
        "removed": len(stale),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", choices=["gold", "iron", "all"], default="all")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate files without contacting MLflow",
    )
    args = parser.parse_args()
    kinds = ["gold", "iron"] if args.dataset == "all" else [args.dataset]
    try:
        plans = []
        for kind in kinds:
            path = ROOT / f"{kind}_set" / f"{kind}.jsonl"
            plans.append(
                {
                    "kind": kind,
                    "name": f"elice-techblog-{kind}",
                    "records": load_records(path, kind),
                    "tags": {
                        "dataset_kind": kind,
                        "source_path": f"tools/evaluation/{kind}_set/{kind}.jsonl",
                        "source_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    },
                }
            )
        if args.dry_run:
            output = [
                {"name": p["name"], "record_count": len(p["records"]), **p["tags"]}
                for p in plans
            ]
        else:
            settings = Settings()
            if not settings.mlflow_enabled or settings.mlflow_tracking_uri is None:
                raise ValueError("Enable MLflow and set MLFLOW_TRACKING_URI")
            mlflow.set_tracking_uri(settings.mlflow_tracking_uri.get_secret_value())
            tracking = client(settings)
            experiment = tracking.get_experiment_by_name(
                settings.qa_mlflow_experiment_name
            )
            experiment_id = (
                experiment.experiment_id
                if experiment
                else tracking.create_experiment(settings.qa_mlflow_experiment_name)
            )
            output = [
                register(tracking, experiment_id, p["name"], p["records"], p["tags"])
                for p in plans
            ]
        print(
            json.dumps(
                {
                    "mode": "dry_run" if args.dry_run else "registered",
                    "datasets": output,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    except (OSError, ValueError) as exc:
        raise SystemExit(str(exc)) from None
    except MlflowException:
        raise SystemExit(
            "MLflow registration failed; check server configuration and retry"
        ) from None


if __name__ == "__main__":
    main()
