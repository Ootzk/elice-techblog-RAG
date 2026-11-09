import copy
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

MODULE_PATH = Path(__file__).resolve().parents[3] / "tools/evaluation/register.py"
spec = importlib.util.spec_from_file_location("register_datasets", MODULE_PATH)
registration = importlib.util.module_from_spec(spec)
spec.loader.exec_module(registration)


def test_repository_datasets_and_duplicate_inputs(tmp_path):
    for kind, count in [("gold", 22), ("iron", 8)]:
        path = registration.ROOT / f"{kind}_set/{kind}.jsonl"
        records = registration.load_records(path, kind)
        assert len(records) == count
        assert records[0]["tags"]["source_id"] == f"{kind}-001"

    source = registration.ROOT / "gold_set/gold.jsonl"
    row = json.loads(source.read_text().splitlines()[0])
    duplicate = copy.deepcopy(row)
    duplicate["id"] = "another-id"
    path = tmp_path / "duplicates.jsonl"
    path.write_text(json.dumps(row) + "\n" + json.dumps(duplicate))
    with pytest.raises(ValueError, match="line 2"):
        registration.load_records(path, "gold")


def test_invalid_reference_does_not_pass_validation(tmp_path):
    row = json.loads((registration.ROOT / "gold_set/gold.jsonl").read_text().splitlines()[0])
    row["expectations"]["evidence"] = []
    path = tmp_path / "invalid.jsonl"
    path.write_text(json.dumps(row))
    with pytest.raises(ValueError, match="line 1"):
        registration.load_records(path, "gold")


class Dataset:
    def __init__(self):
        self.dataset_id = "dataset-1"
        self.experiment_ids = ["exp"]
        self.records = []
        self.next_id = 0

    def delete_records(self, ids):
        self.records = [r for r in self.records if r.dataset_record_id not in ids]

    def merge_records(self, records):
        for row in records:
            existing = next((r for r in self.records if r.inputs == row["inputs"]), None)
            if existing:
                existing.expectations.update(row["expectations"])
                existing.tags.update(row["tags"])
            else:
                self.next_id += 1
                self.records.append(
                    SimpleNamespace(dataset_record_id=str(self.next_id), **copy.deepcopy(row))
                )


class Tracking:
    def __init__(self):
        self.dataset = None

    def search_datasets(self, **kwargs):
        return [self.dataset] if self.dataset else []

    def create_dataset(self, *args, **kwargs):
        self.dataset = Dataset()
        return self.dataset

    def get_dataset(self, dataset_id):
        return self.dataset

    def add_dataset_to_experiments(self, *args):
        pytest.fail("Already-associated datasets must not be associated again")

    def set_dataset_tags(self, dataset_id, tags):
        self.tags = tags


def test_reregistration_reuses_dataset_and_removes_stale_records_and_expectations():
    tracking = Tracking()
    records = [
        {
            "inputs": {"question": "kept"},
            "expectations": {"answerable": False, "old": 1},
            "tags": {"source_id": "gold-001"},
        },
        {
            "inputs": {"question": "removed"},
            "expectations": {"answerable": False},
            "tags": {"source_id": "gold-002"},
        },
    ]
    first = registration.register(tracking, "exp", "gold", records, {})
    ids = [r.dataset_record_id for r in tracking.dataset.records]
    again = registration.register(tracking, "exp", "gold", records, {})
    assert first["dataset_id"] == again["dataset_id"]
    assert again["written"] == again["removed"] == 0
    assert [r.dataset_record_id for r in tracking.dataset.records] == ids

    records = copy.deepcopy(records[:1])
    del records[0]["expectations"]["old"]
    records.append(
        {
            "inputs": {"question": "new question"},
            "expectations": {"answerable": False},
            "tags": {"source_id": "gold-003"},
        }
    )
    updated = registration.register(tracking, "exp", "gold", records, {})
    assert updated["dataset_id"] == first["dataset_id"]
    assert updated["written"] == 2 and updated["removed"] == 1
    assert {r.inputs["question"] for r in tracking.dataset.records} == {"kept", "new question"}
    assert all("old" not in r.expectations for r in tracking.dataset.records)
