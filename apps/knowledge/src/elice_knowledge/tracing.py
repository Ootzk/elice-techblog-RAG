"""Optional child spans: actual application data, no configuration objects or credentials."""

import logging
from contextlib import contextmanager
from threading import Lock

_lock = Lock()
_graph_enabled = False
_logger = logging.getLogger(__name__)


def finish_span(span, outputs=None, attributes=None):
    if span is None:
        return
    try:
        if outputs is not None:
            span.set_outputs(outputs)
        if attributes:
            span.set_attributes(attributes)
    except Exception:
        _logger.warning("Child span update failed", exc_info=False)


def active_attributes(attributes):
    import mlflow

    finish_span(mlflow.get_current_active_span(), attributes=attributes)


@contextmanager
def graph_tracing(settings):
    import mlflow

    global _graph_enabled
    if not settings.mlflow_enabled or mlflow.get_current_active_span() is None:
        # Autologging is process-wide; suppress this request without affecting others.
        with mlflow.tracing.context(enabled=False):
            yield
        return
    try:
        with _lock:
            if not _graph_enabled:
                # Own LLM/retrieval semantics explicitly; avoid duplicate LlamaIndex spans.
                mlflow.llama_index.autolog(disable=True, silent=True)
                mlflow.langchain.autolog(run_tracer_inline=True)
                _graph_enabled = True
    except Exception:
        _logger.warning("LangGraph tracing setup failed", exc_info=False)
    yield


@contextmanager
def child_span(name, span_type, inputs, attributes=None):
    import mlflow

    if mlflow.get_current_active_span() is None:
        yield None
        return
    manager, span = None, None
    try:
        manager = mlflow.start_span(name=name, span_type=span_type, attributes=attributes)
        span = manager.__enter__()
        span.set_inputs(inputs)
    except Exception:
        _logger.warning("Child span setup failed", exc_info=False)
    try:
        yield span
    except BaseException as exc:
        if span is not None:
            try:
                span.set_status("ERROR")
                span.set_attribute("error.code", getattr(exc, "code", type(exc).__name__))
            except Exception:
                pass
        raise
    finally:
        if span is not None:
            try:
                # Do not export exception bodies, which can contain upstream credentials.
                manager.__exit__(None, None, None)
            except Exception:
                _logger.warning("Child span export failed", exc_info=False)


def documents(sources):
    return [
        {
            "id": source["chunk_id"],
            "page_content": source["text"],
            "metadata": {key: value for key, value in source.items() if key != "text"},
        }
        for source in sources
    ]
