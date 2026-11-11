import importlib.util
import sys
from pathlib import Path


def test_online_scorer_registration_accepts_per_judge_options(monkeypatch):
    path = Path(__file__).parents[3] / "tools/evaluation/judges.py"
    spec = importlib.util.spec_from_file_location("judge_registration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    calls = []

    class Judge:
        scorer_version = 1
        sample_rate = None
        filter_string = None

        def register(self, **kwargs):
            return self

        def start(self, **kwargs):
            return self

    def make(**kwargs):
        calls.append(kwargs)
        return Judge()

    monkeypatch.setattr(module, "configure", lambda _: "experiment")
    monkeypatch.setattr(module, "selected_raters", lambda _: ["a", "b"])
    monkeypatch.setattr(module, "list_scorers", lambda **kwargs: [])
    monkeypatch.setattr(module, "make_judge", make)
    monkeypatch.setattr(sys, "argv", ["judges.py", "--judge-options", '{"a":{"temperature":0.2}}'])
    module.main()
    online_a = [x for x in calls if x["name"].startswith("qa-online-") and x["name"].endswith("-a")]
    online_b = [x for x in calls if x["name"].startswith("qa-online-") and x["name"].endswith("-b")]
    offline = [x for x in calls if not x["name"].startswith("qa-online-")]
    assert online_a and all(x["inference_params"] == {"temperature": 0.2} for x in online_a)
    assert online_b and all(x["inference_params"] is None for x in online_b)
    assert offline and all(x["inference_params"] is None for x in offline)
