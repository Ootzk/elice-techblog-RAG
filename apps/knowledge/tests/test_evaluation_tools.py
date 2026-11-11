import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from elice_knowledge.evaluation_bundle import create_bundle


def load_tool(name):
    path = Path(__file__).resolve().parents[3] / "tools" / "evaluation" / (name + ".py")
    spec = importlib.util.spec_from_file_location("tool_" + name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_export_is_blind_does_not_overwrite_packet_and_leaves_no_downloads(tmp_path, monkeypatch):
    tool = load_tool("review")
    records = [dict(id="r", inputs={"question": "Q"}, expectations={"answerable": False})]
    cases = [
        dict(
            record_id="r",
            error=None,
            result=dict(
                status="insufficient_evidence",
                answer="no evidence",
                generation_sources=[],
                generation_model="SECRET-GENERATOR",
            ),
        )
    ]
    bundle = create_bundle(
        "source", dict(dataset_id="d", dataset_digest="h", records=records), {}, cases
    )

    def download(run_id, name, scratch):
        path = Path(scratch) / name
        path.write_text(json.dumps(bundle))
        return str(path)

    monkeypatch.setattr(tool, "configure", lambda *a: "exp")
    monkeypatch.setattr(tool, "client", lambda *a: SimpleNamespace(download_artifacts=download))
    monkeypatch.setattr(
        "sys.argv", ["review", "export", "--source-run-id", "source", "--output-dir", str(tmp_path)]
    )
    tool.main()
    assert {p.name for p in tmp_path.iterdir()} == {"review-packet.json"}
    assert "SECRET-GENERATOR" not in (tmp_path / "review-packet.json").read_text()
    original = (tmp_path / "review-packet.json").read_text()
    with pytest.raises(FileExistsError):
        tool.main()
    assert (tmp_path / "review-packet.json").read_text() == original
