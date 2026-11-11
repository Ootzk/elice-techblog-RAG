"""Register isolated quality dimensions; activate dev online scoring with --enable-dev."""

import argparse
import json
from typing import Literal

from elice_knowledge.judges import (
    DIMENSIONS,
    ONLINE_FILTER,
    configure,
    instructions,
    judge_name,
    offline_instructions,
    selected_raters,
)
from elice_knowledge.settings import Settings
from mlflow.genai.judges import make_judge
from mlflow.genai.scorers import ScorerSamplingConfig, list_scorers


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--enable-dev", action="store_true")
    parser.add_argument(
        "--judge-options",
        type=json.loads,
        default=None,
        help="JSON object mapping online judge names to inference parameters",
    )
    args = parser.parse_args()
    settings = Settings()
    experiment_id = configure(settings)
    existing = {
        judge.name: judge for judge in list_scorers(experiment_id=experiment_id)
    }
    raters = selected_raters(settings)
    from elice_knowledge.model_options import validate_judge_options

    if args.judge_options is not None and (
        not isinstance(args.judge_options, dict)
        or set(args.judge_options) - set(raters)
    ):
        parser.error("--judge-options must map configured judges to option objects")
    options = {
        name: validate_judge_options(value)
        for name, value in (args.judge_options or {}).items()
    }
    if args.enable_dev:
        desired = {
            judge_name("online", dimension, rater)
            for dimension in DIMENSIONS["online"]
            for rater in raters
        }
        for name, judge in existing.items():
            if (
                name.startswith("qa-online-")
                and name not in desired
                and judge.sample_rate
            ):
                judge.stop(experiment_id=experiment_id)
    results = []
    for kind, dimensions in DIMENSIONS.items():
        for dimension in dimensions:
            for rater in raters:
                name = judge_name(kind, dimension, rater)
                model = f"gateway:/elice-techblog-rag-judge-{rater}"
                text = (
                    instructions(dimension)
                    if kind == "online"
                    else offline_instructions(kind, dimension)
                )
                value_type = (
                    Literal["0", "1", "2", "not_applicable"]
                    if kind == "online"
                    else Literal["0", "1", "not_applicable"]
                    if kind == "iron"
                    else Literal["0", "1", "2", "not_applicable"]
                )
                judge = existing.get(name)
                if (
                    kind == "online"
                    and args.judge_options is None
                    and judge is not None
                ):
                    inference_params = getattr(judge, "inference_params", None) or {}
                else:
                    inference_params = (
                        options.get(rater, {}) if kind == "online" else {}
                    )
                if (
                    judge is None
                    or judge.instructions != text
                    or judge.model != model
                    or (getattr(judge, "inference_params", None) or {})
                    != inference_params
                ):
                    judge = make_judge(
                        name=name,
                        instructions=text,
                        model=model,
                        feedback_value_type=value_type,
                        inference_params=inference_params or None,
                    ).register(experiment_id=experiment_id)
                if kind == "online" and args.enable_dev:
                    judge = judge.start(
                        experiment_id=experiment_id,
                        sampling_config=ScorerSamplingConfig(
                            sample_rate=1.0, filter_string=ONLINE_FILTER
                        ),
                    )
                results.append(
                    {
                        "name": name,
                        "version": judge.scorer_version,
                        "sample_rate": judge.sample_rate,
                        "filter": judge.filter_string,
                    }
                )
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
