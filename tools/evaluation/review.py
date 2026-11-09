"""Rejudge frozen answers, export blind packets, and compare human/model labels."""

import argparse
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from elice_knowledge.agreement import analyze, machine_labels, review_packet
from elice_knowledge.evaluation_bundle import validate_bundle
from elice_knowledge.judges import configure, evaluate_answers, frozen_judges
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
    parser.add_argument("action", choices=("export", "judge", "analyze"))
    parser.add_argument("--source-run-id", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--judge-run-id", action="append", default=[])
    parser.add_argument("--human-labels", action="append", type=Path, default=[])
    parser.add_argument("--rater", default="reviewer")
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--submit", action="store_true")
    parser.add_argument(
        "--log",
        action="store_true",
        help="Log agreement and human assessments to MLflow",
    )
    args = parser.parse_args()
    if not 1 <= args.repeat <= 5:
        parser.error("--repeat must be between 1 and 5")
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
        packet, labels = review_packet(bundle, args.rater)
        path = args.output_dir / "human-labels.jsonl"
        if path.exists():
            raise FileExistsError(
                "Use a new output directory; existing human labels are preserved"
            )
        write_json(args.output_dir / "review-packet.json", packet)
        path.write_text(
            "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in labels)
        )
        print(
            json.dumps(
                {
                    "packet": str(args.output_dir / "review-packet.json"),
                    "labels": str(path),
                }
            )
        )
    elif args.action == "judge":
        specs = frozen_judges(settings, bundle["kind"])
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
                    },
                    indent=2,
                )
            )
            return
        for _ in range(args.repeat):
            run_id = tracking.create_run(
                experiment,
                tags={
                    "mlflow.runName": "saved-answer-judging",
                    "activity": "rejudging",
                    "source_run_id": args.source_run_id,
                    "source_bundle_digest": bundle["digest"],
                },
            ).info.run_id
            try:
                tracking.log_dict(run_id, bundle, "evaluation-bundle.json")
                tracking.log_dict(run_id, specs, "judge-versions.json")
                metrics, judgments = evaluate_answers(
                    settings, run_id, bundle["records"], bundle["cases"], specs
                )
                tracking.set_terminated(run_id, "FINISHED")
                write_json(
                    args.output_dir / (run_id + ".json"),
                    {"run_id": run_id, "metrics": metrics, "judgments": judgments},
                )
                print(
                    json.dumps({"run_id": run_id, "source_run_id": args.source_run_id}),
                    flush=True,
                )
            except Exception:
                tracking.set_terminated(run_id, "FAILED")
                raise
    else:
        entries = []
        for run_id in args.judge_run_id:
            judged_bundle = validate_bundle(
                read_artifact(
                    tracking, run_id, "evaluation-bundle.json", args.output_dir
                )
            )
            if judged_bundle["digest"] != bundle["digest"]:
                raise ValueError("judged_answers_differ_from_source")
            results = read_artifact(
                tracking, run_id, "judge-results.json", args.output_dir
            )
            entries.extend(machine_labels(bundle, results["cases"], run_id))
        human = []
        for path in args.human_labels:
            rows = [
                json.loads(line)
                for line in path.read_text().splitlines()
                if line.strip()
            ]
            if any(not row["rater"].startswith("human:") for row in rows):
                raise ValueError("Human rater must start with human:")
            human.extend(rows)
        entries.extend(human)
        report = analyze(bundle, entries)
        write_json(args.output_dir / "agreement.json", report)
        if args.log:
            import mlflow
            from mlflow.entities import AssessmentSource, AssessmentSourceType

            run_id = tracking.create_run(
                experiment,
                tags={
                    "mlflow.runName": "judge-agreement",
                    "source_run_id": args.source_run_id,
                    "source_bundle_digest": bundle["digest"],
                },
            ).info.run_id
            try:
                tracking.log_dict(run_id, report, "agreement.json")
                tracking.log_dict(run_id, {"labels": entries}, "review-labels.json")
                for row in human:
                    if row["label"] not in {"pass", "fail", "not_applicable"}:
                        continue
                    case = next(
                        c for c in bundle["cases"] if c["record_id"] == row["record_id"]
                    )
                    if not case.get("trace_id"):
                        raise ValueError("source_qa_trace_missing")
                    mlflow.log_feedback(
                        trace_id=case["trace_id"],
                        name="human-" + row["dimension"],
                        value=row["label"],
                        rationale=row.get("rationale"),
                        source=AssessmentSource(
                            source_type=AssessmentSourceType.HUMAN,
                            source_id=row["rater"],
                        ),
                        metadata={
                            "answer_digest": row["answer_digest"],
                            "review_run_id": run_id,
                        },
                    )
                tracking.set_terminated(run_id, "FINISHED")
                print(json.dumps({"review_run_id": run_id}))
            except Exception:
                tracking.set_terminated(run_id, "FAILED")
                raise
        print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
