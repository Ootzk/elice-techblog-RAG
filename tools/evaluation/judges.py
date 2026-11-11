"""Register isolated quality dimensions; activate dev online scoring with --enable-dev."""

import argparse
import json
from typing import Literal

from elice_knowledge.judges import (
    DIMENSIONS,
    ONLINE_FILTER,
    RATERS,
    configure,
    instructions,
    judge_name,
    offline_instructions,
)
from elice_knowledge.settings import Settings
from mlflow.genai.judges import make_judge
from mlflow.genai.scorers import ScorerSamplingConfig, list_scorers


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--enable-dev", action="store_true")
    args = parser.parse_args()
    experiment_id = configure(Settings())
    existing = {
        judge.name: judge for judge in list_scorers(experiment_id=experiment_id)
    }
    results = []
    for kind, dimensions in DIMENSIONS.items():
        for dimension in dimensions:
            for rater in RATERS:
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
                if judge is None or judge.instructions != text or judge.model != model:
                    judge = make_judge(
                        name=name,
                        instructions=text,
                        model=model,
                        feedback_value_type=value_type,
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
