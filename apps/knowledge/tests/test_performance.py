from elice_knowledge.performance import summarize


def case(duration, usage, error=None):
    return {
        "duration_ms": duration,
        "error": error,
        "result": {"usage": usage, "llm_call_count": 3, "tool_call_count": 2},
    }


def test_performance_separates_failures_and_missing_cache_from_reported_zero():
    cases = [
        case(100, {"prompt_tokens": 100, "cached_tokens": 0, "completion_tokens": 10}),
        case(300, {"prompt_tokens": 200, "cached_tokens": 100, "completion_tokens": 20}),
        case(800, {"prompt_tokens": 500}),
        case(5, {}, "generation_timeout"),
    ]
    metrics = summarize(cases)
    assert metrics["qa_duration_ms_mean"] == 400
    assert metrics["qa_duration_ms_median"] == 300
    assert metrics["qa_duration_ms_max"] == 800
    assert metrics["failed_qa_duration_ms_mean"] == 5
    assert metrics["cached_input_tokens_total"] == 100
    assert metrics["cached_input_tokens_measured_cases"] == 2
    assert metrics["input_tokens_total"] == 800
    assert metrics["cache_hit_ratio"] == 100 / 300
    assert metrics["output_tokens_measured_cases"] == 2
    assert metrics["llm_call_count_total"] == 9
    assert metrics["total_tokens_measured_cases"] == 0
    assert "total_tokens_total" not in metrics


def test_no_reported_usage_does_not_mean_zero_tokens():
    metrics = summarize([case(50, {})])
    assert metrics["cached_input_tokens_measured_cases"] == 0
    assert "cached_input_tokens_total" not in metrics
    assert "cache_hit_ratio" not in metrics
