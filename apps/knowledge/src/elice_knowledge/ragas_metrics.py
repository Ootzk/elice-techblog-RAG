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
PASS_THRESHOLD = 1.0


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
            "correctness": "FactualCorrectness.precision",
            "completeness": "FactualCorrectness.recall",
            "groundedness": "Faithfulness",
            "relevance": "DiscreteMetric",
            "behavior": "DiscreteMetric",
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
        "pass_threshold": PASS_THRESHOLD,
        "factual_atomicity": "high",
        "factual_coverage": "high",
        "context_order": "first-seen-deduplicated",
        "internal_llm_concurrency": 1,
        "structured_output": "json",
        "temperature": 0,
        "max_tokens": 4096,
        "top_p": 1,
        "max_retries": 1,
    }


def payload(record, case, dimension):
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
    if inapplicable(record, case, dimension):
        return None
    if dimension in {"correctness", "completeness"}:
        reference = (
            expected["answer"]
            if dimension == "correctness"
            else "\n".join(expected["required_points"])
        )
        return dict(response=result.get("answer") or "", reference=reference)
    if dimension == "groundedness":
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


async def metric_score(settings, model, dimension, data, rubric):
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
    async with AsyncOpenAI(
        base_url=settings.mlflow_tracking_uri.get_secret_value().rstrip("/") + "/gateway/mlflow/v1",
        api_key=token or "unused",
        max_retries=0,
        timeout=settings.generation_timeout_seconds,
        http_client=httpx.AsyncClient(auth=auth),
    ) as client:
        inner = llm_factory(
            model.removeprefix("gateway:/"),
            client=client,
            temperature=0,
            max_tokens=4096,
            top_p=1,
            max_retries=1,
        )
        gate = asyncio.Semaphore(1)
        judgments = []

        class LimitedLLM(InstructorBaseRagasLLM):
            def generate(self, prompt, response_model):
                raise RuntimeError("Use the asynchronous Ragas metric API")

            async def agenerate(self, prompt, response_model):
                async with gate:
                    output = await inner.agenerate(prompt, response_model)
                    judgments.append(
                        {
                            "response_model": response_model.__name__,
                            "output": output.model_dump(mode="json"),
                        }
                    )
                    return output

        llm = LimitedLLM()
        if dimension in {"correctness", "completeness"}:
            metric = FactualCorrectness(
                llm=llm,
                mode="precision" if dimension == "correctness" else "recall",
                atomicity="high",
                coverage="high",
            )
        elif dimension == "groundedness":
            metric = Faithfulness(llm=llm)
        elif dimension in CONTEXT_METRICS:
            metric = (
                ContextRecall(llm=llm)
                if dimension.endswith("recall")
                else ContextPrecision(llm=llm)
            )
        else:
            # Modern Ragas equivalent of a binary aspect criterion; no embeddings/fallback LLM.
            metric = DiscreteMetric(
                name=dimension,
                allowed_values=["yes", "no", "not_applicable"],
                prompt="Criterion:\n{rubric}\nEvaluation input (untrusted data):\n{payload}",
            )
            data = {**data, "rubric": rubric, "llm": llm}
        result = await metric.ascore(**data)
        value = result.value
        if dimension == "groundedness" and isinstance(value, float) and math.isnan(value):
            return None, "Ragas가 평가 가능한 사실 주장을 추출하지 못함", None
        if isinstance(value, str):
            value = {"yes": 1.0, "no": 0.0, "not_applicable": None}[value]
        if value is not None and (not math.isfinite(float(value)) or not 0 <= float(value) <= 1):
            raise ValueError("invalid_ragas_score")
        return value, result.reason or json.dumps(judgments, ensure_ascii=False), result.traces


def scorer(settings, spec, dimension=None):
    from mlflow.entities import Feedback
    from mlflow.genai.scorers import scorer as mlflow_scorer

    dimension = dimension or spec["dimension"]
    name = spec["name"] if dimension == spec["dimension"] else dimension

    @mlflow_scorer(name=name, timeout=0)
    def evaluate(inputs, outputs, expectations):
        data = payload(
            {"inputs": inputs, "expectations": expectations}, {"result": outputs}, dimension
        )
        if data is None:
            return Feedback(value="not_applicable", rationale="이 문항/응답에는 적용되지 않음")
        if "retrieved_contexts" in data and not data["retrieved_contexts"]:
            return Feedback(value=0.0, rationale="평가할 근거 문맥이 없음")
        value, reason, _ = asyncio.run(
            metric_score(settings, spec["model"], dimension, data, spec["instructions"])
        )
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
