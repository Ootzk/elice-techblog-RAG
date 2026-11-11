import json

import pytest

from elice_knowledge.agreement import build_agreement, pairwise, review_packet
from elice_knowledge.evaluation_bundle import create_bundle, validate_bundle


def bundle():
    records = [
        dict(
            id="r1",
            inputs={"question": "Q"},
            expectations=dict(
                answerable=True,
                answer="reference",
                required_points=["fact"],
                evidence=[{"quote": "fact"}],
            ),
        )
    ]
    cases = [
        dict(
            record_id="r1",
            error=None,
            result=dict(
                status="answered",
                answer="fact",
                generation_model="hidden-generation-model",
                generation_sources=[{"text": "fact"}],
                retrieved_sources=[{"text": "unseen material"}],
            ),
        )
    ]
    return create_bundle(
        "run", {"dataset_id": "d", "dataset_digest": "digest", "records": records}, {}, cases
    )


def test_blind_packet_and_binding_reject_changed_answers_and_duplicates():
    source = bundle()
    packet, labels = review_packet(source, "alice")
    assert len(packet) == len(labels) == 4
    assert "hidden-generation-model" not in json.dumps(packet)
    assert "unseen material" not in json.dumps(packet)
    grounding = next(row for row in packet if row["dimension"] == "groundedness")
    assert "expectations" not in grounding
    assert all(row["score"] is None for row in labels)
    assert build_agreement(source, labels)["human_labels"] == 0
    labels[0]["score"] = 2
    labels[0]["status"] = "scored"
    with pytest.raises(ValueError, match="duplicate_review_label"):
        build_agreement(source, labels + [labels[0]])
    labels[0]["answer_digest"] = "different-answer"
    with pytest.raises(ValueError, match="binding_mismatch"):
        build_agreement(source, labels)
    source["cases"][0]["result"]["answer"] = "changed"
    with pytest.raises(ValueError, match="digest_mismatch"):
        validate_bundle(source)


def test_binary_kappa_na_exclusion_and_constant_labels():
    stats = pairwise(
        dict(a=2, b=2, c=0, d=0, e="not_applicable", f="error"),
        dict(a=2, b=0, c=0, d=0, e=2, f=2),
    )
    assert stats["valid_pairs"] == 4 and stats["excluded_pairs"] == 2
    assert stats["agreement"] == 0.75 and stats["cohen_kappa"] == 0.5
    assert stats["confusion"]["2_0"] == 1
    assert stats["applicability_pairs"] == 5 and stats["applicability_agreement"] == 0.8
    stats = pairwise(dict(a=2, b=2), dict(a=2, b=2))
    assert stats["agreement"] == 1 and stats["cohen_kappa"] is None
    assert pairwise({"a": 2}, {"a": 2})["cohen_kappa"] is None


def test_rounds_compare_identical_answers_and_distinguish_rubric_changes():
    source = bundle()
    from copy import deepcopy

    from elice_knowledge.corpus import digest

    _, first = review_packet(source, "alice")
    for row in first:
        row["score"] = 2
        row["status"] = "scored"
        row["rater"] = "model:chatgpt"
        row["protocol"] = digest({"model": "chatgpt", "version": 1})
    second = deepcopy(first)
    for row in second:
        row["round"] = "2"
    assert (
        build_agreement(source, first + second)["comparisons"][0]["comparison"]
        == "repeat_consistency"
    )
    third = deepcopy(first)
    for row in third:
        row["protocol"] = digest({"model": "chatgpt", "version": 2})
    assert (
        build_agreement(source, first + third)["comparisons"][0]["comparison"] == "judge_revision"
    )
    assert bundle()["cases"][0]["result"]["answer"] == "fact"


def test_uniform_random_baseline_reproducible_and_respects_applicability():
    import importlib.util
    from pathlib import Path

    from elice_knowledge.evaluation_schema import validate_grading
    from elice_knowledge.rubrics import load_rubric

    path = Path(__file__).resolve().parents[3] / "tools/evaluation/random_baseline.py"
    spec = importlib.util.spec_from_file_location("random_baseline", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    source = bundle()
    rubric = load_rubric("gold")
    profile, result = module.make_result(source, rubric, 42, 1)
    assert module.make_result(source, rubric, 42, 1) == (profile, result)
    assert profile["type"] == "baseline" and profile["seed"] == 42
    assert validate_grading(source, result) == result
    assert all(r["score"] in (0, 1, 2) for r in result["ratings"])
    first = result["ratings"]
    second = module.make_result(source, rubric, 42, 2)[1]["ratings"]
    report = build_agreement(source, first + second, rubric)
    assert all(c["comparison"] == "repeat_consistency" for c in report["comparisons"])
    assert all(not p["members"] for p in report["panels"])
    source["records"][0]["expectations"]["answerable"] = False
    from elice_knowledge.evaluation_bundle import create_bundle

    source = create_bundle(
        "run",
        {"dataset_id": "d", "dataset_digest": "digest", "records": source["records"]},
        {},
        source["cases"],
    )
    _, result = module.make_result(source, rubric, 42, 1)
    assert (
        next(r for r in result["ratings"] if r["dimension"] == "correctness")["status"]
        == "not_applicable"
    )
