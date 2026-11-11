from elice_reporting.stacked_efficiency import (
    answer_groups,
    judge_vector,
    paired_answer_groups,
    stack_chart,
    token_vector,
)


def case(record_id, usage, steps=None):
    return {
        "record_id": record_id,
        "error": None,
        "result": {"usage": usage, "agent_steps": steps or [], "tool_call_count": len(steps or [])},
    }


def snapshot(*cases):
    return {"bundle": {"cases": list(cases)}}


def test_token_cache_is_subset_and_unknown_is_not_zero():
    assert token_vector(
        {"prompt_tokens": 100, "cached_tokens": 25, "completion_tokens": 20, "total_tokens": 120}
    ) == {"first": 100, "cached": 25, "second": 20}
    assert token_vector({"prompt_tokens": 100, "completion_tokens": 20})["cached"] is None
    assert (
        token_vector({"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 121}) is None
    )
    assert (
        judge_vector(
            [
                {"input_tokens": 10, "output_tokens": 2, "cached_input_tokens": 3},
                {"input_tokens": 20, "output_tokens": 4},
            ]
        )["cached"]
        is None
    )


def test_paired_stacks_use_same_question_and_joint_token_observations():
    a = snapshot(
        case("q1", {"prompt_tokens": 10, "completion_tokens": 2, "cached_tokens": 4}),
        case("q2", {"prompt_tokens": 100, "completion_tokens": 1}),
    )
    b = snapshot(
        case("q1", {"prompt_tokens": 20, "completion_tokens": 5}),
        case("q2", {"prompt_tokens": 200}),
    )
    groups = paired_answer_groups({"A": [a], "B": [b]}, "tokens")
    assert groups[0]["points"][0]["vector"] == {"first": 10, "second": 2, "cached": 4}
    assert groups[1]["points"][0]["vector"] == {"first": 20, "second": 5, "cached": None}
    assert groups[0]["points"][0]["support"] == "공통 1문항"
    assert answer_groups([a], "tokens")[0]["points"][0]["vector"]["cached"] is None
    view = stack_chart(groups, "tokens", "test")
    glyphs = [r.glyph for r in view.object.renderers if getattr(r, "glyph", None)]
    assert any(getattr(g, "hatch_pattern", None) == "dot" for g in glyphs)
    source = view.object.renderers[0].data_source.data
    assert source["text"] == ["12", "25"]
    assert source["first_label"] == ["10", "20"]
    assert source["cache_label"] == ["4", "—"]
    assert source["second_label"] == ["2", "5"]


def test_repeat_stack_shows_mean_without_individual_points():
    groups = [
        {
            "label": "답변",
            "points": [
                {"answer": 1, "vector": {"first": 1, "second": 3, "cached": None}},
                {"answer": 2, "vector": {"first": 3, "second": 1, "cached": None}},
            ],
        }
    ]
    plot = stack_chart(groups, "tools", "도구 호출", repeat=True).object
    assert len(plot.renderers) == 2
    source = plot.renderers[0].data_source.data
    assert source["first_label"] == ["2.0"]
    assert source["second_label"] == ["2.0"]
    assert source["text"] == ["4.0"]
