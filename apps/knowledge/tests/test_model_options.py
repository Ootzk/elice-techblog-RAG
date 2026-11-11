import pytest
from pydantic import ValidationError

from elice_knowledge.model_options import validate_options
from elice_knowledge.service_api import EvaluationRequest, ProfileRequest


def test_profile_accepts_execution_options_and_rejects_transport_override():
    request = ProfileRequest(
        generation_options={"temperature": 0.2, "seed": 42}, embedding_options={"dimensions": 1536}
    )
    assert request.generation_options["seed"] == 42
    assert request.embedding_options["dimensions"] == 1536
    for options in ({"model": "other"}, {"stream": True}, {"api_key": "secret"}):
        with pytest.raises(ValidationError):
            ProfileRequest(generation_options=options)
    with pytest.raises(ValidationError):
        ProfileRequest(embedding_options={"truncate": True})


def test_judge_options_are_keyed_by_judge_and_reject_secrets():
    common = dict(
        dataset_id="d",
        dataset_digest="a" * 64,
        qa_profile_key="b" * 64,
        scorer_profile_key="c" * 64,
    )
    request = EvaluationRequest(
        **common, judge_options={"any-judge": {"temperature": 0, "stream": True}}
    )
    assert request.judge_options["any-judge"]["stream"] is True
    with pytest.raises(ValidationError):
        EvaluationRequest(
            **common, judge_options={"any-judge": {"headers": {"Authorization": "secret"}}}
        )
    with pytest.raises(ValueError):
        validate_options({"value": float("nan")})
