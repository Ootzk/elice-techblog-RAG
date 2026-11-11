"""Rejudge frozen answers, export blind packets, and compare human/model labels."""

import argparse
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from elice_knowledge.agreement import build_agreement, review_packet
from elice_knowledge.evaluation_bundle import validate_bundle
from elice_knowledge.evaluation_schema import SCHEMA, tags, validate_grading
from elice_knowledge.judge_runs import evaluate_models
from elice_knowledge.judges import configure, frozen_judges
from elice_knowledge.reliability import report_metrics
from elice_knowledge.rubrics import load_rubric
from elice_knowledge.service_store import ServiceStore
from elice_knowledge.settings import Settings
from elice_knowledge.tracking import client


def read_artifact(tracking, run_id, filename, directory):
    # Never leave unblinded source outputs beside the human packet.
    with TemporaryDirectory(dir=directory, prefix=".mlflow-") as scratch:
        path = tracking.download_artifacts(run_id, filename, scratch)
        return json.loads(Path(path).read_text())


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("export", "judge", "agreement"))
    parser.add_argument("--source-run-id", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--grading-run-id",
        action="append",
        default=[],
        help="Select grading runs; use one round per subject for panel coefficients",
    )
    parser.add_argument("--model-rater", action="append")
    parser.add_argument("--grading-file", action="append", type=Path, default=[])
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--submit", action="store_true")
    parser.add_argument("--rubric-id")
    parser.add_argument(
        "--diagnostics",
        action="store_true",
        help="Also run separate numeric Ragas diagnostics",
    )
    parser.add_argument(
        "--log",
        action="store_true",
        help="Log the agreement report and reliability metrics to MLflow",
    )
    args = parser.parse_args()
    if not 1 <= args.repeat <= 100:
        parser.error("--repeat must be between 1 and 100")
    settings = Settings()
    experiment = configure(settings)
    tracking = client(settings)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    bundle = validate_bundle(
        read_artifact(
            tracking, args.source_run_id, "evaluation-bundle.json", args.output_dir
        )
    )
    if bundle["source_run_id"] != args.source_run_id:
        raise ValueError("source_run_mismatch")
    if args.action == "export":
        packet, _ = review_packet(
            bundle, "preview", load_rubric(bundle["kind"], args.rubric_id)
        )
        path = args.output_dir / "review-packet.json"
        if path.exists():
            raise FileExistsError(
                "Use a new output directory; existing packet is preserved"
            )
        write_json(
            path,
            {
                "schema": bundle["schema"],
                "source_run_id": args.source_run_id,
                "source_bundle_digest": bundle["digest"],
                "items": packet,
            },
        )
        print(json.dumps({"packet": str(path)}))
    elif args.action == "judge":
        rubric = load_rubric(bundle["kind"], args.rubric_id)
        specs = frozen_judges(settings, bundle["kind"], rubric=rubric)
        if args.model_rater:
            specs = [s for s in specs if s["rater"] in args.model_rater]
        if not args.submit:
            print(
                json.dumps(
                    {
                        "mode": "preview",
                        "source_digest": bundle["digest"],
                        "successful_answers": sum(
                            not c["error"] for c in bundle["cases"]
                        ),
                        "judges": specs,
                        "repeat": args.repeat,
                        "rubric_id": rubric["id"],
                        "diagnostics": args.diagnostics,
                        "planned_grading_runs": args.repeat
                        * len({s["rater"] for s in specs}),
                    },
                    indent=2,
                )
            )
            return
        results = evaluate_models(
            ServiceStore(settings),
            bundle,
            specs,
            repeat=args.repeat,
            raters=args.model_rater,
            rubric=rubric,
            diagnostics=args.diagnostics,
        )
        for result in results:
            write_json(args.output_dir / (result["grading_id"] + ".json"), result)
            print(json.dumps(result), flush=True)
        if any(row["state"] == "FAILED" for row in results):
            raise SystemExit("Model grading failed; inspect the grading runs")
    else:
        results = []
        synthetic_gradings = []
        if len(set(args.grading_run_id)) != len(args.grading_run_id):
            parser.error("Duplicate grading run IDs")
        for run_id in args.grading_run_id:
            run = tracking.get_run(run_id)
            if any(
                run.data.tags.get(key) != value
                for key, value in tags("grading").items()
            ):
                raise ValueError("unsupported_grading_run_schema")
            if run.data.tags.get("review.provenance") == "synthetic":
                synthetic_gradings.append(run_id)
            results.append(
                read_artifact(tracking, run_id, "grading-result.json", args.output_dir)
            )
        results.extend(json.loads(path.read_text()) for path in args.grading_file)
        if not results:
            parser.error("agreement requires --grading-run-id or --grading-file")
        for result in results:
            validate_grading(bundle, result)
            if not result["complete"]:
                raise ValueError("agreement_requires_complete_grading")
        if len({r["rubric_digest"] for r in results}) != 1:
            raise ValueError("agreement_requires_same_rubric")
        entries = [row for result in results for row in result["ratings"]]
        report = build_agreement(bundle, entries, results[0]["rubric"])
        report["grading_run_ids"] = args.grading_run_id
        report["synthetic_grading_run_ids"] = synthetic_gradings
        report["schema"] = SCHEMA
        write_json(args.output_dir / "agreement.json", report)
        if args.log:
            run_id = tracking.create_run(
                experiment,
                tags=tags(
                    "agreement",
                    **{
                        "mlflow.runName": "grading-agreement",
                        "mlflow.parentRunId": args.source_run_id,
                        "source_run_id": args.source_run_id,
                        "source_bundle_digest": bundle["digest"],
                        "agreement.provenance": "synthetic-demo"
                        if synthetic_gradings
                        else "observed",
                    },
                ),
            ).info.run_id
            try:
                tracking.log_dict(run_id, report, "agreement.json")
                tracking.log_dict(run_id, {"results": results}, "grading-inputs.json")
                for name, value in report_metrics(report).items():
                    tracking.log_metric(run_id, name, value)
                tracking.set_terminated(run_id, "FINISHED")
                print(json.dumps({"agreement_run_id": run_id}))
            except Exception:
                tracking.set_terminated(run_id, "FAILED")
                raise
        print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
