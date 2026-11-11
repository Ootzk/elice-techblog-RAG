"""Create explicitly synthetic uniform-random grading runs for frozen answers."""

import argparse
import json
import random
import re
from pathlib import Path
from tempfile import TemporaryDirectory

from elice_knowledge.corpus import digest
from elice_knowledge.evaluation_bundle import answer_digest, validate_bundle
from elice_knowledge.evaluation_schema import (
    grading_result,
    metrics,
    tags,
    validate_grading,
)
from elice_knowledge.judges import DIMENSIONS, inapplicable
from elice_knowledge.rubrics import CONTRACT, load_rubric, snapshot
from elice_knowledge.settings import Settings
from elice_knowledge.tracking import client


def make_result(bundle, rubric, seed, round_number):
    """Each applicable judgment is independently uniform; N/A follows the shared rubric."""
    rubric = snapshot(bundle, rubric)
    profile = {
        "type": "baseline",
        "method": "uniform-random-v1",
        "seed": seed,
        "rubric": rubric,
        "rubric_digest": digest(rubric),
        "contract": CONTRACT,
    }
    profile_key = digest(profile)
    grading = {
        "profile": profile,
        "profile_digest": profile_key,
        "rater_type": "baseline",
        "rater_id": "baseline:uniform-random",
        "round": round_number,
    }
    records = {r["id"]: r for r in bundle["records"]}
    ratings = []
    for case in bundle["cases"]:
        if case["error"]:
            continue
        record = records[case["record_id"]]
        for dimension in DIMENSIONS[bundle["kind"]]:
            na = inapplicable(record, case, dimension, rubric=rubric)
            key = digest(
                ["uniform-random-v1", seed, round_number, case["record_id"], dimension]
            )
            score = None if na else random.Random(int(key, 16)).choice(rubric["scale"])
            ratings.append(
                {
                    "record_id": case["record_id"],
                    "answer_digest": answer_digest(record, case),
                    "dimension": dimension,
                    "rater": grading["rater_id"],
                    "round": str(round_number),
                    "protocol": profile_key,
                    "score": score,
                    "status": "not_applicable" if na else "scored",
                    "rationale": "공통 평가 기준에 따라 N/A"
                    if na
                    else "무작위 균등 추출 (판정 근거 없음)",
                }
            )
    return profile, grading_result(bundle, grading, ratings)


def read(tracking, run_id, name):
    with TemporaryDirectory(prefix="random-baseline-") as directory:
        return json.loads(
            Path(tracking.download_artifacts(run_id, name, directory)).read_text()
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--answers-run-id", required=True)
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument(
        "--rubric-grading-run-id", help="Use the exact rubric from an existing grading"
    )
    parser.add_argument(
        "--submit", action="store_true", help="Create baseline grading runs in MLflow"
    )
    args = parser.parse_args()
    if not 1 <= args.repeat <= 100:
        parser.error("--repeat must be between 1 and 100")
    if not re.fullmatch(r"[a-f0-9]{32}", args.answers_run_id):
        parser.error("--answers-run-id must be an MLflow run ID")
    tracking = client(Settings())
    source = tracking.get_run(args.answers_run_id)
    if (
        source.info.status != "FINISHED"
        or source.data.tags.get("evaluation.kind") != "answers"
    ):
        raise ValueError("A finished answers run is required")
    bundle = validate_bundle(
        read(tracking, args.answers_run_id, "evaluation-bundle.json")
    )
    if bundle["source_run_id"] != args.answers_run_id:
        raise ValueError("source_run_mismatch")
    if args.rubric_grading_run_id:
        rubric = validate_grading(
            bundle, read(tracking, args.rubric_grading_run_id, "grading-result.json")
        )["rubric"]
    else:
        rubric = load_rubric(bundle["kind"])
    profile, _ = make_result(bundle, rubric, args.seed, 1)
    profile_key = digest(profile)
    existing = tracking.search_runs(
        [source.info.experiment_id],
        filter_string=(
            f"tags.`source_run_id` = '{args.answers_run_id}' and tags.`rater_id` = 'baseline:uniform-random'"
        ),
        max_results=10000,
    )
    if len(existing) == 10000:
        raise ValueError("Too many baseline runs to allocate a new round safely")
    rounds = [
        int(r.data.tags["round"])
        for r in existing
        if r.data.tags.get("rater_profile_digest") == profile_key
    ]
    start = max(rounds, default=0) + 1
    print(
        json.dumps(
            {
                "baseline": "uniform-random",
                "seed": args.seed,
                "rounds": list(range(start, start + args.repeat)),
                "rubric": rubric["id"],
                "submit": args.submit,
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    if not args.submit:
        return
    for round_number in range(start, start + args.repeat):
        profile, result = make_result(bundle, rubric, args.seed, round_number)
        run_id = tracking.create_run(
            source.info.experiment_id,
            tags=tags(
                "grading",
                **{
                    "mlflow.runName": f"무작위 기준선 · round {round_number}",
                    "mlflow.parentRunId": args.answers_run_id,
                    "source_run_id": args.answers_run_id,
                    "source_bundle_digest": bundle["digest"],
                    "rater_type": "baseline",
                    "rater_id": "baseline:uniform-random",
                    "rater_profile_digest": profile_key,
                    "round": str(round_number),
                    "rubric_digest": profile["rubric_digest"],
                    "baseline.method": profile["method"],
                    "baseline.seed": str(args.seed),
                    "review.provenance": "random-baseline",
                },
            ),
        ).info.run_id
        try:
            tracking.log_dict(run_id, profile, "rater-profile.json")
            tracking.log_dict(run_id, result, "grading-result.json")
            for name, value in metrics(bundle, result).items():
                tracking.log_metric(run_id, name, value)
            tracking.set_terminated(run_id, "FINISHED")
        except Exception:
            tracking.set_terminated(run_id, "FAILED")
            raise
        print(json.dumps({"run_id": run_id, "round": round_number}), flush=True)


if __name__ == "__main__":
    main()
