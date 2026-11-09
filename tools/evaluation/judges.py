"""Register Gold/Iron/online Judges; activate dev online scoring with --enable-dev."""

import argparse
import json

from elice_knowledge.judges import KINDS, ONLINE_FILTER, RATERS, configure, instructions
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
    for kind in KINDS:
        for rater in RATERS:
            name = f"qa-{kind}-{rater}"
            model = f"gateway:/elice-techblog-rag-judge-{rater}"
            text = instructions(kind)
            judge = existing.get(name)
            if judge is None or judge.instructions != text or judge.model != model:
                judge = make_judge(
                    name=name, instructions=text, model=model, feedback_value_type=bool
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
