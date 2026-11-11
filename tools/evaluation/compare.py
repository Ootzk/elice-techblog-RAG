"""Compare two frozen agreement runs locally; --log publishes a separate comparison run."""

import argparse
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from elice_knowledge.comparison import compare, comparison_metrics
from elice_knowledge.corpus import digest
from elice_knowledge.evaluation_bundle import SCHEMA as ANSWERS_SCHEMA
from elice_knowledge.evaluation_schema import SCHEMA, tags
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
        raise ValueError("comparison_requires_finished_agreement_runs")
    agreement = artifact(tracking, run_id, "agreement.json", directory)
    source = agreement["source_run_id"]
    original = tracking.get_run(source)
    if (
        original.data.tags.get("evaluation.kind") != "answers"
        or original.data.tags.get("evaluation.schema") != ANSWERS_SCHEMA
        or original.info.status not in {"FINISHED", "FAILED"}
        or run.data.tags.get("source_run_id") != source
    ):
        raise ValueError("comparison_agreement_lineage_mismatch")
    bundle = artifact(tracking, source, "evaluation-bundle.json", directory)
    if run.data.tags.get("source_bundle_digest") != bundle["digest"]:
        raise ValueError("comparison_agreement_digest_mismatch")
    gradings = artifact(tracking, run_id, "grading-inputs.json", directory)["results"]
    profiles = {}
    expected_profiles = {g["rater_profile_digest"] for g in gradings}
    for grading_id in agreement.get("grading_run_ids", []):
        grade = tracking.get_run(grading_id)
        if (
            grade.data.tags.get("evaluation.kind") != "grading"
            or grade.data.tags.get("source_run_id") != source
            or grade.data.tags.get("source_bundle_digest") != bundle["digest"]
        ):
            raise ValueError("comparison_grading_lineage_mismatch")
        profile = artifact(tracking, grading_id, "rater-profile.json", directory)
        key = digest(profile)
        if (
            key not in expected_profiles
            or grade.data.tags.get("rater_profile_digest") != key
        ):
            raise ValueError("comparison_grading_profile_mismatch")
        profiles[key] = profile
    return {
        "agreement_run_id": run_id,
        "experiment_id": run.info.experiment_id,
        "agreement": agreement,
        "bundle": bundle,
        "gradings": gradings,
        "profiles": profiles,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-agreement-run-id", required=True)
    parser.add_argument("--candidate-agreement-run-id", required=True)
    parser.add_argument("--purpose", choices=("system", "judge"), required=True)
    parser.add_argument("--hypothesis", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--bootstrap-resamples", type=int, default=2000)
    parser.add_argument("--confidence-level", type=float, default=0.95)
    parser.add_argument(
        "--log",
        action="store_true",
        help="Publish a comparison run; never calls a model",
    )
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    filenames = (
        "comparison-result.json",
        "comparison-inputs.json",
        "comparison-run.json",
    )
    if any((args.output_dir / name).exists() for name in filenames):
        parser.error(
            "Use a new output directory; existing comparison files are preserved"
        )
    tracking = client(Settings())
    baseline = snapshot(tracking, args.baseline_agreement_run_id, args.output_dir)
    candidate = snapshot(tracking, args.candidate_agreement_run_id, args.output_dir)
    report = compare(
        baseline,
        candidate,
        purpose=args.purpose,
        hypothesis=args.hypothesis,
        seed=args.seed,
        resamples=args.bootstrap_resamples,
        confidence=args.confidence_level,
    )
    inputs = {"baseline": baseline, "candidate": candidate}
    for name, data in ((filenames[0], report), (filenames[1], inputs)):
        (args.output_dir / name).write_text(
            json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
        )
    output = {
        "report": str(args.output_dir / filenames[0]),
        "purpose": args.purpose,
        "shared_records": len(report["coverage"]["shared_record_ids"]),
    }
    if args.log:
        run_id = tracking.create_run(
            baseline["experiment_id"],
            tags=tags(
                "comparison",
                **{
                    "mlflow.runName": f"compare-{args.purpose}-{args.baseline_agreement_run_id[:8]}-vs-{args.candidate_agreement_run_id[:8]}",
                    "comparison_contract": report["comparison_contract"],
                    "purpose": args.purpose,
                    "baseline_agreement_run_id": args.baseline_agreement_run_id,
                    "candidate_agreement_run_id": args.candidate_agreement_run_id,
                    "comparison_digest": report["digest"],
                },
            ),
        ).info.run_id
        try:
            tracking.log_dict(run_id, report, filenames[0])
            tracking.log_dict(run_id, inputs, filenames[1])
            for name, value in comparison_metrics(report).items():
                tracking.log_metric(run_id, name, value)
            for name, value in {
                "purpose": args.purpose,
                "hypothesis": args.hypothesis,
                "seed": args.seed,
                "bootstrap_resamples": args.bootstrap_resamples,
                "confidence_level": args.confidence_level,
            }.items():
                tracking.log_param(run_id, name, value)
            tracking.set_terminated(run_id, "FINISHED")
        except Exception:
            tracking.set_terminated(run_id, "FAILED")
            raise
        output["comparison_run_id"] = run_id
        (args.output_dir / filenames[2]).write_text(
            json.dumps(output, ensure_ascii=False, indent=2) + "\n"
        )
    print(json.dumps(output, ensure_ascii=False))


if __name__ == "__main__":
    main()
