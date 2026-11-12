import json
from copy import deepcopy
from pathlib import Path

from elice_reporting.data import ordered_records


def test_existing_run_matches_full_source_record_and_preserves_order():
    path = Path(__file__).resolve().parents[2] / "evaluation/gold_set/gold.jsonl"
    source = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    records = [
        dict(id=str(i), inputs=row["inputs"], expectations=row["expectations"])
        for i, row in enumerate(source[:3])
    ]
    bundle = {"kind": "gold", "records": list(reversed(records))}
    assert [row["id"] for row in ordered_records(bundle)] == ["0", "1", "2"]
    changed = deepcopy(bundle)
    changed["records"][0]["expectations"]["answer"] += " 변경"
    assert ordered_records(changed) == changed["records"]


def test_saved_order_is_used_without_repository_lookup():
    bundle = {"records": [{"id": "b", "source_order": 4}, {"id": "a", "source_order": 1}]}
    assert [r["id"] for r in ordered_records(bundle)] == ["a", "b"]
