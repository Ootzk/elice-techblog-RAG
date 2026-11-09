from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from elice_knowledge.settings import Settings
from elice_knowledge.tracking import record_connectivity


@pytest.fixture
def tracking(monkeypatch):
    tracking = Mock()
    tracking.get_experiment_by_name.return_value = SimpleNamespace(
        experiment_id="project-experiment"
    )
    tracking.create_run.return_value = SimpleNamespace(info=SimpleNamespace(run_id="run-123"))
    monkeypatch.setattr("elice_knowledge.tracking.client", lambda _: tracking)
    return tracking


def test_sample_run_is_labeled_as_connectivity_and_records_vector_provenance(tracking, monkeypatch):
    monkeypatch.setattr("elice_knowledge.tracking.version", lambda name: "0.2.1")
    result = {
        "sample_id": "sample",
        "profile_id": "profile",
        "dimensions": 1024,
        "nearest_distance": 0.0,
    }
    run_id = record_connectivity(Settings(_env_file=None), result, {"digest": "model-digest"})
    assert run_id == "run-123"
    assert tracking.create_run.call_args.kwargs["tags"]["purpose"] == "connectivity-only"
    assert tracking.create_run.call_args.kwargs["tags"]["app.version"] == "0.2.1"
    tracking.log_param.assert_any_call(run_id, "embedding_model_digest", "model-digest")
    tracking.set_terminated.assert_called_once_with(run_id, "FINISHED")


def test_failed_metric_recording_cannot_be_reported_as_a_successful_run(tracking):
    tracking.log_metric.side_effect = RuntimeError("upstream failure")
    result = {
        "sample_id": "sample",
        "profile_id": "profile",
        "dimensions": 1024,
        "nearest_distance": 0.0,
    }
    with pytest.raises(RuntimeError):
        record_connectivity(Settings(_env_file=None), result, {"digest": "model-digest"})
    tracking.set_terminated.assert_called_once_with("run-123", "FAILED")
