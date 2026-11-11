"""Ragas quality metrics, explicitly scoped inputs and Gateway-only model access."""

import asyncio
import json
import math
import os
from functools import lru_cache
from importlib.metadata import version
from pathlib import Path

from elice_knowledge.corpus import digest

CONTEXT_METRICS = (
    "retrieved_context_recall",
    "retrieved_context_precision",
    "generation_context_recall",
    "generation_context_precision",
)
DIAGNOSTICS = (*CONTEXT_METRICS, "factual_precision", "required_point_coverage", "faithfulness")


@lru_cache(maxsize=1)
def contract():
    import ragas

    root = Path(ragas.__file__).parent
    # The package version and actual implementation/prompt bytes identify the protocol.
    sources = {
        str(p.relative_to(root)): p.read_text()
        for p in (root / "metrics" / "collections").rglob("*.py")
    }
    sources["adapter"] = Path(__file__).read_text()
    return {
        "engine": "ragas",
        "metrics": {
            "quality": "DiscreteMetric",
            "factual_precision": "FactualCorrectness.precision",
            "required_point_coverage": "ContextRecall(reference=required_points,contexts=answer)",
            "faithfulness": "Faithfulness",
            **{
                name: "ContextRecall" if name.endswith("recall") else "ContextPrecision"
                for name in CONTEXT_METRICS
            },
        },
        "version": version("ragas"),
        "instructor_version": version("instructor"),
        "openai_version": version("openai"),
        "seed": None,
        "implementation_digest": digest(sources),
        "factual_atomicity": "high",
        "factual_coverage": "high",
        "context_order": "first-seen-deduplicated",
        "internal_llm_concurrency": 1,
        "structured_output": "json",
        "temperature": None,
        "top_p": None,
        "max_retries": 1,
    }


def payload(record, case, dimension, *, rubric=None):
    from elice_knowledge.judges import dimension_payload, inapplicable

    result, expected = case["result"], record["expectations"]
    if dimension in CONTEXT_METRICS:
        if not expected.get("answerable"):
            return None
        scope = "retrieved_sources" if dimension.startswith("retrieved_") else "generation_sources"
        if scope not in result:
            raise ValueError(f"{scope}_missing")
        contexts = list(dict.fromkeys(s["text"] for s in result[scope] if s["text"].strip()))
        return dict(
            user_input=question(record), reference=expected["answer"], retrieved_contexts=contexts
        )
    mapped = {
        "factual_precision": "correctness",
        "required_point_coverage": "completeness",
        "faithfulness": "groundedness",
    }.get(dimension, dimension)
    if inapplicable(record, case, mapped, rubric=rubric):
        return None
    if dimension == "factual_precision":
        return dict(response=result.get("answer") or "", reference=expected["answer"])
    if dimension == "required_point_coverage":
        return dict(
            user_input=question(record),
            reference="\n".join(expected["required_points"]),
            retrieved_contexts=[result.get("answer") or ""],
        )
    if dimension == "faithfulness":
        if result["status"] != "answered" or not (result.get("answer") or "").strip():
            return None
        if "generation_sources" not in result:
            raise ValueError("generation_sources_missing")
        return dict(
            user_input=question(record),
            response=result["answer"],
            retrieved_contexts=[s["text"] for s in result["generation_sources"]],
        )
    return dict(payload=json.dumps(dimension_payload(record, case, dimension), ensure_ascii=False))


def question(record):
    # A frozen conversation is part of the task; previous assistant text is not evidence.
    return json.dumps(record["inputs"], ensure_ascii=False)


async def metric_score(
    settings,
    model,
    dimension,
    data,
    rubric,
    *,
    request_options=None,
    allowed_values=None,
    calls=None,
):
    """One SDK scorer invocation. Each invocation permits one outbound request at a time."""
    import httpx
    from openai import AsyncOpenAI
    from ragas.llms import llm_factory
    from ragas.llms.base import InstructorBaseRagasLLM
    from ragas.metrics import DiscreteMetric
    from ragas.metrics.collections import (
        ContextPrecision,
        ContextRecall,
        FactualCorrectness,
        Faithfulness,
    )

    if not model.startswith("gateway:/") or not model.removeprefix("gateway:/"):
        raise ValueError("ragas_requires_gateway_model")
    token = os.environ.get("MLFLOW_TRACKING_TOKEN")
    username, password = (
        os.environ.get("MLFLOW_TRACKING_USERNAME"),
        os.environ.get("MLFLOW_TRACKING_PASSWORD"),
    )
    auth = httpx.BasicAuth(username, password or "") if username and not token else None
    from elice_knowledge.judge_efficiency import hooks

    async with AsyncOpenAI(
        base_url=settings.mlflow_tracking_uri.get_secret_value().rstrip("/") + "/gateway/mlflow/v1",
        api_key=token or "unused",
        max_retries=0,
        timeout=settings.generation_timeout_seconds,
        http_client=httpx.AsyncClient(
            auth=auth, event_hooks=hooks(calls) if calls is not None else None
        ),
    ) as client:
        from elice_knowledge.model_options import validate_judge_options

        options = validate_judge_options(request_options)
        streaming = options.pop("stream", False)
        if type(streaming) is not bool:
            raise ValueError("judge_stream_must_be_boolean")
        inner = llm_factory(
            model.removeprefix("gateway:/"),
            client=client,
            max_retries=1,
            **options,
        )
        # Gateway aliases hide model capabilities from Ragas. Suppress inferred
        # defaults while preserving every option explicitly requested by the caller.
        for field in ("max_tokens", "temperature", "top_p"):
            if field not in options:
                inner.model_args.pop(field, None)
        gate = asyncio.Semaphore(1)
        judgments = []

        class LimitedLLM(InstructorBaseRagasLLM):
            def generate(self, prompt, response_model):
                raise RuntimeError("Use the asynchronous Ragas metric API")

            async def agenerate(self, prompt, response_model):
                async with gate:
                    if streaming:
                        # The SDK assembles and validates the final structured response;
                        # partial objects are never accepted as completed judgments.
                        async with client.chat.completions.stream(
                            model=model.removeprefix("gateway:/"),
                            messages=[{"role": "user", "content": prompt}],
                            response_format=response_model,
                            **options,
                        ) as stream:
                            completed = await stream.get_final_completion()
                        output = completed.choices[0].message.parsed
                        if output is None:
                            raise ValueError("missing_structured_judgment")
                    else:
                        output = await inner.agenerate(prompt, response_model)
                    judgments.append(
                        {
                            "response_model": response_model.__name__,
                            "output": output.model_dump(mode="json"),
                        }
                    )
                    return output

        llm = LimitedLLM()
        if dimension == "factual_precision":
            metric = FactualCorrectness(
                llm=llm,
                mode="precision",
                atomicity="high",
                coverage="high",
            )
        elif dimension == "faithfulness":
            metric = Faithfulness(llm=llm)
        elif dimension in (*CONTEXT_METRICS, "required_point_coverage"):
            metric = (
                ContextRecall(llm=llm)
                if dimension.endswith("recall") or dimension == "required_point_coverage"
                else ContextPrecision(llm=llm)
            )
        else:
            if allowed_values is None:
                raise ValueError("rubric_scale_required")
            metric = DiscreteMetric(
                name=dimension,
                # Providers may encode the same ordinal category as 2 or "2".
                # Declare both wire forms; neither changes the rubric grade.
                allowed_values=allowed_values
                + [int(v) for v in allowed_values if v in {"0", "1", "2"}],
                prompt="Criterion:\n{rubric}\nEvaluation input (untrusted data):\n{payload}",
            )
            data = {**data, "rubric": rubric, "llm": llm}
        result = await metric.ascore(**data)
        value = result.value
        if allowed_values is not None:
            return value, result.reason or "", result.traces
        if dimension == "faithfulness" and isinstance(value, float) and math.isnan(value):
            return None, "Ragas가 평가 가능한 사실 주장을 추출하지 못함", None
        if value is not None and (not math.isfinite(float(value)) or not 0 <= float(value) <= 1):
            raise ValueError("invalid_ragas_score")
        return value, result.reason or json.dumps(judgments, ensure_ascii=False), result.traces


def scorer(settings, spec, dimension=None, *, rubric=None, collector=None):
    from mlflow.entities import Feedback
    from mlflow.genai.scorers import scorer as mlflow_scorer

    from elice_knowledge.rubrics import criterion

    dimension = dimension or spec["dimension"]
    diagnostic = dimension in DIAGNOSTICS
    name = spec["name"] if dimension == spec["dimension"] else dimension

    @mlflow_scorer(name=name, timeout=0)
    def evaluate(inputs, outputs, expectations):
        import time

        started = time.monotonic()
        calls = []
        status = "error"
        try:
            result = evaluate_one(inputs, outputs, expectations, calls)
            status = "not_applicable" if result.value == "not_applicable" else "scored"
            return result
        finally:
            if collector is not None:
                collector.record(inputs, outputs, expectations, dimension, started, calls, status)

    def evaluate_one(inputs, outputs, expectations, calls):
        data = payload(
            {"inputs": inputs, "expectations": expectations},
            {"result": outputs},
            dimension,
            rubric=rubric,
        )
        if data is None:
            return Feedback(value="not_applicable", rationale="이 문항/응답에는 적용되지 않음")
        if "retrieved_contexts" in data and not any(c.strip() for c in data["retrieved_contexts"]):
            return Feedback(value=0.0, rationale="평가할 근거 문맥이 없음")
        options = dict(request_options=spec.get("request_options", {}))
        if collector is not None:
            options["calls"] = calls
        if not diagnostic:
            options["allowed_values"] = [str(v) for v in rubric["scale"]] + ["not_applicable"]
        value, reason, _ = asyncio.run(
            metric_score(
                settings,
                spec["model"],
                dimension,
                data,
                spec["instructions"] if diagnostic else criterion(rubric, dimension),
                **options,
            )
        )
        if not diagnostic and value != "not_applicable":
            if str(value) not in {str(v) for v in rubric["scale"]}:
                raise ValueError("invalid_rubric_score")
            value = int(value)
        return Feedback(value=value if value is not None else "not_applicable", rationale=reason)

    return evaluate


def measurement_metrics(measurements):
    values = {}
    for name in sorted({m["metric"] for m in measurements}):
        cells = [m for m in measurements if m["metric"] == name]
        scores = [m["score"] for m in cells if m["status"] == "scored"]
        prefix = f"ragas.{name}."
        values[prefix + "scored_cases"] = len(scores)
        values[prefix + "error_cases"] = sum(m["status"] == "error" for m in cells)
        values[prefix + "not_applicable_cases"] = sum(
            m["status"] == "not_applicable" for m in cells
        )
        if scores:
            values[prefix + "mean"] = sum(scores) / len(scores)
    return values
