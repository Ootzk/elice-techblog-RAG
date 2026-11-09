"""QA resource summaries, with explicit measurement coverage and no Judge usage."""

from statistics import mean, median

TOKEN_FIELDS = {
    "input_tokens": "prompt_tokens",
    "cached_input_tokens": "cached_tokens",
    "output_tokens": "completion_tokens",
    "total_tokens": "total_tokens",
}


def summarize(cases):
    metrics = {}

    def record(name, values, total=False):
        metrics[name + "_measured_cases"] = len(values)
        if values:
            metrics.update(
                {
                    name + "_mean": mean(values),
                    name + "_median": median(values),
                    name + "_max": max(values),
                }
            )
            if total:
                metrics[name + "_total"] = sum(values)

    valid = [case for case in cases if not case["error"]]
    record("qa_duration_ms", [case["duration_ms"] for case in valid])
    record("failed_qa_duration_ms", [case["duration_ms"] for case in cases if case["error"]])
    for field in ("llm_call_count", "tool_call_count"):
        record(field, [case["result"][field] for case in valid], total=True)
    for name, field in TOKEN_FIELDS.items():
        record(
            name,
            [case["result"]["usage"][field] for case in valid if field in case["result"]["usage"]],
            total=True,
        )
    cache_cohort = [
        case["result"]["usage"]
        for case in valid
        if {"cached_tokens", "prompt_tokens"} <= case["result"]["usage"].keys()
    ]
    denominator = sum(usage["prompt_tokens"] for usage in cache_cohort)
    if denominator:
        metrics["cache_hit_ratio"] = (
            sum(usage["cached_tokens"] for usage in cache_cohort) / denominator
        )
    metrics["cache_hit_ratio_measured_cases"] = len(cache_cohort)
    return metrics
