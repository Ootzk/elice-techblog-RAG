import json
import shutil
import subprocess
from copy import deepcopy
from types import SimpleNamespace

import panel as pn
import pytest

from elice_reporting import demo, from_mlflow
from elice_reporting.analysis_sections import (
    evaluation_conditions,
    item_browser,
    item_switch_code,
    metric_guide,
)
from elice_reporting.analysis_visuals import analysis_subjects, kappa_heatmaps, rater_legend
from elice_reporting.data import _digest


def test_selector_preloads_cases_and_shows_question_answer_without_detail_card():
    import holoviews as hv

    pn.extension()
    hv.extension("bokeh")
    data = demo("analysis")
    subjects = analysis_subjects(data)
    browser = item_browser(data, subjects, ["0", "1"])
    assert isinstance(browser.objects[0], pn.widgets.Select)
    first, second = browser.objects[2:]
    assert first.visible and not second.visible
    assert "질문" in first.objects[0].object
    assert data.inputs["bundle"]["cases"][0]["result"]["answer"] in first.objects[1].object
    assert not first.select(pn.Accordion)
    root = browser.get_root()
    from bokeh.models import Select

    select = next(s for s in root.select({"type": Select}) if s.title == "문항 선택")
    assert select.value == "0"
    assert select.js_property_callbacks["change:value"]


def test_dropdown_callback_switches_views_in_exported_html():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required for the client-side callback test")
    script = (
        """const item_0={visible:true}, item_1={visible:false}, cb_obj={value:'1'};
    function update(){"""
        + item_switch_code(2)
        + """}
    update(); const second = [item_0.visible,item_1.visible];
    cb_obj.value='0'; update();
    console.log(JSON.stringify([second,[item_0.visible,item_1.visible]]));"""
    )
    result = subprocess.run([node, "-e", script], capture_output=True, text=True, check=True)
    assert json.loads(result.stdout) == [[False, True], [True, False]]


def test_conditions_have_dataset_configuration_and_nested_legend_and_metric_definitions():
    data = demo("analysis")
    conditions = evaluation_conditions(data, analysis_subjects(data))
    text = "\n".join(pane.object for pane in conditions.select(pn.pane.HTML))
    assert "데이터셋 ID" in text and "데이터셋 내용 해시" in text
    assert "qa_agent_max_tool_calls" in text
    assert "1회차" in text and "실제 모델 ID 미기록" in text
    assert not conditions.select(pn.Accordion)
    assert "원본 answers" in text and "원본 gradings" not in text
    assert "원본 grading" in text
    definitions = metric_guide(data)
    definition_text = "\n".join(p.object for p in definitions.select(pn.pane.HTML))
    assert "생성" in definition_text
    assert all("<table" in p.object for p in definitions.objects if isinstance(p, pn.pane.HTML))
    headings = [
        p.object
        for p in definitions.objects
        if isinstance(p, pn.pane.Markdown) and p.object.startswith("### ")
    ]
    assert headings == ["### Grading · 공통 rubric 등급", "### Analysis · 평가자 간 일치도"]
    assert definitions.objects[1].object.startswith("- **등급:**")
    assert "- **판정:**" in definitions.objects[1].object
    assert "- **N/A·오류·미기록:**" in definitions.objects[1].object
    assert "- **진단 지표:**" in definitions.objects[1].object
    assert isinstance(definitions.objects[2], pn.pane.HTML)
    assert not definitions.select(pn.Accordion)
    assert "Fleiss" not in text and "우연 일치" in definition_text
    grid = kappa_heatmaps(data, analysis_subjects(data)).objects[0].objects[0]
    assert isinstance(grid, pn.GridBox) and grid.ncols == 2


def test_mlflow_profile_models_are_bound_to_frozen_grading_and_not_guessed(tmp_path, monkeypatch):
    data = deepcopy(demo("analysis"))
    grading = data.inputs["gradings"][2]
    old_digest = grading["rater_profile_digest"]
    profile = {
        "type": "model",
        "scorers": [{"model": "gateway:/judge-claude"}],
        "rubric": grading["rubric"],
        "rubric_digest": grading["rubric_digest"],
    }
    key = _digest(profile)
    grading["rater_profile_digest"] = key
    data.report["grading_run_ids"] = ["grading-1"]
    for pair in data.report["comparisons"]:
        for side in ("left", "right"):
            if pair[side]["profile_digest"] == old_digest:
                pair[side]["profile_digest"] = key

    def get_run(run_id):
        tags = {
            "evaluation.kind": "analysis"
            if run_id == "analysis-1"
            else "grading"
            if run_id == "grading-1"
            else "answers",
            "evaluation.schema": "evaluation-v2",
            "source_run_id": data.report["source_run_id"],
            "source_bundle_digest": data.inputs["bundle"]["digest"],
            "rater_profile_digest": key,
            "rater_id": grading["rater_id"],
            "round": str(grading["round"]),
        }
        return SimpleNamespace(
            info=SimpleNamespace(status="FINISHED", experiment_id="42"),
            data=SimpleNamespace(tags=tags),
        )

    def download(run_id, name, directory):
        value = {
            "agreement.json": data.report,
            "evaluation-bundle.json": data.inputs["bundle"],
            "grading-inputs.json": {"results": data.inputs["gradings"]},
            "rater-profile.json": profile,
        }[name]
        path = tmp_path / name
        path.write_text(json.dumps(value))
        return str(path)

    monkeypatch.setattr(
        "mlflow.MlflowClient",
        lambda **kw: SimpleNamespace(
            get_run=get_run,
            download_artifacts=download,
            search_traces=lambda **kw: [SimpleNamespace(info=SimpleNamespace(trace_id="trace-1"))],
            get_trace=lambda trace_id: SimpleNamespace(
                data=SimpleNamespace(
                    spans=[SimpleNamespace(attributes={"mlflow.llm.model": "claude-haiku-5-5"})]
                )
            ),
        ),
    )
    loaded = from_mlflow(
        "analysis",
        "analysis-1",
        tmp_path,
        tracking_uri="https://user:secret@mlflow.example/team?token=private",
    )
    legend = rater_legend(analysis_subjects(loaded)).object
    assert "claude-haiku-5-5" in legend
    assert "Claude Haiku 5.5" in legend and "실행 trace" in legend
    links = loaded.links
    assert (
        links["answers"]["url"]
        == "https://mlflow.example/team/#/experiments/42/runs/answers-demo-A"
    )
    assert (
        links["gradings"][0]["url"] == "https://mlflow.example/team/#/experiments/42/runs/grading-1"
    )
    conditions = evaluation_conditions(loaded, analysis_subjects(loaded))
    markup = "".join(p.object for p in conditions.select(pn.pane.HTML))
    assert 'href="https://mlflow.example/team/#/experiments/42/runs/grading-1"' in markup
    assert "secret" not in markup and "private" not in markup
    profile["scorers"][0]["model"] = "changed"
    with pytest.raises(ValueError, match="profile binding"):
        from_mlflow("analysis", "analysis-1", tmp_path)


def test_reason_filters_support_each_all_combination_and_escape_saved_reasons():
    from html import unescape

    from elice_reporting.analysis_sections import rationale_browser, rationale_filter_code

    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required for offline callback validation")
    script = (
        """
const rater={value:'all'},dimension={value:'all'};
const group_0={},group_1={},table_0={},table_1={};
const rows=[['<tr>human-c</tr>','<tr>model-c</tr>'],
            ['<tr>human-g</tr>','<tr>model-g</tr>']];
const start='<table>',end='</table>';
function update(){
"""
        + rationale_filter_code(2)
        + """
return [[group_0.visible,group_1.visible],[table_0.text,table_1.text]];}
const results=[update()];
rater.value='0'; results.push(update());
rater.value='all';dimension.value='1';results.push(update());
rater.value='1';dimension.value='0';results.push(update());
console.log(JSON.stringify(results));
"""
    )
    output = subprocess.run([node, "-e", script], capture_output=True, text=True, check=True)
    results = json.loads(output.stdout)
    assert [row[0] for row in results] == [[True, True], [True, True], [False, True], [True, False]]
    tables = [[unescape(table) for table in row[1]] for row in results]
    assert tables[0] == [
        "<table><tr>human-c</tr><tr>model-c</tr></table>",
        "<table><tr>human-g</tr><tr>model-g</tr></table>",
    ]
    assert tables[1][0] == "<table><tr>human-c</tr></table>"
    assert tables[2] == tables[0]
    assert tables[3][0] == "<table><tr>model-c</tr></table>"
    # The callback must escape the complete table once for Panel's HTML model.
    assert results[0][1][0].startswith("&lt;table&gt;")
    data = demo("analysis")
    subjects = analysis_subjects(data)
    first_model = next(s for s in subjects if s["rater"].startswith("model:"))
    grading = next(g for g in data.inputs["gradings"] if g["rater_id"] == first_model["rater"])
    grading["ratings"][0]["rationale"] = "<script>unsafe()</script>"
    view = rationale_browser(data, subjects, "0")
    panes = view.select(pn.pane.HTML)
    markup = "".join(p.object for p in panes)
    assert "&lt;script&gt;unsafe()&lt;/script&gt;" in markup
    assert "<script>unsafe()" not in markup
    tables = [p.object for p in panes if '<table class="rationale-table">' in p.object]
    assert len(tables) == 4
    assert all("<th>평가자</th><th>사유</th><th>점수</th>" in table for table in tables)
    assert all(table.count("<img ") == 1 for table in tables)
    assert first_model["icon"] in tables[0]
    assert "groundedness" not in tables[2]  # Metric names belong to group headings only.
    assert len(view.select(pn.widgets.Select)) == 2


def test_stored_ordinal_reason_is_escaped_without_reinterpreting_score():
    from elice_reporting.analysis_sections import rationale_markup

    text = "필수 항목 일부 누락. <script>unsafe()</script>"
    html = rationale_markup(text)
    assert "&lt;script&gt;unsafe()&lt;/script&gt;" in html
    assert "<script>unsafe()" not in html
    data = demo("analysis")
    notes = metric_guide(data).objects[1].object
    assert "pass/fail 대신" in notes and "변환하지 않습니다" in notes
    assert "0.99도 fail" not in notes


def test_context_browser_preserves_exact_selected_text_and_distinguishes_search_only():
    from elice_reporting.analysis_sections import context_browser

    generated = dict(
        chunk_id="one",
        title="Selected",
        url="https://example.test/a",
        heading_path="Section A",
        text="<script>selected snapshot</script>",
    )
    result = dict(
        generation_sources=[generated],
        retrieved_sources=[
            generated,
            dict(chunk_id="two", title="Unused", url="javascript:alert(1)", text="unused body"),
        ],
        citations=[dict(chunk_id="one", source_id=3)],
    )
    view = context_browser(result)
    selectors = view.select(pn.widgets.Select)
    assert len(selectors) == 1 and len(selectors[0].options) == 2
    assert any("생성 문맥 1 · 인용 [3]" in k for k in selectors[0].options)
    assert any("검색 전용" in k for k in selectors[0].options)
    panes = view.select(pn.pane.HTML)
    selected = next(p for p in panes if "selected snapshot" in p.object)
    unused = next(p for p in panes if "unused body" in p.object)
    assert selected.visible and not unused.visible
    assert "&lt;script&gt;selected snapshot&lt;/script&gt;" in selected.object
    assert 'href="https://example.test/a"' in selected.object and "Section A" in selected.object
    assert 'href="javascript:' not in unused.object
    assert not view.select(pn.Accordion)


def test_context_empty_and_missing_are_not_conflated():
    from elice_reporting.analysis_sections import context_browser

    missing = context_browser({})
    empty = context_browser({"generation_sources": []})
    assert any("기록되지 않았습니다" in p.object for p in missing.select(pn.pane.Markdown))
    assert any("전달된 생성 문맥은 없습니다" in p.object for p in empty.select(pn.pane.Markdown))
    changed = context_browser(
        dict(
            generation_sources=[dict(chunk_id="one", text="exact shortened context")],
            retrieved_sources=[dict(chunk_id="one", text="longer retrieved text")],
        )
    )
    assert len(changed.select(pn.widgets.Select)[0].options) == 2


def test_grounding_guidance_distinguishes_no_context_from_missing_context():
    from elice_reporting.analysis_sections import grounding_scope

    empty = grounding_scope({"generation_sources": []}).object
    assert "문맥 자료: 0개" in empty
    assert "사실 주장 없이 답변을 유보" in empty and "N/A" in empty
    assert "근거 없이 구체적인 사실" in empty and "0점" in empty
    assert "질문 적합성" in empty
    missing = grounding_scope({}).object
    assert "미기록" in missing and "문맥 자료: 0개" not in missing
    present = grounding_scope({"generation_sources": [{"text": "context"}]}).object
    assert "문맥 자료: 1개" in present and "N/A" not in present


def test_grounding_guidance_handles_unanswerable_question_with_nonempty_context():
    from elice_reporting.analysis_sections import grounding_scope

    result = {
        "status": "insufficient_evidence",
        "generation_sources": [{"text": "related context"} for _ in range(14)],
    }
    before = deepcopy(result)
    text = grounding_scope(result, {"answerable": False}).object
    assert "문맥 자료: 14개" in text and "문맥 자료: 0개" not in text
    assert "데이터셋 기준" in text and "질문에 답할 근거가 부족한 문항" in text
    assert "자료가 전달됐더라도" in text
    assert "근거 충실도 N/A" in text and "근거 충실도 0점" in text
    assert "유보 응답이라도 구체적인 사실 주장" in text
    assert result == before
    # Runtime abstention also needs guidance, independently of the reference answerability.
    assert "근거 충실도 N/A" in grounding_scope(result).object
