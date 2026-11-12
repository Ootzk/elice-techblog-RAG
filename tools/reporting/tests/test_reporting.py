import json
import shutil
import subprocess
from copy import deepcopy
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace

import nbformat
import pandas as pd
import panel as pn
import pytest
from nbclient import NotebookClient

from elice_reporting import (
    agreement_frames,
    case_view,
    comparison_frames,
    demo,
    export_html,
    from_files,
    from_mlflow,
    interactive_table,
    report_purpose_callout,
    text_block,
)
from elice_reporting.export import TABLE_CALLBACK

ROOT = Path(__file__).resolve().parents[3]
PACKAGE = Path(__file__).resolve().parents[1]


def test_report_purpose_callouts_explain_each_report_scope():
    agreement = report_purpose_callout("agreement").object
    repeatability = report_purpose_callout("repeatability").object
    comparison = report_purpose_callout("comparison").object
    assert "AGREEMENT" in agreement and "같은 평가자가 다시 채점해도" in agreement
    assert "REPEATABILITY" in repeatability and "채점 자체의 흔들림" in repeatability
    assert "COMPARISON" in comparison and "답변을 각각 여러 번 만든 결과" in comparison


def test_demo_tables_preserve_unavailable_coefficients_and_rater_identity():
    data = demo("agreement")
    frames = agreement_frames(data)
    assert data.source == "demo" and data.report["demo"]
    assert len(frames["cases"]) == 4 and not frames["labels"].empty
    assert (frames["pairs"]["purpose"] == "human_alignment").any()
    assert frames["pairs"]["cohen_kappa"].min() >= -1
    data.report["comparisons"][0]["cohen_kappa"] = None
    data.report["comparisons"][0]["kappa_undefined_reason"] = "constant labels"
    undefined = agreement_frames(data)["pairs"].iloc[0]
    assert pd.isna(undefined["cohen_kappa"])
    assert undefined["undefined_reason"] == "constant labels"
    assert frames["labels"]["rater"].nunique() == 3
    compare = comparison_frames(demo("comparison"))
    assert compare["quality"]["delta_mean"].max() > 0
    assert not compare["transitions"].empty
    assert {"record_id", "rater", "metric"} <= set(compare["transitions"].columns)


def test_readable_case_uses_generation_context_and_escapes_model_text():
    import panel as pn

    bundle = deepcopy(demo("agreement").inputs["bundle"])
    bundle["cases"][0]["result"]["answer"] = "<script>bad()</script>"
    view = case_view(bundle, "0", title="문항 0")
    html = "\n".join(pane.object for pane in view.select(pn.pane.HTML))
    assert "&lt;script&gt;bad()&lt;/script&gt;" in html
    context = view.objects[-1].objects[0].objects[0].object
    assert "fact" in context and "unseen material" not in context
    assert view.select(pn.Accordion)[0].active == []
    assert "&lt;b&gt;" in text_block("title", "<b>").object


def test_old_grading_contract_is_rejected(tmp_path):
    data = demo("agreement")
    data.inputs["gradings"][1].pop("contract")
    report, inputs = tmp_path / "agreement.json", tmp_path / "inputs.json"
    report.write_text(json.dumps(data.report))
    inputs.write_text(json.dumps(data.inputs))
    with pytest.raises(ValueError, match="rubric-v1"):
        from_files("agreement", report, inputs)


def test_previous_analysis_artifact_is_rejected(tmp_path):
    data = demo("agreement")
    data.report["analysis_protocol"] = data.report.pop("agreement_protocol")
    report, inputs = tmp_path / "agreement.json", tmp_path / "inputs.json"
    report.write_text(json.dumps(data.report))
    inputs.write_text(json.dumps(data.inputs))
    with pytest.raises(ValueError, match="Agreement protocol"):
        from_files("agreement", report, inputs)


def test_report_bindings_reject_edited_inputs(tmp_path):
    data = demo("comparison")
    report = tmp_path / "comparison.json"
    inputs = tmp_path / "inputs.json"
    report.write_text(json.dumps(data.report))
    inputs.write_text(json.dumps(data.inputs))
    assert from_files("comparison", report, inputs).kind == "comparison"
    tampered = deepcopy(data.inputs)
    tampered["baseline"]["bundle"]["kind"] = "other"
    inputs.write_text(json.dumps(tampered))
    with pytest.raises(ValueError, match="input digest"):
        from_files("comparison", report, inputs)


def test_comparison_rejects_previous_analysis_run_references(tmp_path):
    from elice_reporting.data import _digest

    data = demo("comparison")
    for side in ("baseline", "candidate"):
        identity = data.inputs[side].pop("agreement_run_id")
        data.inputs[side]["analysis_run_id"] = identity
        data.inputs[side]["analysis"] = data.inputs[side].pop("agreement")
        data.report[side]["analysis_run_id"] = data.report[side].pop("agreement_run_id")
        data.report[side]["input_digest"] = _digest(data.inputs[side])
    data.report["digest"] = _digest({k: v for k, v in data.report.items() if k != "digest"})
    report, inputs = tmp_path / "comparison.json", tmp_path / "inputs.json"
    report.write_text(json.dumps(data.report))
    inputs.write_text(json.dumps(data.inputs))
    with pytest.raises(ValueError, match="agreement run references"):
        from_files("comparison", report, inputs)


@pytest.mark.parametrize("kind", ["agreement", "comparison"])
def test_mlflow_loading_is_read_only_and_uses_frozen_artifacts(kind, tmp_path, monkeypatch):
    fixture = demo(kind)
    reads = []
    report = fixture.report

    def run(key):
        tags = {
            "evaluation.kind": kind if key == "report-run" else "answers",
            "evaluation.schema": "evaluation-v2",
            "source_bundle_digest": report.get("source_digest"),
            "comparison_digest": report.get("digest"),
        }
        return SimpleNamespace(
            info=SimpleNamespace(status="FINISHED"), data=SimpleNamespace(tags=tags)
        )

    def download(key, name, directory):
        reads.append((key, name))
        if name == "evaluation-bundle.json":
            value = fixture.inputs["bundle"]
        elif name == "grading-inputs.json":
            value = {"results": fixture.inputs["gradings"]}
        elif name == "comparison-inputs.json":
            value = fixture.inputs
        else:
            value = report
        path = Path(directory) / name
        path.write_text(json.dumps(value))
        return str(path)

    monkeypatch.setattr(
        "mlflow.MlflowClient",
        lambda **kw: SimpleNamespace(get_run=run, download_artifacts=download),
    )
    loaded = from_mlflow(kind, "report-run", tmp_path)
    assert loaded.report == report and loaded.inputs == fixture.inputs
    assert len(reads) == (3 if kind == "agreement" else 2)


class Resources(HTMLParser):
    def __init__(self):
        super().__init__()
        self.remote = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in {"script", "link"}:
            value = attrs.get("src", attrs.get("href", ""))
            if value.startswith(("http://", "https://", "//")):
                self.remote.append(value)


def test_table_callbacks_and_export_are_standalone(tmp_path):
    from bokeh.models import DataTable, TextInput

    table = interactive_table(
        pd.DataFrame({"question": ["<script>alert(1)</script>", "검색"], "value": [None, 0.5]})
    )
    root = table.get_root()
    assert not list(root.select({"type": DataTable}))
    search = list(root.select({"type": TextInput}))[0]
    assert search.js_property_callbacks["change:value"]
    path = export_html(table, tmp_path / "report.html")
    document = path.read_text()
    resources = Resources()
    resources.feed(document)
    assert not resources.remote
    assert "table.text = escape(markup)" in document and "bokeh" in document.lower()
    with pytest.raises(FileExistsError):
        export_html(table, path)
    export_html(table, path, overwrite=True)


def test_exported_table_search_sort_reset_and_escaping_without_python_callbacks():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required to execute the browser-side table callback")
    setup = {
        "columns": ["question", "score"],
        "rows": [["<script>bad()</script>", 10], ["검색", 2], ["누락", None]],
        "search": {"value": ""},
        "sort": {"value": "score"},
        "descending": {"active": False},
        "table": {"text": ""},
    }
    program = "\n".join(f"const {k} = {json.dumps(v)};" for k, v in setup.items())
    program += "\nfunction update() {" + TABLE_CALLBACK + "}\n"
    program += """
    const results = [];
    update(); results.push(table.text);
    search.value = '검색'; update(); results.push(table.text);
    search.value = ''; descending.active = true; update(); results.push(table.text);
    sort.value = '원본 순서'; update(); results.push(table.text);
    console.log(JSON.stringify(results));
    """
    result = subprocess.run([node, "-e", program], capture_output=True, text=True, check=True)
    ascending, filtered, descending, original = [unescape(v) for v in json.loads(result.stdout)]
    assert ascending.index("검색") < ascending.index("&lt;script&gt;") < ascending.index("누락")
    assert "1 / 3행" in filtered and "검색" in filtered and "누락" not in filtered
    assert descending.index("&lt;script&gt;") < descending.index("검색") < descending.index("누락")
    assert original.index("&lt;script&gt;") < original.index("검색") < original.index("누락")
    assert "<script>bad()" not in original


def test_comparison_template_uses_repeated_condition_contract():
    path = PACKAGE / "notebooks/templates/comparison.ipynb"
    notebook = nbformat.read(path, as_version=4)
    nbformat.validate(notebook)
    source = "\n".join(cell.source for cell in notebook.cells)
    assert "comparison-v2" in source
    assert "quality_view(data, subjects)" in source
    assert "stability_view(data, subjects)" in source
    assert source.index("## 2. 지표 정의와 판정 기준") < source.index("## 3. 결과 분석")
    assert "비교 가설" not in source
    for cell in notebook.cells:
        if cell.cell_type == "code":
            assert cell.execution_count is None and not cell.outputs
            compile(cell.source, str(path), "exec")


@pytest.mark.parametrize("kind, judge_mode", [("agreement", False)])
def test_template_executes_and_exports_without_network_or_saved_outputs(
    kind, judge_mode, tmp_path, monkeypatch
):
    path = PACKAGE / "notebooks/templates" / f"{kind}.ipynb"
    notebook = nbformat.read(path, as_version=4)
    nbformat.validate(notebook)
    source = "\n".join(cell.source for cell in notebook.cells)
    for token in (
        "KEY_FINDING",
        "INTERPRETATION",
        "RESEARCH_QUESTION",
        "작성자의 핵심 해석",
        "[작성 필요]",
    ):
        assert token not in source
    assert not notebook.metadata.get("widgets")
    for cell in notebook.cells:
        if cell.cell_type == "code":
            assert cell.execution_count is None and not cell.outputs
            compile(cell.source, str(path), "exec")
    output = tmp_path / f"{kind}.html"
    notebook.cells[2].source += '\nSOURCE_MODE = "demo"\nOUTPUT_HTML = ROOT / ' + repr(str(output))
    if judge_mode:
        notebook.cells[4].source += '\ndata.report["purpose"] = "judge"'
    # The kernel inherits this environment but templates use demo mode, not MLflow.
    monkeypatch.setenv("MLFLOW_TRACKING_URI", "http://127.0.0.1:1/should-never-be-used")
    notebook.cells.append(
        nbformat.v4.new_code_cell(
            (
                "assert not report_view.select(pn.Accordion)\n"
                if kind == "agreement"
                else "assert details.active == []\n"
            )
            + (
                "assert focus is not None and not chart_data.empty\n"
                if kind == "comparison"
                else ""
            )
            + "assert case_ids == list(available_ids)\n"
            + (
                "assert len(examples_view.objects[2:]) == len(available_ids)\n"
                if kind == "agreement"
                else "assert len(examples) == len(available_ids)\n"
            )
            + "headings = [p.object for p in report_view if isinstance(p, pn.pane.Markdown)]\n"
            'assert [h[3] for h in headings if h.startswith("## ") and h[3:4].isdigit()] '
            + '== ["1", "2", "3", "4", "5", "6"]'
        )
    )
    NotebookClient(
        notebook, timeout=120, kernel_name="python3", resources={"metadata": {"path": str(ROOT)}}
    ).execute()
    assert output.exists() and output.stat().st_size > 10000
    html = output.read_text()
    resources = Resources()
    resources.feed(html)
    assert not resources.remote
    for token in ("작성자의 핵심 해석", "한계와 다음 확인", "판단할 질문", "[작성 필요]"):
        assert token not in html and token.encode("unicode_escape").decode() not in html
    assert "실제 실험 결과가 아닙니다" in html or (
        "실제 실험 결과가 아닙니다".encode("unicode_escape").decode() in html
    )
    assert not any(
        out.output_type == "error"
        for cell in notebook.cells
        if cell.cell_type == "code"
        for out in cell.outputs
    )


def test_agreement_rejects_mixed_rubric_snapshots(tmp_path):
    data = demo("agreement")
    data.inputs["gradings"][1]["rubric_digest"] = "different-rubric"
    report, inputs = tmp_path / "agreement.json", tmp_path / "inputs.json"
    report.write_text(json.dumps(data.report))
    inputs.write_text(json.dumps(data.inputs))
    with pytest.raises(ValueError, match="same rubric"):
        from_files("agreement", report, inputs)


def test_efficiency_view_shows_measured_judge_round_and_question(tmp_path):
    from elice_reporting.agreement_visuals import agreement_subjects
    from elice_reporting.efficiency_sections import efficiency_view

    data = demo("agreement")
    subject = next(s for s in agreement_subjects(data) if s["rater"].startswith("model:"))
    record_id = data.inputs["bundle"]["records"][0]["id"]
    data.links["gradings"] = [
        {
            "rater": subject["rater"],
            "round": subject["round"],
            "profile_digest": subject["profile"],
            "efficiency": {
                "evaluation_elapsed_ms": 3000,
                "rows": [
                    {
                        "record_id": record_id,
                        "dimension": "correctness",
                        "status": "scored",
                        "scorer_duration_ms": 2000,
                        "calls": [
                            {
                                "duration_ms": 1500,
                                "input_tokens": 10,
                                "cached_input_tokens": 2,
                                "output_tokens": 4,
                                "total_tokens": 14,
                            }
                        ],
                    }
                ],
            },
        }
    ]
    view = efficiency_view(data)
    charts = [pane.object for pane in view.select(pn.pane.Bokeh)]
    sources = [
        renderer.data_source.data
        for chart in charts
        for renderer in chart.renderers
        if hasattr(renderer, "data_source")
    ]
    assert any("3.0초" in source.get("text", []) for source in sources)
    assert any("1.5초" in source.get("text", []) for source in sources)
    assert any("14" in source.get("text", []) for source in sources)
    assert not view.select(pn.pane.HTML)
    path = export_html(view, tmp_path / "efficiency.html", title="efficiency")
    assert path.is_file()


def test_efficiency_graph_weights_answers_equally_without_individual_points():
    from elice_reporting.efficiency_sections import _call_value, _chart

    points = [
        {"answer": 1, "attempt": "A1 · 1회차", "value": 0.0, "support": "관측"},
        {"answer": 1, "attempt": "A1 · 2회차", "value": 1.0, "support": "관측"},
        {"answer": 2, "attempt": "A2 · 1회차", "value": 1.0, "support": "관측"},
    ]
    pane = _chart([{"label": "Judge", "points": points}], "초", "시간", repeat=True)
    sources = [r.data_source.data for r in pane.object.renderers if hasattr(r, "data_source")]
    assert sources[0]["value"] == [0.75]
    assert len(sources) == 1
    assert (
        _call_value([{"input_tokens": 2}, {"input_tokens": None}], "input_tokens", "토큰")[0]
        is None
    )
    assert _call_value([{"input_tokens": 0}], "input_tokens", "토큰")[0] == 0


def test_efficiency_tool_counts_reconcile_steps():
    from elice_reporting.stacked_efficiency import tool_counts as _tool_counts

    result = {
        "tool_call_count": 2,
        "agent_steps": [
            {"action": "keyword_search"},
            {"action": "meaning_search", "error": "failed"},
            {"action": "answer"},
        ],
    }
    assert _tool_counts(result) == {"keyword_search": 1, "meaning_search": 1}
    assert _tool_counts({"tool_call_count": 1, "agent_steps": []}) is None
    assert _tool_counts({"tool_call_count": 0, "agent_steps": []}) == {
        "keyword_search": 0,
        "meaning_search": 0,
    }
