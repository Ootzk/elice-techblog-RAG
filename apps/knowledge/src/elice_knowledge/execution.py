"""One QA execution path for live conversations and evaluation records."""

import logging
import time
from contextlib import contextmanager

from elice_knowledge.agent import run_qa
from elice_knowledge.prompts import from_profile
from elice_knowledge.qa import QARequest
from elice_knowledge.service_store import PROFILE_FIELDS, ServiceError


@contextmanager
def qa_trace(settings, request, profile_key, mode):
    """A single application boundary; no credentials or Settings are exported."""
    if not settings.mlflow_enabled or not settings.mlflow_tracking_uri:
        import mlflow

        with mlflow.tracing.context(enabled=False):
            yield None
        return
    import mlflow
    from mlflow.entities import MlflowExperimentLocation

    from elice_knowledge.judges import configure

    context, span = None, None
    try:
        experiment_id = configure(settings)
        context = mlflow.start_span(
            name="techblog-qa",
            span_type="CHAIN",
            trace_destination=MlflowExperimentLocation(experiment_id=experiment_id),
        )
        span = context.__enter__()
        span.set_inputs(
            {"question": request.question, "history": [m.model_dump() for m in request.history]}
        )
        mlflow.update_current_trace(
            metadata={
                "environment": settings.app_env,
                "evaluation_mode": mode,
                "qa_profile_key": profile_key,
            }
        )
    except Exception:
        logging.getLogger(__name__).warning("QA trace setup failed", exc_info=False)
    try:
        yield span
    finally:
        if span is not None:
            try:
                context.__exit__(None, None, None)
            except Exception:
                logging.getLogger(__name__).warning("QA trace export failed", exc_info=False)


def execute(store, profile_key, question, history=None, *, mode="online"):
    config = store.profile(profile_key)
    settings = store.settings.model_copy(update={name: config[name] for name in PROFILE_FIELDS})
    request = QARequest(
        question=question,
        index_key=config["index_key"],
        top_k=config["top_k"],
        history=history or [],
    )
    started = time.monotonic()
    error, result = None, None
    trace_id = None
    with qa_trace(settings, request, profile_key, mode) as span:
        if span:
            trace_id = span.trace_id
        try:
            result = run_qa(settings, request, from_profile(config)).model_dump()
            expected = settings.generation_model
            actual = result.get("generation_model")
            if actual and actual.removeprefix("openai/") != expected.removeprefix("openai/"):
                raise ServiceError("generation_model_changed", 409)
        except Exception as exc:
            error = getattr(exc, "code", type(exc).__name__)
        if span:
            try:
                span.set_outputs(result if not error else {"error": error})
                if error:
                    span.set_status("ERROR")
            except Exception:
                logging.getLogger(__name__).warning("QA trace output failed", exc_info=False)
    elapsed = (time.monotonic() - started) * 1000
    run_id = store.save_run(
        profile_key, request.model_dump(), result if not error else None, error, elapsed
    )
    return {
        "trace_id": trace_id,
        "run_id": run_id,
        "duration_ms": elapsed,
        "result": result if not error else None,
        "error": error,
    }


def public_result(execution):
    if not execution:
        return None
    result = execution.get("result")
    if not result:
        return None
    return {key: result[key] for key in ["status", "answer", "citations", "reason"]}
