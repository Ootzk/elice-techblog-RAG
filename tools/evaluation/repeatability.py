"""Compare completed agreement runs from the same condition without model calls."""

import argparse
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from elice_knowledge.evaluation_bundle import SCHEMA as ANSWERS_SCHEMA
from elice_knowledge.evaluation_bundle import validate_bundle
from elice_knowledge.evaluation_schema import SCHEMA, tags
from elice_knowledge.repeatability import build
from elice_knowledge.settings import Settings
from elice_knowledge.tracking import client


def artifact(tracking, run_id, filename, directory):
    with TemporaryDirectory(dir=directory, prefix=".mlflow-") as scratch:
        path = tracking.download_artifacts(run_id, filename, scratch)
        return json.loads(Path(path).read_text())


def snapshot(tracking, run_id, directory):
    run = tracking.get_run(run_id)
    if (
        run.info.status != "FINISHED"
        or run.data.tags.get("evaluation.schema") != SCHEMA
        or run.data.tags.get("evaluation.kind") != "agreement"
    ):
        raise ValueError("repeatability_requires_finished_agreement")
    agreement = artifact(tracking, run_id, "agreement.json", directory)
    source_id = agreement["source_run_id"]
    source = tracking.get_run(source_id)
    bundle = validate_bundle(
        artifact(tracking, source_id, "evaluation-bundle.json", directory)
    )
    if (
        source.data.tags.get("evaluation.schema") != ANSWERS_SCHEMA
        or source.data.tags.get("evaluation.kind") != "answers"
        or source.info.status not in {"FINISHED", "FAILED"}
        or (
            source.info.status == "FAILED"
            and not any(case["error"] for case in bundle["cases"])
        )
        or run.data.tags.get("source_run_id") != source_id
        or run.data.tags.get("source_bundle_digest") != bundle.get("digest")
    ):
        raise ValueError("repeatability_agreement_lineage_mismatch")
    grading_runs, excluded_grading_runs = [], []
    for grading_id in agreement.get("grading_run_ids", []):
        grading = tracking.get_run(grading_id)
        grading_tags = grading.data.tags
        if (
            grading.info.status != "FINISHED"
            or grading_tags.get("evaluation.schema") != SCHEMA
            or grading_tags.get("evaluation.kind") != "grading"
            or grading_tags.get("source_run_id") != source_id
            or grading_tags.get("source_bundle_digest") != bundle["digest"]
            or not grading_tags.get("rater_id")
            or not grading_tags.get("rater_profile_digest")
            or not grading_tags.get("round")
        ):
            raise ValueError("repeatability_grading_lineage_mismatch")
        entry = {
            "run_id": grading_id,
            "rater": grading_tags["rater_id"],
            "profile_digest": grading_tags["rater_profile_digest"],
            "round": grading_tags["round"],
        }
        (excluded_grading_runs if entry["rater"].startswith("baseline:") else grading_runs).append(
            entry
        )
    return {
        "agreement_run_id": run_id,
        "bundle": bundle,
        "agreement": agreement,
        "grading_runs": grading_runs,
        "excluded_grading_runs": excluded_grading_runs,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agreement-run-id", action="append", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--log", action="store_true", help="Publish a separate repeatability run"
    )
    args = parser.parse_args()
    if len(args.agreement_run_id) < 2:
        parser.error("Provide at least two --agreement-run-id values")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    names = (
        "repeatability-result.json",
        "repeatability-inputs.json",
        "repeatability-run.json",
    )
    if any((args.output_dir / name).exists() for name in names):
        parser.error(
            "Use a new output directory; existing repeatability files are preserved"
        )
    tracking = client(Settings())
    snapshots = [
        snapshot(tracking, rid, args.output_dir) for rid in args.agreement_run_id
    ]
    report = build(snapshots)
    inputs = {"agreements": snapshots}
    for filename, value in ((names[0], report), (names[1], inputs)):
        (args.output_dir / filename).write_text(
            json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
        )
    output = {"report": str(args.output_dir / names[0]), "agreements": len(snapshots)}
    if args.log:
        experiment_ids = {
            tracking.get_run(rid).info.experiment_id for rid in args.agreement_run_id
        }
        if len(experiment_ids) != 1:
            raise ValueError("repeatability_agreement_experiment_mismatch")
        run_id = tracking.create_run(
            experiment_ids.pop(),
            tags=tags(
                "repeatability",
                **{
                    "mlflow.runName": "answer-repeatability",
                    "repeatability_contract": report["repeatability_contract"],
                    "repeatability_digest": report["digest"],
                },
            ),
        ).info.run_id
        try:
            tracking.log_dict(run_id, report, names[0])
            tracking.log_dict(run_id, inputs, names[1])
            tracking.log_param(
                run_id, "agreement_run_ids", json.dumps(args.agreement_run_id)
            )
            tracking.log_param(run_id, "dataset_digest", report["dataset_digest"])
            tracking.log_param(run_id, "qa_profile_digest", report["qa_profile_digest"])
            tracking.log_metric(run_id, "repeatability.agreements", len(snapshots))
            tracking.log_metric(
                run_id, "repeatability.summary_rows", len(report["summaries"])
            )
            for item in report["boundary_changes"]["overall"]:
                boundary = item["boundary"]
                if item["mean_absolute_change"] is not None:
                    tracking.log_metric(
                        run_id,
                        f"repeatability.{boundary}.mean_absolute_change",
                        item["mean_absolute_change"],
                    )
                tracking.log_metric(
                    run_id,
                    f"repeatability.{boundary}.observed_pairs",
                    item["observed_pairs"],
                )
                tracking.log_metric(
                    run_id,
                    f"repeatability.{boundary}.possible_pairs",
                    item["possible_pairs"],
                )
            tracking.set_terminated(run_id, "FINISHED")
        except Exception:
            tracking.set_terminated(run_id, "FAILED")
            raise
        output["repeatability_run_id"] = run_id
        (args.output_dir / names[2]).write_text(json.dumps(output, indent=2) + "\n")
    print(json.dumps(output, ensure_ascii=False))


if __name__ == "__main__":
    main()
