"""One QA execution path for live conversations and evaluation records."""

import time

from elice_knowledge.agent import run_qa
from elice_knowledge.prompts import from_profile
from elice_knowledge.qa import QARequest
from elice_knowledge.service_store import PROFILE_FIELDS, ServiceError


def execute(store, profile_key, question, history=None):
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
    try:
        result = run_qa(settings, request, from_profile(config)).model_dump()
        expected = settings.generation_model
        actual = result.get("generation_model")
        if actual and actual.removeprefix("openai/") != expected.removeprefix("openai/"):
            raise ServiceError("generation_model_changed", 409)
    except Exception as exc:
        error = getattr(exc, "code", type(exc).__name__)
    elapsed = (time.monotonic() - started) * 1000
    run_id = store.save_run(
        profile_key, request.model_dump(), result if not error else None, error, elapsed
    )
    return {
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
