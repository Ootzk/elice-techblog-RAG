from types import SimpleNamespace

import pytest

from elice_knowledge.evaluation import dataset_snapshot
from elice_knowledge.service_store import ServiceError
from elice_knowledge.settings import Settings


def test_native_dataset_contract_frozen_digest_and_strict_inputs(monkeypatch):
    from mlflow.entities.dataset_record import DatasetRecord

    record = DatasetRecord.from_dict(
        {
            "dataset_record_id": "record-1",
            "dataset_id": "dataset",
            "inputs": {"question": "질문", "history": []},
            "expectations": {"answerable": False},
            "tags": {"source_id": "gold-001"},
            "created_time": 1,
            "last_update_time": 1,
        }
    )
    dataset = SimpleNamespace(
        name="test-dataset", records=[record], tags={"source_order": '["gold-001"]'}
    )
    client = SimpleNamespace(get_dataset=lambda dataset_id: dataset)
    monkeypatch.setattr("elice_knowledge.evaluation.tracking_client", lambda *a: client)
    monkeypatch.setattr("mlflow.set_tracking_uri", lambda *a: None)
    settings = Settings(mlflow_tracking_uri="http://tracking.example")
    first = dataset_snapshot(settings, "dataset")
    assert first["records"][0]["id"] == "record-1"
    assert first["records"][0]["source_id"] == "gold-001"
    assert first["records"][0]["source_order"] == 0
    assert dataset_snapshot(settings, "dataset")["digest"] == first["digest"]
    record.expectations["answerable"] = True
    with pytest.raises(ServiceError, match="dataset_requires_evidence_quotes"):
        dataset_snapshot(settings, "dataset")
    record.expectations["evidence"] = [{"url": "https://elice.io/ko/test", "quote": "실제 근거"}]
    assert dataset_snapshot(settings, "dataset")["digest"] != first["digest"]


def test_iron_contract_and_mixed_rubrics_are_rejected(monkeypatch):
    records = [
        SimpleNamespace(
            dataset_record_id="iron",
            tags={},
            inputs={"question": "Q"},
            expectations={
                "expected_behavior": "deny_sensitive_information",
                "required_points": ["No secrets"],
                "failure_conditions": ["Discloses credentials"],
                "evidence": [],
            },
        )
    ]
    dataset = SimpleNamespace(name="iron", records=records, tags={})
    monkeypatch.setattr(
        "elice_knowledge.evaluation.tracking_client",
        lambda *a: SimpleNamespace(get_dataset=lambda _: dataset),
    )
    monkeypatch.setattr("mlflow.set_tracking_uri", lambda *a: None)
    settings = Settings(mlflow_tracking_uri="http://tracking.example")
    assert dataset_snapshot(settings, "dataset")["kind"] == "iron"
    records.append(
        SimpleNamespace(
            dataset_record_id="gold",
            inputs={"question": "Q"},
            expectations={"answerable": False},
            tags={},
        )
    )
    with pytest.raises(ServiceError, match="mixed_dataset_rubrics"):
        dataset_snapshot(settings, "dataset")
    records.pop()
    records[0].expectations["failure_conditions"] = []
    with pytest.raises(ServiceError, match="dataset_requires_iron_rubric"):
        dataset_snapshot(settings, "dataset")
