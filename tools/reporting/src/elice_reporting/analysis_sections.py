"""Recorded experiment metadata, metric definitions and offline item selection."""

import json
from html import escape
from urllib.parse import urlsplit

import panel as pn
from markupsafe import Markup

from .analysis_visuals import (
    DIMENSIONS,
    grading_cells,
    grading_heatmap,
    grading_legend,
    matrix_size,
    rater_legend,
)
from .presentation import text_block


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


def evaluation_conditions(data, subjects):
    bundle = data.inputs["bundle"]
    profile = bundle["qa_profile"]
    models = list(
        dict.fromkeys(
            c["result"]["generation_model"]
            for c in bundle["cases"]
            if c.get("result") and c["result"].get("generation_model")
        )
    )
    answers = data.links.get("answers", {"run_id": bundle["source_run_id"]})
    dataset = [
        ("데이터셋 유형", bundle["kind"]),
        ("데이터셋 ID", bundle["dataset_id"]),
        ("데이터셋 내용 해시", bundle["dataset_digest"]),
        ("문항 수 / 생성 실패", f"{len(bundle['records'])} / {data.report['qa_failed_cases']}"),
        ("원본 answers", _run_reference(answers)),
    ]
    settings = [
        (key, json.dumps(value, ensure_ascii=False))
        for key, value in profile.items()
        if not isinstance(value, (dict, list))
    ]
    for name, prompt in profile.get("prompts", {}).items():
        if isinstance(prompt, dict):
            settings.append(
                (
                    f"프롬프트 · {name}",
                    json.dumps(
                        {
                            key: prompt[key]
                            for key in ("name", "version", "digest", "source")
                            if key in prompt
                        },
                        ensure_ascii=False,
                    ),
                )
            )
    return pn.Column(
        _table(dataset),
        pn.pane.Markdown("### 답변 생성·검색 설정"),
        _table([("기록된 응답 모델", ", ".join(models) or "미기록"), *settings]),
        pn.pane.Markdown("### 평가 주체"),
        rater_legend(subjects),
        sizing_mode="stretch_width",
    )


def metric_guide(data):
    rubric = data.report["rubric"]
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
        + escape(d["title"])
        + "</td><td>"
        + escape(d["description"])
        + "</td>"
        + "".join("<td>" + escape(d["levels"][str(score)]) + "</td>" for score in rubric["scale"])
        + "<td>"
        + escape(d["not_applicable"])
        + "</td></tr>"
        for d in rubric["dimensions"].values()
    )
    return pn.Column(
        pn.pane.Markdown("### Grading · 공통 rubric 등급"),
        pn.pane.Markdown(
            "- **등급:** 사람과 모델 모두 아래 기준으로 직접 채점합니다. "
            "비율 점수를 등급으로 변환하지 않습니다.\n"
            "- **판정:** pass/fail 대신 저장된 등급과 한국어 사유를 표시합니다.\n"
            "- **N/A·오류·미기록:** 0점이 아니며 유효 등급 일치도 계산에서 제외합니다.\n"
            "- **진단 지표:** 별도로 실행한 Ragas 비율 점수는 "
            "등급이나 사람 판정과 혼합하지 않습니다.\n"
            "- **모델 등급 채점:** [Ragas "
            "DiscreteMetric](https://docs.ragas.io/en/latest/references/metrics/)을 사용합니다.\n"
            f"- **rubric:** `{rubric['id']}` · `{data.report['rubric_digest']}`"
        ),
        pn.pane.HTML(
            '<table style="width:100%;font:13px/1.6 system-ui"><thead>'
            + header
            + "</thead><tbody>"
            + rows
            + "</tbody></table>",
            stylesheets=[
                "th,td{text-align:left;vertical-align:top;padding:10px;b "
                "rder-bottom:1px solid #e4e9ef;}"
            ],
        ),
        pn.pane.Markdown("### Analysis · 평가자 간 일치도"),
        _table(
            [
                (
                    "선형 가중 Cohen κ",
                    "동일 rubric의 등급 차이에 비례한 불일치를 우연 일치로 보정합니다. "
                    "전체 등급 순서는 고정합니다. "
                    "일반 κ·제곱 가중 κ는 원본 analysis에 보조 저장합니다.",
                ),
                (
                    "Fleiss κ · 명목형 보조",
                    "세 명 이상이 정확히 같은 등급을 부여했는지 측정합니다. "
                    "순서형 주 지표가 아니며 한 주체의 여러 회차는 패널에 중복 투입하지 않습니다.",
                ),
                (
                    "Krippendorff α · 순서형",
                    "여러 주체의 순서형 등급 일치도를 비교하며 "
                    "문항마다 최소 두 개의 유효 등급이 있으면 사용합니다. "
                    "미기록·N/A는 점수로 채우지 않습니다.",
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


def grading_selection_code(columns, dimensions):
    """Map fixed plot coordinates to the existing offline reason filters."""
    return f"""
const x = cb_obj.x, y = cb_obj.y;
if (!Number.isFinite(x) || !Number.isFinite(y)) return;
const column = Math.round(x), row = Math.round(y);
const inColumn = column >= 0 && column < {columns} && Math.abs(x-column) <= 0.46;
const inRow = row >= 0 && row < {dimensions} && Math.abs(y-row) <= 0.46;
const header = Math.abs(y-({dimensions}-0.08)) <= 0.42;
let selectedRater, selectedDimension;
if (x < -0.5 && header) {{
    selectedRater = 'all'; selectedDimension = 'all';
}} else if (inColumn && header) {{
    selectedRater = String(column); selectedDimension = 'all';
}} else if (x < -0.5 && inRow) {{
    selectedRater = 'all'; selectedDimension = String({dimensions}-1-row);
}} else if (inColumn && inRow) {{
    selectedRater = String(column); selectedDimension = String({dimensions}-1-row);
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
        matrix = pn.pane.HoloViews(grading_heatmap(data, subjects, record_id))
        matrix.jscallback(
            args={
                "rater": controls["평가자 · 회차"],
                "dimension": controls["Judge 항목"],
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
                text_block("질문", question),
                text_block("답변", result.get("answer")),
                *([text_block("생성 오류", case["error"])] if case.get("error") else []),
                *(
                    [grounding_scope(result, records[record_id].get("expectations", {}))]
                    if bundle["kind"] == "gold"
                    else []
                ),
                pn.pane.Markdown(
                    "매트릭스 클릭: **셀** → 평가자·항목 / **위 아이콘** → 평가자·전체 / "
                    "**왼쪽 항목명** → 전체·항목 / **좌상단** → 전체·전체",
                    styles={"font-size": "12px", "color": "#526071"},
                ),
                pn.Row(matrix, reasons, sizing_mode="stretch_width"),
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
            "**생성에 전달된 문맥 자료: 미기록**\n\n"
            "생성 문맥이 저장되지 않은 상태입니다. 근거가 0개인 경우와 구분해야 하며, "
            "이 기록만으로 근거 충실도를 판단할 수 없습니다."
        )
    else:
        text = f"**생성에 전달된 문맥 자료: {len(sources)}개**"
        if unanswerable:
            text += "\n\n**데이터셋 기준: 질문에 답할 근거가 부족한 문항**"
        if abstained:
            text += "\n\n**응답 상태: 근거 부족으로 답변 유보**"
        if not sources or unanswerable or abstained:
            text += (
                "\n\n자료가 전달됐더라도 질문에 답할 근거를 확보했다는 뜻은 아닙니다. "
                if sources
                else "\n\n생성에 전달된 문맥 자료가 없습니다. "
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
    """Display frozen generation context and distinguish unused retrieval snapshots."""
    generated = result.get("generation_sources")
    retrieved = result.get("retrieved_sources", [])
    heading = pn.pane.Markdown("### 문맥 자료")
    note = pn.pane.Markdown(
        "**생성 문맥**은 최종 답변 생성에 전달된 본문이며 근거 충실도 검사의 대상입니다. "
        "**검색 전용**은 검색됐지만 같은 본문이 생성 문맥에 포함되지 않은 자료입니다. "
        "인용 번호는 답변의 출처 번호이며, 인용되지 않은 생성 문맥도 검사에 사용됩니다."
    )

    def identity(source):
        return (source.get("chunk_id", source.get("url")), source.get("text"))

    selected = {identity(s) for s in generated or []}
    entries = [("생성 문맥", i, source) for i, source in enumerate(generated or [], 1)]
    entries.extend(
        ("검색 전용", i, source)
        for i, source in enumerate(retrieved, 1)
        if identity(source) not in selected
    )
    missing = (
        "생성 문맥이 기록되지 않았습니다."
        if generated is None
        else "이 답변에 전달된 생성 문맥은 없습니다."
        if not generated
        else ""
    )
    if not entries:
        return pn.Column(heading, note, pn.pane.Markdown(missing), sizing_mode="stretch_width")
    views, options = [], {}
    for index, (scope, number, source) in enumerate(entries):
        citations = [
            str(c["source_id"])
            for c in result.get("citations", [])
            if scope == "생성 문맥"
            and source.get("chunk_id")
            and c.get("chunk_id") == source["chunk_id"]
        ]
        citation = " · 인용 [" + ", ".join(citations) + "]" if citations else ""
        title = source.get("title") or "제목 미기록"
        options[f"{scope} {number}{citation} · {title}"] = str(index)
        url = source.get("url", "")
        reference = (
            f'<a href="{escape(url, quote=True)}" target="_blank" rel="noopener noreferrer">'
            f"{escape(url)} ↗</a>"
            if urlsplit(url).scheme in {"http", "https"}
            else escape(url)
        )
        view = pn.pane.HTML(
            f'<h4 style="margin:0 0 8px">{escape(scope)} {number}{escape(citation)} · '
            f"{escape(title)}</h4>"
            f'<div style="overflow-wrap:anywhere">{reference}</div>'
            f'<p style="color:#64748b">{escape(source.get("heading_path") or "섹션 미기록")}</p>'
            '<div style="white-space:pre-wrap;overflow-wrap:anywhere;line-height:1.7">'
            + escape(source.get("text") or "본문 미기록")
            + "</div>",
            visible=index == 0,
            sizing_mode="stretch_width",
        )
        views.append(view)
    selector = pn.widgets.Select(
        label="문맥 선택", options=options, value="0", sizing_mode="stretch_width"
    )
    selector.jscallback(
        args={f"item_{i}": view for i, view in enumerate(views)},
        value=item_switch_code(len(views)),
    )
    return pn.Column(
        heading,
        note,
        *([pn.pane.Markdown(missing)] if missing else []),
        selector,
        pn.Column(*views, height=320, scroll=True, sizing_mode="stretch_width"),
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
        f'title="{escape(subject["label"], quote=True)}"></td><td>'
        + rationale_markup(row["rationale"])
        + '</td><td><span style="display:inline-block;padding:2px 7px;border-radius:5px;'
        f'background:{row["fill"]};color:{row["ink"]}">'
        + escape(row["score_display"])
        + "</span></td></tr>"
    )


def rationale_browser(data, subjects, record_id):
    """Group reasons by dimension and filter the frozen table rows entirely offline."""
    rubric = data.report["rubric"]
    dimensions = DIMENSIONS if data.inputs["bundle"]["kind"] == "gold" else ("behavior",)
    cells = grading_cells(data, subjects, record_id, dimensions)
    raters = {"전체 평가자": "all"}
    raters.update({s["label"]: str(i) for i, s in enumerate(subjects)})
    initial = next((i for i, s in enumerate(subjects) if s["rater"].startswith("model:")), 0)
    rater = pn.widgets.Select(label="평가자 · 회차", options=raters, value=str(initial))
    titles = {d: f"{rubric['dimensions'][d]['title']} · {d}" for d in dimensions}
    dimension = pn.widgets.Select(
        label="Judge 항목",
        options={"전체 항목": "all", **{titles[d]: str(i) for i, d in enumerate(dimensions)}},
        value="0",
    )
    args = {"rater": rater, "dimension": dimension}
    rows, groups = [], []
    for j, name in enumerate(dimensions):
        entries = [
            rationale_row(subject, cells[(cells.x == i) & (cells.dimension == name)].iloc[0])
            for i, subject in enumerate(subjects)
        ]
        rows.append(entries)
        table = pn.pane.HTML(
            RATIONALE_TABLE_START + entries[initial] + RATIONALE_TABLE_END,
            stylesheets=[RATIONALE_STYLE],
            sizing_mode="stretch_width",
        )
        group = pn.Column(
            pn.pane.HTML(f'<h4 style="margin:12px 0 8px">{escape(titles[name])}</h4>'),
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
