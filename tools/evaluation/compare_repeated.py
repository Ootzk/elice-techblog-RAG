"""Compare two frozen repeatability runs without invoking any model."""

import argparse
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from elice_knowledge.corpus import digest
from elice_knowledge.evaluation_schema import SCHEMA, tags
from elice_knowledge.repeated_comparison import build, metrics
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
        or run.data.tags.get("evaluation.kind") != "repeatability"
    ):
        raise ValueError("comparison_requires_finished_repeatability_runs")
    report = artifact(tracking, run_id, "repeatability-result.json", directory)
    inputs = artifact(tracking, run_id, "repeatability-inputs.json", directory)
    if (
        run.data.tags.get("repeatability_digest") != report.get("digest")
        or report.get("digest")
        != digest({k: v for k, v in report.items() if k != "digest"})
        or len(report.get("agreements", [])) != len(inputs.get("agreements", []))
    ):
        raise ValueError("comparison_repeatability_binding_mismatch")
    for expected, source in zip(
        report["agreements"], inputs["agreements"], strict=True
    ):
        if (
            expected["agreement_run_id"] != source["agreement_run_id"]
            or expected["source_bundle_digest"] != source["bundle"]["digest"]
            or expected["agreement_digest"] != digest(source["agreement"])
        ):
            raise ValueError("comparison_repeatability_source_mismatch")
    return {
        "run_id": run_id,
        "experiment_id": run.info.experiment_id,
        "report": report,
        "inputs": inputs,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-repeatability-run-id", required=True)
    parser.add_argument("--candidate-repeatability-run-id", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--log", action="store_true", help="Publish a separate comparison run"
    )
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    names = ("comparison-result.json", "comparison-inputs.json", "comparison-run.json")
    if any((args.output_dir / name).exists() for name in names):
        parser.error(
            "Use a new output directory; existing comparison files are preserved"
        )
    tracking = client(Settings())
    baseline = snapshot(tracking, args.baseline_repeatability_run_id, args.output_dir)
    candidate = snapshot(tracking, args.candidate_repeatability_run_id, args.output_dir)
    if baseline["experiment_id"] != candidate["experiment_id"]:
        raise ValueError("comparison_experiment_mismatch")
    report = build(baseline, candidate)
    inputs = {"baseline": baseline, "candidate": candidate}
    for name, value in ((names[0], report), (names[1], inputs)):
        (args.output_dir / name).write_text(
            json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
        )
    output = {
        "report": str(args.output_dir / names[0]),
        "baseline_repeatability_run_id": baseline["run_id"],
        "candidate_repeatability_run_id": candidate["run_id"],
    }
    if args.log:
        run_id = tracking.create_run(
            baseline["experiment_id"],
            tags=tags(
                "comparison",
                **{
                    "mlflow.runName": "compare-repeatability",
                    "comparison_contract": report["comparison_contract"],
                    "comparison_digest": report["digest"],
                    "baseline_repeatability_run_id": baseline["run_id"],
                    "candidate_repeatability_run_id": candidate["run_id"],
                },
            ),
        ).info.run_id
        try:
            tracking.log_dict(run_id, report, names[0])
            tracking.log_dict(run_id, inputs, names[1])
            for name, value in metrics(report).items():
                tracking.log_metric(run_id, name, value)
            tracking.set_terminated(run_id, "FINISHED")
        except Exception:
            tracking.set_terminated(run_id, "FAILED")
            raise
        output["comparison_run_id"] = run_id
        (args.output_dir / names[2]).write_text(json.dumps(output, indent=2) + "\n")
    print(json.dumps(output, ensure_ascii=False))


if __name__ == "__main__":
    main()
