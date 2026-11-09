import json

import pytest

from elice_knowledge.agreement import analyze, machine_labels, pairwise, review_packet
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
    assert all(row["label"] is None for row in labels)
    assert analyze(source, labels)["human_labels"] == 0
    labels[0]["label"] = "pass"
    with pytest.raises(ValueError, match="duplicate_review_label"):
        analyze(source, labels + [labels[0]])
    labels[0]["answer_digest"] = "different-answer"
    with pytest.raises(ValueError, match="binding_mismatch"):
        analyze(source, labels)
    source["cases"][0]["result"]["answer"] = "changed"
    with pytest.raises(ValueError, match="digest_mismatch"):
        validate_bundle(source)


def test_binary_kappa_na_exclusion_and_constant_labels():
    stats = pairwise(
        dict(a="pass", b="pass", c="fail", d="fail", e="not_applicable", f="error"),
        dict(a="pass", b="fail", c="fail", d="fail", e="pass", f="pass"),
    )
    assert stats["binary_pairs"] == 4 and stats["excluded_pairs"] == 2
    assert stats["agreement"] == 0.75 and stats["cohen_kappa"] == 0.5
    assert stats["confusion"]["pass_fail"] == 1
    assert stats["applicability_pairs"] == 5 and stats["applicability_agreement"] == 0.8
    stats = pairwise(dict(a="pass", b="pass"), dict(a="pass", b="pass"))
    assert stats["agreement"] == 1 and stats["cohen_kappa"] is None
    assert pairwise({"a": "pass"}, {"a": "pass"})["cohen_kappa"] is None


def test_rounds_compare_identical_answers_and_distinguish_rubric_changes():
    source = bundle()
    judged = [
        {
            "record_id": "r1",
            "judges": {
                "qa-gold-correctness-chatgpt": {
                    "dimension": "correctness",
                    "rater": "chatgpt",
                    "version": 1,
                    "status": "pass",
                }
            },
        }
    ]
    first = machine_labels(source, judged, "round-1")
    second = machine_labels(source, judged, "round-2")
    assert analyze(source, first + second)["comparisons"][0]["comparison"] == "repeat_consistency"
    judged[0]["judges"]["qa-gold-correctness-chatgpt"]["version"] = 2
    third = machine_labels(source, judged, "round-3")
    assert analyze(source, first + third)["comparisons"][0]["comparison"] == "judge_revision"
    assert bundle()["cases"][0]["result"]["answer"] == "fact"
