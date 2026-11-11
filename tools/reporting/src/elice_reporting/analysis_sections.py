"""Recorded experiment metadata, metric definitions and offline item selection."""

from html import escape
from urllib.parse import urlsplit

import panel as pn
from markupsafe import Markup

from .analysis_visuals import (
    DIMENSIONS,
    PLOT_STYLE,
    grading_cells,
    grading_heatmap,
    grading_legend,
    matrix_size,
    rater_legend,
)
from .conditions import condition_sections
from .presentation import (
    content_card,
    metric_badge,
    metric_label,
    model_name,
    text_block,
)


def _table(rows):
    return pn.pane.HTML(
        '<table style="width:100%;border-collapse:collapse;font:14px system-ui">'
        + "".join(
            '<tr><th style="width:220px;text-align:left;vertical-align:top;padding:9px;'
            'border-bottom:1px solid #e8edf2;color:#526071">' + escape(str(key)) + "</th>"
            '<td style="padding:9px;border-bottom:1px solid #e8edf2;overflow-wrap:anywhere">'
            + (str(value) if isinstance(value, Markup) else escape(str(value)))
            + "</td></tr>"
            for key, value in rows
        )
        + "</table>",
        sizing_mode="stretch_width",
    )


def _run_reference(link, label=None):
    identity = link.get("run_id", "미기록")
    url = link.get("url")
    label = label or identity
    if url and urlsplit(url).scheme in {"http", "https"}:
        return Markup(
            f'<a href="{escape(url, quote=True)}" target="_blank" rel="noopener noreferrer" '
            f'title="{escape(identity, quote=True)}">{escape(label)} ↗</a>'
        )
    return Markup(escape(label))


def condition_categories(bundle, *, failed, rubric_id, answers_reference):
    profile = bundle["qa_profile"]
    models = list(
        dict.fromkeys(
            c["result"]["generation_model"]
            for c in bundle["cases"]
            if c.get("result") and c["result"].get("generation_model")
        )
    )
    dataset = [
        ("데이터셋", "🥇 Gold" if bundle["kind"] == "gold" else "🦾 Iron"),
        ("평가 문항", f"{len(bundle['records'])}문항"),
        ("답변 생성 실패", failed),
        ("판정 기준", rubric_id),
        ("원본 답변", answers_reference),
    ]
    fields = (
        ("top_k", "검색당 결과 수", "개"),
        ("qa_agent_max_tool_calls", "최대 검색 도구 호출", "회"),
        ("qa_max_context_tokens", "참고 자료 예산", "토큰"),
        ("qa_history_pairs", "대화 이력", "쌍"),
        ("qa_history_tokens", "대화 이력 예산", "토큰"),
        ("generation_max_output_tokens", "답변 출력 한도", "토큰"),
        ("generation_temperature", "생성 온도", ""),
    )
    settings = [("답변 모델", ", ".join(model_name(m) for m in models) or "미기록")]
    for key, title, unit in fields:
        if key in profile:
            value = profile[key]
            display = (
                "공급자 기본값"
                if value is None
                else (
                    f"{value:,} {unit}".strip() if isinstance(value, (int, float)) else str(value)
                )
            )
            settings.append((title, display))
    for name, prompt in profile.get("prompts", {}).items():
        if isinstance(prompt, dict):
            title = "검색 계획 프롬프트" if "planner" in name else "답변 프롬프트"
            settings.append((title, f"{prompt.get('name', name)} · v{prompt.get('version', '?')}"))
    categories = [("데이터셋", _table(dataset)), ("답변·검색 설정", _table(settings))]
    sections = condition_sections(profile.get("experiment_conditions"))
    categories.extend((title, _table(rows)) for title, rows in sections)
    if not sections:
        categories.append(("상세 실험 조건", _table([("기록 상태", "미기록")])))
    return categories


def condition_selector(categories):
    views = [
        pn.Column(content, visible=index == 0, sizing_mode="stretch_width")
        for index, (_, content) in enumerate(categories)
    ]
    selector = pn.widgets.Select(
        label="평가 조건 범주",
        options={title: str(index) for index, (title, _) in enumerate(categories)},
        value="0",
        sizing_mode="stretch_width",
    )
    selector.jscallback(
        args={f"item_{index}": view for index, view in enumerate(views)},
        value=item_switch_code(len(views)),
    )
    return pn.Column(selector, *views, sizing_mode="stretch_width")


def evaluation_conditions(data, subjects):
    bundle = data.inputs["bundle"]
    categories = condition_categories(
        bundle,
        failed=f"{data.report['qa_failed_cases']}문항",
        rubric_id=data.report["rubric"]["id"],
        answers_reference=_run_reference(data.links.get("answers", {}), "답변 실행 기록"),
    )
    categories.append(("평가 주체", rater_legend(subjects)))
    return condition_selector(categories)


def _criterion_text(text):
    for technical, label in (
        ("required_points", "필수 요점"),
        ("generation_sources", "답변 작성에 제공된 자료"),
        ("생성 문맥", "답변 작성에 제공된 자료"),
        ("optional", "선택"),
    ):
        text = text.replace(technical, label)
    return text


def rubric_guide(rubric, *, repeatability=False):
    """Display the shared rubric anchors for analysis and repeatability reports."""
    header = (
        "<tr><th>항목</th><th>평가 대상</th>"
        + "".join(
            f"<th>{score} · {escape(rubric['scale_labels'][str(score)])}</th>"
            for score in rubric["scale"]
        )
        + "<th>N/A</th></tr>"
    )
    rows = "".join(
        "<tr><td>"
        + metric_badge(name)
        + "</td><td>"
        + escape(_criterion_text(d["description"]))
        + "</td>"
        + "".join(
            "<td>" + escape(_criterion_text(d["levels"][str(score)])) + "</td>"
            for score in rubric["scale"]
        )
        + "<td>"
        + escape(_criterion_text(d["not_applicable"]))
        + "</td></tr>"
        for name, d in rubric["dimensions"].items()
    )
    status_notes = (
        "- **원점수:** 저장된 등급과 한국어 사유를 사용합니다. "
        "경계 충족 여부는 등급에서 별도로 계산합니다.\n"
        "- **N/A·오류·미기록:** 0점이 아니며 유효 등급을 분모로 쓰는 지표에서 제외합니다.\n"
        if repeatability
        else "- **판정:** pass/fail 대신 저장된 등급과 한국어 사유를 표시합니다.\n"
        "- **N/A·오류·미기록:** 0점이 아니며 유효 등급 일치도 계산에서 제외합니다.\n"
    )
    return pn.Column(
        pn.pane.Markdown("### 답변별 채점 기준"),
        pn.pane.Markdown(
            "- **등급:** 사람과 모델 모두 아래 기준으로 직접 채점합니다. "
            "비율 점수를 등급으로 변환하지 않습니다.\n"
            + status_notes
            + "- **진단 지표:** 별도로 실행한 Ragas 비율 점수는 "
            "등급이나 사람 판정과 혼합하지 않습니다.\n"
            "- **모델 등급 채점:** [Ragas "
            "DiscreteMetric](https://docs.ragas.io/en/latest/references/metrics/)을 사용합니다.\n"
            f"- **판정 기준 버전:** `{rubric['id']}`"
        ),
        pn.pane.HTML(
            '<table style="width:100%;font:13px/1.6 system-ui"><thead>'
            + header
            + "</thead><tbody>"
            + rows
            + "</tbody></table>",
            stylesheets=[
                "th,td{text-align:left;vertical-align:top;padding:10px; "
                "border-bottom:1px solid #e4e9ef;}"
            ],
        ),
        sizing_mode="stretch_width",
    )


def metric_guide(data):
    return pn.Column(
        *rubric_guide(data.report["rubric"]).objects,
        pn.pane.Markdown("### 평가자 간 일치도"),
        _table(
            [
                (
                    "선형 가중 Cohen κ",
                    "동일 rubric의 등급 차이에 비례한 불일치를 우연 일치로 보정합니다. "
                    "전체 등급 순서는 고정합니다. "
                    "일반 κ·제곱 가중 κ는 원본 analysis에 보조 저장합니다.",
                ),
            ]
        ),
        sizing_mode="stretch_width",
    )


def item_switch_code(count):
    return (
        "const items = [" + ",".join(f"item_{i}" for i in range(count)) + "];\n"
        "items.forEach((item, index) => {item.visible = index === Number(cb_obj.value);});"
    )


def grading_selection_code(raters, dimensions):
    """Map transposed grading cells and headers to the offline reason filters."""
    return f"""
const x = cb_obj.x, y = cb_obj.y;
if (!Number.isFinite(x) || !Number.isFinite(y)) return;
const column = Math.round(x), row = Math.round(y);
const inColumn = column >= 0 && column < {dimensions} && Math.abs(x-column) <= 0.46;
const inRow = row >= 0 && row < {raters} && Math.abs(y-row) <= 0.46;
const header = Math.abs(y-({raters}-0.08)) <= 0.42;
let selectedRater, selectedDimension;
if (x < -0.5 && header) {{
    selectedRater = 'all'; selectedDimension = 'all';
}} else if (inColumn && header) {{
    selectedRater = 'all'; selectedDimension = String(column);
}} else if (x < -0.5 && inRow) {{
    selectedRater = String({raters}-1-row); selectedDimension = 'all';
}} else if (inColumn && inRow) {{
    selectedRater = String({raters}-1-row); selectedDimension = String(column);
}} else {{
    return;
}}
rater.value = selectedRater;
dimension.value = selectedDimension;
"""


def item_browser(data, subjects, case_ids):
    if not case_ids:
        return pn.pane.Markdown("저장된 문항이 없습니다.")
    bundle = data.inputs["bundle"]
    records = {r["id"]: r for r in bundle["records"]}
    cases = {c["record_id"]: c for c in bundle["cases"]}
    items, options = [], {}
    for i, record_id in enumerate(case_ids):
        question = records[record_id]["inputs"]["question"]
        case = cases[record_id]
        result = case.get("result") or {}
        reasons = rationale_browser(data, subjects, record_id)
        controls = {control.label: control for control in reasons.select(pn.widgets.Select)}
        matrix = pn.pane.HoloViews(grading_heatmap(data, subjects, record_id), styles=PLOT_STYLE)
        matrix.jscallback(
            args={
                "rater": controls["평가자 · 회차"],
                "dimension": controls["평가 항목"],
            },
            **{
                "event:tap": grading_selection_code(
                    len(subjects), len(data.report["rubric"]["dimensions"])
                )
            },
        )
        options[f"{i + 1}. {question[:100]}{'…' if len(question) > 100 else ''}"] = str(i)
        items.append(
            pn.Column(
                content_card("💬 질문", question, accent="#2563eb"),
                content_card("✦ 답변", result.get("answer"), markdown=True, accent="#7c3aed"),
                *([text_block("생성 오류", case["error"])] if case.get("error") else []),
                *(
                    [grounding_scope(result, records[record_id].get("expectations", {}))]
                    if bundle["kind"] == "gold"
                    else []
                ),
                pn.pane.Markdown(
                    "매트릭스 클릭: **셀** → 평가자·항목 / **왼쪽 평가자** → 평가자·전체 / "
                    "**위 평가 항목** → 전체·항목 / **좌상단** → 전체·전체",
                    styles={"font-size": "12px", "color": "#526071"},
                ),
                pn.Row(
                    matrix,
                    reasons,
                    sizing_mode="stretch_width",
                    styles={"overflow-x": "auto"},
                ),
                context_browser(result),
                visible=i == 0,
                sizing_mode="stretch_width",
            )
        )
    selector = pn.widgets.Select(
        label="문항 선택", options=options, value="0", sizing_mode="stretch_width"
    )
    selector.jscallback(
        args={f"item_{i}": item for i, item in enumerate(items)}, value=item_switch_code(len(items))
    )
    return pn.Column(
        selector, grading_legend(data.report["rubric"]), *items, sizing_mode="stretch_width"
    )


def grounding_scope(result, expectations=None):
    """Describe the recorded context, without assigning or overriding any grade."""
    sources = result.get("generation_sources")
    unanswerable = (expectations or {}).get("answerable") is False
    abstained = result.get("status") == "insufficient_evidence"
    if sources is None:
        text = (
            "**답변 작성에 제공된 자료: 미기록**\n\n"
            "참고 자료가 저장되지 않은 상태입니다. 근거가 0개인 경우와 구분해야 하며, "
            "이 기록만으로 근거 충실도를 판단할 수 없습니다."
        )
    else:
        text = f"**답변 작성에 제공된 자료: {len(sources)}개**"
        if unanswerable:
            text += "\n\n**데이터셋 기준: 질문에 답할 근거가 부족한 문항**"
        if abstained:
            text += "\n\n**응답 상태: 근거 부족으로 답변 유보**"
        if not sources or unanswerable or abstained:
            text += (
                "\n\n자료가 전달됐더라도 질문에 답할 근거를 확보했다는 뜻은 아닙니다. "
                if sources
                else "\n\n답변 작성에 제공된 자료가 없습니다. "
            )
            text += (
                "답변 근거가 부족하다는 사실만으로 N/A나 0점을 결정하지 않습니다. "
                "답변이 실제로 무엇을 주장했는지 확인합니다.\n\n"
                "- 사실 주장 없이 답변을 유보했다면 → **근거 충실도 N/A**\n"
                "- 근거 없이 구체적인 사실을 주장했다면 → **근거 충실도 0점**\n"
                "- 유보가 질문에 적절했는지는 → **질문 적합성**에서 평가\n\n"
                "유보 응답이라도 구체적인 사실 주장이 포함돼 있다면 그 주장은 검증 대상입니다."
            )
    return pn.pane.Markdown(text, sizing_mode="stretch_width")


def context_browser(result):
    """Separate cited references, uncited supplied references and search-only candidates."""
    generated = result.get("generation_sources")
    retrieved = result.get("retrieved_sources", [])
    heading = pn.pane.Markdown("### 📚 참고 자료")
    note = pn.pane.Markdown(
        "**인용 자료**는 답변의 출처 번호와 연결됩니다. **미인용 참고 자료**도 답변 작성에 "
        "제공됐으므로 근거 충실도 검증에 사용할 수 있습니다. "
        "**검색 후보**는 검색됐지만 답변 작성에는 제공되지 않은 자료입니다."
    )

    def identity(source):
        return (source.get("chunk_id", source.get("url")), source.get("text"))

    selected = {identity(source) for source in generated or []}
    entries = []
    for source in generated or []:
        citations = [
            str(c["source_id"])
            for c in result.get("citations", [])
            if source.get("chunk_id") and c.get("chunk_id") == source["chunk_id"]
        ]
        entries.append(("인용 자료" if citations else "미인용 참고 자료", source, citations))
    entries.sort(key=lambda entry: not entry[2])
    entries.extend(
        ("검색 후보", source, []) for source in retrieved if identity(source) not in selected
    )
    missing = (
        "답변 작성에 제공된 자료가 기록되지 않았습니다."
        if generated is None
        else "답변 작성에 제공된 자료는 없습니다."
        if not generated
        else ""
    )
    if not entries:
        return pn.Column(heading, note, pn.pane.Markdown(missing), sizing_mode="stretch_width")
    views, options = [], {}
    for index, (scope, source, citations) in enumerate(entries):
        title = source.get("title") or "제목 미기록"
        badge = "인용 [" + ", ".join(citations) + "]" if citations else scope
        options[
            f"{'🔗' if citations else '📄' if scope != '검색 후보' else '🔎'} {badge} · {title}"
        ] = str(index)
        color = "#7c3aed" if citations else "#64748b" if scope != "검색 후보" else "#b45309"
        url = source.get("url", "")
        link = (
            f'<a href="{escape(url, quote=True)}" target="_blank" rel="noopener noreferrer" '
            'style="color:#2563eb;text-decoration:none">원문 열기 ↗</a>'
            if urlsplit(url).scheme in {"http", "https"}
            else "원문 링크 없음"
        )
        header = pn.pane.HTML(
            f'<div style="display:flex;gap:12px;align-items:center;flex-wrap:wrap">'
            f'<span style="color:{color};font:650 12px system-ui">{escape(badge)}</span>'
            f'<strong>{escape(title)}</strong><span style="margin-left:auto">{link}</span></div>'
            f'<p style="color:#64748b;font:12px system-ui">'
            f"{escape(source.get('heading_path') or '섹션 미기록')}</p>",
            sizing_mode="stretch_width",
        )
        view = pn.Column(
            header,
            content_card(
                "📄 저장된 본문", source.get("text") or "본문 미기록", markdown=True, accent=color
            ),
            visible=index == 0,
            sizing_mode="stretch_width",
        )
        views.append(view)
    selector = pn.widgets.Select(
        label="참고 자료 선택", options=options, value="0", sizing_mode="stretch_width"
    )
    selector.jscallback(
        args={f"item_{i}": view for i, view in enumerate(views)}, value=item_switch_code(len(views))
    )
    return pn.Column(
        heading,
        note,
        *([pn.pane.Markdown(missing)] if missing else []),
        selector,
        pn.Column(*views, height=420, scroll=True, sizing_mode="stretch_width"),
        sizing_mode="stretch_width",
    )


RATIONALE_TABLE_START = (
    '<table class="rationale-table"><colgroup>'
    '<col style="width:64px"><col><col style="width:100px"></colgroup>'
    "<thead><tr><th>평가자</th><th>사유</th><th>점수</th></tr></thead><tbody>"
)
RATIONALE_TABLE_END = "</tbody></table>"
RATIONALE_STYLE = """
.rationale-table { width:100%; table-layout:fixed; border-collapse:collapse;
    font:13px/1.65 system-ui,sans-serif; }
.rationale-table th,.rationale-table td { text-align:left; vertical-align:top;
    padding:10px 8px; border-bottom:1px solid #e4e9ef; overflow-wrap:anywhere; }
.rationale-table th { color:#526071; background:#f1f5f9; }
.rationale-table td:last-child { white-space:nowrap; }
"""


def rationale_filter_code(count):
    code = [
        "const escape = value => String(value).replace(/[&<>\"']/g, "
        "char => ({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',\"'\":'&#39;'}[char]));"
    ]
    for j in range(count):
        code.extend(
            [
                f"group_{j}.visible = dimension.value === 'all' || dimension.value === '{j}';",
                f"const selected_{j} = rater.value === 'all' ? rows[{j}] "
                f": [rows[{j}][Number(rater.value)]];",
                f"table_{j}.text = escape(start + selected_{j}.join('') + end);",
            ]
        )
    return "\n".join(code)


def rationale_markup(reason):
    """Display the saved reason verbatim with safe wrapping."""
    return (
        '<div style="white-space:pre-wrap;overflow-wrap:anywhere;line-height:1.7">'
        + escape(reason or "저장된 사유 없음")
        + "</div>"
    )


def rationale_row(subject, row):
    return (
        '<tr><td><img width="36" height="36" '
        f'src="{escape(subject["icon"], quote=True)}" '
        f'alt="{escape(subject["label"], quote=True)}" '
        f'title="{escape(subject["label"], quote=True)}">'
        '<div style="font:10px system-ui;color:#64748b">'
        f"{escape(subject['short_name'])}</div></td><td>"
        + rationale_markup(row["rationale"])
        + '</td><td><span style="display:inline-block;padding:2px 7px;border-radius:5px;'
        f'background:{row["fill"]};color:{row["ink"]}">'
        + escape(row["score_display"])
        + "</span></td></tr>"
    )


def rationale_browser(data, subjects, record_id):
    """Group reasons by dimension and filter the frozen table rows entirely offline."""
    dimensions = DIMENSIONS if data.inputs["bundle"]["kind"] == "gold" else ("behavior",)
    cells = grading_cells(data, subjects, record_id, dimensions)
    raters = {"전체 평가자": "all"}
    raters.update({s["label"]: str(i) for i, s in enumerate(subjects)})
    initial = next((i for i, s in enumerate(subjects) if s["rater"].startswith("model:")), 0)
    rater = pn.widgets.Select(label="평가자 · 회차", options=raters, value=str(initial))
    titles = {d: metric_label(d) for d in dimensions}
    dimension = pn.widgets.Select(
        label="평가 항목",
        options={"전체 항목": "all", **{titles[d]: str(i) for i, d in enumerate(dimensions)}},
        value="0",
    )
    args = {"rater": rater, "dimension": dimension}
    rows, groups = [], []
    for j, name in enumerate(dimensions):
        entries = [
            rationale_row(
                subject,
                cells[(cells.y == len(subjects) - 1 - i) & (cells.dimension == name)].iloc[0],
            )
            for i, subject in enumerate(subjects)
        ]
        rows.append(entries)
        table = pn.pane.HTML(
            RATIONALE_TABLE_START + entries[initial] + RATIONALE_TABLE_END,
            stylesheets=[RATIONALE_STYLE],
            sizing_mode="stretch_width",
        )
        group = pn.Column(
            pn.pane.HTML(f'<h4 style="margin:12px 0 8px">{metric_badge(name)}</h4>'),
            table,
            sizing_mode="stretch_width",
            visible=j == 0,
        )
        args[f"table_{j}"] = table
        args[f"group_{j}"] = group
        groups.append(group)
    args.update(rows=rows, start=RATIONALE_TABLE_START, end=RATIONALE_TABLE_END)
    for control in (rater, dimension):
        control.jscallback(args=args, value=rationale_filter_code(len(dimensions)))
    return pn.Column(
        pn.Row(rater, dimension, sizing_mode="stretch_width"),
        pn.Column(
            *groups,
            height=max(560, matrix_size(subjects) - 55),
            scroll=True,
            sizing_mode="stretch_width",
        ),
        sizing_mode="stretch_width",
        min_width=380,
    )
