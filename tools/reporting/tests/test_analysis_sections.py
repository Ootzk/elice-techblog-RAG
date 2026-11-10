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
    assert "질문" in first.objects[0].select(pn.pane.HTML)[0].object
    assert (
        data.inputs["bundle"]["cases"][0]["result"]["answer"]
        in first.objects[1].select(pn.pane.HTML)[0].object
    )
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
    assert "평가 문항" in text and "최대 검색 도구 호출" in text
    assert data.inputs["bundle"]["dataset_digest"] not in text
    assert "데이터셋 ID" not in text and "프로필" not in text
    assert "최대 검색 도구 호출" in text
    assert "1회차" in text and "정확한 모델명 미기록" in text
    assert not conditions.select(pn.Accordion)
    assert "원본 답변" in text and "원본 gradings" not in text
    assert "채점 기록" in text or "연결 없음" in text
    definitions = metric_guide(data)
    definition_text = "\n".join(p.object for p in definitions.select(pn.pane.HTML))
    assert "생성" in definition_text
    assert all("<table" in p.object for p in definitions.objects if isinstance(p, pn.pane.HTML))
    headings = [
        p.object
        for p in definitions.objects
        if isinstance(p, pn.pane.Markdown) and p.object.startswith("### ")
    ]
    assert headings == ["### 답변별 채점 기준", "### 평가자 간 일치도"]
    assert definitions.objects[1].object.startswith("- **등급:**")
    assert "- **판정:**" in definitions.objects[1].object
    assert "- **N/A·오류·미기록:**" in definitions.objects[1].object
    assert "- **진단 지표:**" in definitions.objects[1].object
    assert isinstance(definitions.objects[2], pn.pane.HTML)
    assert not definitions.select(pn.Accordion)
    assert "Fleiss" not in definition_text and "Krippendorff" not in definition_text
    assert "우연 일치" in definition_text
    view = kappa_heatmaps(data, analysis_subjects(data))
    assert isinstance(view.objects[0], pn.widgets.Select)
    assert isinstance(view.objects[1].objects[0], pn.GridBox)


def test_condition_category_dropdown_switches_offline_views():
    data = demo("analysis")
    view = evaluation_conditions(data, analysis_subjects(data))
    selector = view.objects[0]
    assert isinstance(selector, pn.widgets.Select)
    assert list(selector.options) == [
        "데이터셋",
        "답변·검색 설정",
        "상세 실험 조건",
        "평가 주체",
    ]
    assert [item.visible for item in view.objects[1:]] == [True, False, False, False]
    from bokeh.models import Select

    root = view.get_root()
    control = next(s for s in root.select({"type": Select}) if s.title == "평가 조건 범주")
    assert control.js_property_callbacks["change:value"]
    node = shutil.which("node")
    if node:
        script = (
            "const item_0={visible:true},item_1={visible:false},"
            "item_2={visible:false},item_3={visible:false},cb_obj={value:'3'};"
            + item_switch_code(4)
            + "console.log(JSON.stringify([item_0.visible,item_1.visible,"
            "item_2.visible,item_3.visible]));"
        )
        result = subprocess.run([node, "-e", script], capture_output=True, text=True, check=True)
        assert json.loads(result.stdout) == [False, False, False, True]


def test_synthetic_review_is_labeled_in_report_legend():
    data = demo("analysis")
    human = next(s for s in analysis_subjects(data) if s["rater"].startswith("human:"))
    data.links.setdefault("gradings", []).append(
        {
            "rater": human["rater"],
            "round": human["round"],
            "profile_digest": human["profile"],
            "synthetic": True,
        }
    )
    subjects = analysis_subjects(data)
    assert any(s["display_name"].startswith("모의 검수") for s in subjects)
    legend = rater_legend(subjects).object
    assert "개발용 임의 점수" in legend
    assert "실제 사람 정합성 해석에서 제외" in legend


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
    assert analysis_subjects(loaded)[2]["actual_models"] == ["claude-haiku-5-5"]
    assert "Claude Haiku 5.5" in legend and "실행 기록" in legend
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
    assert any("인용 [3]" in k for k in selectors[0].options)
    assert any("검색 후보" in k for k in selectors[0].options)
    panes = view.select(pn.pane.HTML)
    selected = next(p for p in panes if "selected snapshot" in p.object)
    unused = next(p for p in panes if "unused body" in p.object)
    viewport = next(obj for obj in view.objects if isinstance(obj, pn.Column))
    assert viewport.objects[0].visible and not viewport.objects[1].visible
    assert "&lt;script&gt;selected snapshot&lt;/script&gt;" in selected.object
    header = viewport.objects[0].objects[0].object
    assert 'href="https://example.test/a"' in header and "Section A" in header
    assert 'href="javascript:' not in unused.object
    assert not view.select(pn.Accordion)


def test_context_empty_and_missing_are_not_conflated():
    from elice_reporting.analysis_sections import context_browser

    missing = context_browser({})
    empty = context_browser({"generation_sources": []})
    assert any("기록되지 않았습니다" in p.object for p in missing.select(pn.pane.Markdown))
    assert any("제공된 자료는 없습니다" in p.object for p in empty.select(pn.pane.Markdown))
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
    assert "제공된 자료: 0개" in empty
    assert "사실 주장 없이 답변을 유보" in empty and "N/A" in empty
    assert "근거 없이 구체적인 사실" in empty and "0점" in empty
    assert "질문 적합성" in empty
    missing = grounding_scope({}).object
    assert "미기록" in missing and "제공된 자료: 0개" not in missing
    present = grounding_scope({"generation_sources": [{"text": "context"}]}).object
    assert "제공된 자료: 1개" in present and "N/A" not in present


def test_grounding_guidance_handles_unanswerable_question_with_nonempty_context():
    from elice_reporting.analysis_sections import grounding_scope

    result = {
        "status": "insufficient_evidence",
        "generation_sources": [{"text": "related context"} for _ in range(14)],
    }
    before = deepcopy(result)
    text = grounding_scope(result, {"answerable": False}).object
    assert "제공된 자료: 14개" in text and "제공된 자료: 0개" not in text
    assert "데이터셋 기준" in text and "질문에 답할 근거가 부족한 문항" in text
    assert "자료가 전달됐더라도" in text
    assert "근거 충실도 N/A" in text and "근거 충실도 0점" in text
    assert "유보 응답이라도 구체적인 사실 주장" in text
    assert result == before
    # Runtime abstention also needs guidance, independently of the reference answerability.
    assert "근거 충실도 N/A" in grounding_scope(result).object


@pytest.mark.parametrize("raters,dimensions", [(3, 4), (5, 1)])
def test_matrix_taps_choose_cell_column_row_or_everything_offline(raters, dimensions):
    from elice_reporting.analysis_sections import grading_selection_code

    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required for offline tap validation")
    taps, expected = [], []
    for rater_index in range(raters):
        for dimension in range(dimensions):
            taps.append([dimension, raters - 1 - rater_index])
            expected.append([str(rater_index), str(dimension)])
    taps.extend(
        [
            [dimensions - 1, raters - 0.08],
            [-0.8, 0],
            [-0.8, raters - 0.08],
            [dimensions + 1, 0],
            [0, -0.65],
            [0, raters - 0.52],
        ]
    )
    expected.extend([["all", str(dimensions - 1)], [str(raters - 1), "all"]] + [["all", "all"]] * 4)
    script = (
        "const rater={value:'initial'},dimension={value:'initial'};"
        + "const taps="
        + json.dumps(taps)
        + ";function tap(x,y){const cb_obj={x,y};function update(){"
        + grading_selection_code(raters, dimensions)
        + "}update();return [rater.value,dimension.value];}"
        + "console.log(JSON.stringify([...taps.map(([x,y])=>tap(x,y)),tap(NaN,0)]));"
    )
    output = subprocess.run([node, "-e", script], capture_output=True, text=True, check=True)
    assert json.loads(output.stdout) == expected + [["all", "all"]]


def test_matrix_tap_callbacks_bind_each_case_to_its_own_dropdown_models():
    import holoviews as hv
    from bokeh.models import HoverTool
    from bokeh.plotting import figure

    pn.extension()
    hv.extension("bokeh")
    data = demo("analysis")
    view = item_browser(data, analysis_subjects(data), ["0", "1"])
    root = view.get_root()
    plots = list(root.select({"type": figure}))
    assert len(plots) == 2
    callbacks = [plot.js_event_callbacks["tap"][0] for plot in plots]
    assert len({c.args["rater"].id for c in callbacks}) == 2
    assert len({c.args["dimension"].id for c in callbacks}) == 2
    for plot, callback in zip(plots, callbacks, strict=True):
        assert all(isinstance(tool, HoverTool) for tool in plot.tools)
        assert plot.toolbar.active_drag is None and plot.toolbar.active_scroll is None
        assert callback.args["rater"].title == "평가자 · 회차"
        assert callback.args["dimension"].title == "평가 항목"
        # The dropdown change callbacks still drive the same offline reason tables.
        assert callback.args["rater"].js_property_callbacks["change:value"]
        assert callback.args["dimension"].js_property_callbacks["change:value"]


def test_markdown_cards_render_structure_and_keep_untrusted_content_inert():
    from elice_reporting.presentation import content_card

    markdown = (
        "## 설명\n\n**강조**와 `code`\n\n"
        "| 항목 | 값 |\n| --- | --- |\n| A | 1 |\n\n"
        "```python\nprint('hello')\n```\n\n"
        "<script>alert(1)</script>\n\n[bad](javascript:alert(1))\n\n"
        "![remote](https://example.test/tracker.png)"
    )
    card = content_card("답변", markdown, markdown=True)
    html = card.select(pn.pane.HTML)[0].object
    assert all(tag in html for tag in ("<h2>", "<strong>", "<table>", "<pre><code"))
    assert "<script>" not in html and "&lt;script&gt;" in html
    assert 'href="javascript:' not in html and "<img " not in html
    assert card.styles["border-radius"] == "14px"


def test_reference_list_keeps_uncited_passages_and_duplicate_titles_selectable():
    from elice_reporting.analysis_sections import context_browser

    source = {
        "title": "Same article",
        "text": "**stored** passage",
        "url": "https://example.test/a",
    }
    result = {
        "generation_sources": [{**source, "chunk_id": "uncited"}, {**source, "chunk_id": "cited"}],
        "retrieved_sources": [
            {**source, "chunk_id": "uncited"},
            {**source, "chunk_id": "cited"},
            {**source, "chunk_id": "search-only"},
        ],
        "citations": [{"chunk_id": "cited", "source_id": 8}],
    }
    view = context_browser(result)
    options = list(view.select(pn.widgets.Select)[0].options)
    assert len(options) == 3
    assert "인용 [8]" in options[0]
    assert "미인용 참고 자료" in options[1]
    assert "검색 후보" in options[2]
    assert "생성 문맥" not in "".join(options)
    assert sum("<strong>stored</strong>" in p.object for p in view.select(pn.pane.HTML)) == 3


def test_experiment_conditions_display_only_recorded_recipe_and_never_guess_missing_values():
    from elice_reporting.conditions import condition_sections

    assert condition_sections(None) == []
    with pytest.raises(ValueError, match="Unsupported"):
        condition_sections({"schema": "unknown"})
    recorded = {
        "schema": "experiment-conditions-v1",
        "chunking": {
            "policy": {
                "profile": "h2",
                "overflow": "sentence",
                "chunk_size": 1024,
                "chunk_overlap": 96,
                "tokenizer": "recorded-tokenizer",
            }
        },
        "embedding": {
            "profile": {
                "embedding": {"model": "recorded-model", "dimensions": 768},
                "distance": "cosine",
            }
        },
        "vector_index": {"backend": "pgvector", "sql_indexes": [{"indexdef": "USING hnsw"}]},
    }
    sections = condition_sections(recorded)
    text = str(sections)
    assert "1,024 토큰" in text and "96 토큰" in text and "768" in text
    assert "recorded-model" in text and "HNSW" in text
    assert "512" not in text and "bge-m3" not in text
    assert dict(sections[0][1])["표 / 코드"] == "미기록 / 미기록"
    data = demo("analysis")
    data.inputs["bundle"]["qa_profile"]["experiment_conditions"] = recorded
    view = evaluation_conditions(data, analysis_subjects(data))
    assert list(view.objects[0].options) == [
        "데이터셋",
        "답변·검색 설정",
        "Corpus·청킹",
        "임베딩·검색 인덱스",
        "키워드 검색·Agent",
        "생성·실행 환경",
        "평가 주체",
    ]
    assert any("recorded-model" in p.object for p in view.select(pn.pane.HTML))
