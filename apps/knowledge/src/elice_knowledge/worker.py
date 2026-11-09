"""Durable worker. Expired work is interrupted rather than automatically replayed."""

import threading
import time

from elice_knowledge.evaluation import run_evaluation
from elice_knowledge.execution import execute
from elice_knowledge.service_store import ServiceStore
from elice_knowledge.sparse import start_warm_up


def process_one(store):
    job = store.claim()
    if not job:
        return False
    stop = threading.Event()

    def renew():
        while not stop.wait(max(1, store.settings.worker_lease_seconds / 3)):
            try:
                store.heartbeat(job, "generating" if job["kind"] == "chat" else "evaluating")
            except Exception:
                return

    thread = threading.Thread(target=renew, daemon=True)
    thread.start()
    try:
        store.heartbeat(job, "generating" if job["kind"] == "chat" else "evaluating")
        if job["kind"] == "chat":
            output = execute(
                store,
                job["payload"]["profile_key"],
                job["payload"]["question"],
                store.history(job["subject_id"]),
            )
            store.finish(job, output, output["error"])
        else:
            output = run_evaluation(store, job)
            store.finish(job, output, "evaluation_cases_failed" if output["failed"] else None)
    except Exception as exc:
        store.finish(job, error=getattr(exc, "code", type(exc).__name__))
    finally:
        stop.set()
        thread.join(timeout=1)
    return True


def run_worker(settings, once=False):
    store = ServiceStore(settings)
    # Chat and evaluation QA run here; a job claimed meanwhile waits for the build.
    start_warm_up(settings)
    while True:
        store.cleanup()
        worked = process_one(store)
        if once:
            return
        if not worked:
            time.sleep(1)
