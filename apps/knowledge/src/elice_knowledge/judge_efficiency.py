"""Collect request observations without persisting prompts or provider responses."""

import json
import math
import time
from threading import Lock

from elice_knowledge.corpus import digest


def row_key(inputs, outputs, expectations):
    return digest({"inputs": inputs, "outputs": outputs, "expectations": expectations})


def response_payloads(body):
    """Read JSON or SSE events without retaining prompts or responses."""
    try:
        return [json.loads(body)]
    except (ValueError, UnicodeDecodeError):
        candidates = []
        for line in body.splitlines():
            if not line.startswith(b"data:"):
                continue
            try:
                candidates.append(json.loads(line[5:].strip()))
            except (ValueError, UnicodeDecodeError):
                continue
        return candidates


def response_usage(body):
    """Read OpenAI-compatible usage; absent values stay absent."""
    for payload in reversed(response_payloads(body)):
        usage = payload.get("usage") if isinstance(payload, dict) else None
        if isinstance(usage, dict):
            details = usage.get("prompt_tokens_details")
            if not isinstance(details, dict):
                details = {}
            fields = {
                "input_tokens": usage.get("prompt_tokens", usage.get("input_tokens")),
                "cached_input_tokens": details.get("cached_tokens"),
                "output_tokens": usage.get("completion_tokens", usage.get("output_tokens")),
                "total_tokens": usage.get("total_tokens"),
            }
            return {
                key: value if type(value) is int and value >= 0 else None
                for key, value in fields.items()
            }
    return {
        key: None
        for key in ("input_tokens", "cached_input_tokens", "output_tokens", "total_tokens")
    }


def response_models(body):
    return sorted(
        {
            payload["model"]
            for payload in response_payloads(body)
            if isinstance(payload, dict)
            and isinstance(payload.get("model"), str)
            and payload["model"].strip()
        }
    )


def model_identity(artifact, endpoint):
    calls = [call for row in artifact["rows"] for call in row["calls"]]
    successful = [call for call in calls if 200 <= call.get("http_status", 0) < 300]
    alias = endpoint.removeprefix("gateway:/")
    responses = sorted({model for call in successful for model in call.get("response_models", [])})
    identified = [name for name in responses if name != alias and not name.startswith("gateway:/")]
    return {
        "schema": "judge-model-v1",
        "grading_run_id": artifact["grading_run_id"],
        "requested_endpoint": endpoint,
        "response_models": responses,
        "identified_models": identified,
        "responses": len(successful),
        "responses_with_model": sum(bool(call.get("response_models")) for call in successful),
        "identified_responses": sum(
            any(name in identified for name in call.get("response_models", []))
            for call in successful
        ),
    }


class Collector:
    def __init__(self, records, cases):
        self._lock = Lock()
        self._rows = []
        self._ids = {}
        for record in records:
            case = cases.get(record["id"])
            if not case:
                continue
            key = row_key(record["inputs"], case["result"], record["expectations"])
            if key in self._ids:
                raise ValueError("judge_efficiency_ambiguous_record")
            self._ids[key] = record["id"]

    def record(self, inputs, outputs, expectations, dimension, start, calls, status):
        key = row_key(inputs, outputs, expectations)
        record_id = self._ids.get(key)
        if record_id is None:
            raise ValueError("judge_efficiency_record_mismatch")
        elapsed = (time.monotonic() - start) * 1000
        with self._lock:
            self._rows.append(
                {
                    "record_id": record_id,
                    "dimension": dimension,
                    "status": status,
                    "scorer_duration_ms": elapsed if math.isfinite(elapsed) else None,
                    "calls": calls,
                }
            )

    def artifact(self, *, run_id, elapsed_ms):
        with self._lock:
            rows = sorted(self._rows, key=lambda row: (row["record_id"], row["dimension"]))
        return {
            "schema": "judge-efficiency-v1",
            "grading_run_id": run_id,
            "evaluation_elapsed_ms": elapsed_ms,
            "rows": rows,
        }


def metrics(artifact):
    calls = [call for row in artifact["rows"] for call in row["calls"]]
    result = {
        "judge.efficiency.evaluation_elapsed_ms": artifact["evaluation_elapsed_ms"],
        "judge.efficiency.http_responses": len(calls),
        "judge.efficiency.scorer_errors": sum(row["status"] == "error" for row in artifact["rows"]),
    }
    for field in (
        "duration_ms",
        "input_tokens",
        "cached_input_tokens",
        "output_tokens",
        "total_tokens",
    ):
        values = [call[field] for call in calls if call.get(field) is not None]
        result[f"judge.efficiency.{field}.observed_calls"] = len(values)
        if values and len(values) == len(calls) and result["judge.efficiency.scorer_errors"] == 0:
            result[f"judge.efficiency.{field}.total"] = sum(values)
    return result


def hooks(calls):
    async def on_request(request):
        request.extensions["judge_efficiency_start"] = time.monotonic()

    async def on_response(response):
        start = response.request.extensions.get("judge_efficiency_start")
        usage = response_usage(b"")
        models = []
        try:
            body = await response.aread()
            usage = response_usage(body)
            models = response_models(body)
        finally:
            elapsed = (time.monotonic() - start) * 1000 if start is not None else None
            calls.append(
                {
                    "duration_ms": elapsed,
                    "http_status": response.status_code,
                    "response_models": models,
                    **usage,
                }
            )

    return {"request": [on_request], "response": [on_response]}
