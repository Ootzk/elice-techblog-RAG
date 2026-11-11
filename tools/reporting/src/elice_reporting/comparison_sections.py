"""Compact, offline A/B views of frozen comparison artifacts."""

from html import escape

import pandas as pd
import panel as pn
from bokeh.models import ColumnDataSource, HoverTool
from bokeh.palettes import interp_palette
from bokeh.plotting import figure
from markupsafe import Markup

from .agreement_sections import (
    _run_reference,
    _table,
    condition_selector,
    item_switch_code,
    rubric_guide,
)
from .agreement_visuals import _icon
from .conditions import condition_sections
from .presentation import content_card, metric_label, model_name, text_block

DIMENSION_ORDER = ("correctness", "completeness", "groundedness", "relevance", "behavior")
DELTA_COLORS = interp_palette(("#bd5c55", "#e8b1a0", "#f3f5f6", "#9dcbc5", "#277b80"), 101)
RESOURCE_GROUPS = {
    "완료·시간": ("failure_rate", "qa_duration_ms"),
    "토큰": ("input_tokens", "cached_input_tokens", "output_tokens", "total_tokens"),
    "검색·모델 호출": ("llm_call_count", "tool_call_count"),
}
RESOURCE_LABELS = {
    "failure_rate": "답변 생성 실패율",
    "qa_duration_ms": "평균 답변 시간",
    "input_tokens": "입력 토큰",
    "cached_input_tokens": "캐시 입력 토큰",
    "output_tokens": "출력 토큰",
    "total_tokens": "총 토큰",
    "llm_call_count": "모델 호출 수",
    "tool_call_count": "검색 도구 호출 수",
}


def _rater_name(rater):
    return {
        "model:chatgpt": "ChatGPT",
        "model:claude": "Claude",
        "model:gemini": "Gemini",
    }.get(rater, "사람 검수" if rater.startswith("human:") else rater)


def _icon_markup(rater, round_=None):
    return (
        '<img src="'
        + escape(_icon(rater, "#64748b", round_, [_rater_name(rater)]), quote=True)
        + '" width="32" height="32" alt="">'
    )


def _display(value, *, metric=None, signed=False):
    if value is None or pd.isna(value):
        return "—"
    if metric == "failure_rate":
        return f"{value:+.0%}" if signed else f"{value:.0%}"
    if metric == "qa_duration_ms":
        return f"{value / 1000:+.1f}초" if signed else f"{value / 1000:.1f}초"
    return f"{value:+.2f}" if signed else f"{value:.2f}"


def _ab_table(rows):
    body = []
    for label, left, right, changed in rows:
        style = ' style="background:#e8f5f2;border-left:3px solid #27877d"' if changed else ""
        body.append(
            f"<tr{style}><th>{str(label) if isinstance(label, Markup) else escape(str(label))}</th>"
            f"<td>{left if isinstance(left, Markup) else escape(str(left))}</td>"
            f"<td>{right if isinstance(right, Markup) else escape(str(right))}</td></tr>"
        )
    return pn.pane.HTML(
        '<table class="ab-table"><thead><tr><th>조건</th>'
        "<th>A · 기준</th><th>B · 변경</th></tr></thead>"
        "<tbody>" + "".join(body) + "</tbody></table>",
        stylesheets=[
            ".ab-table{width:100%;border-collapse:collapse;font:13px/1.5 system-ui;}"
            ".ab-table th,.ab-table td{padding:11px 13px;border-bottom:1px solid #e4e9ef;"
            "vertical-align:top;text-align:left;overflow-wrap:anywhere;}"
            ".ab-table thead th{color:#526071;background:#f3f6f8;font-size:12px;}"
            ".ab-table tbody th{width:29%;color:#45576a;font-weight:600;}"
        ],
        sizing_mode="stretch_width",
    )


def _settings(profile):
    fields = (
        ("generation_model", "답변 모델"),
        ("top_k", "검색당 결과 수"),
        ("qa_agent_max_tool_calls", "최대 검색 도구 호출"),
        ("qa_max_context_tokens", "참고 자료 예산 · 토큰"),
        ("qa_history_pairs", "대화 이력 · 쌍"),
        ("generation_max_output_tokens", "답변 출력 한도 · 토큰"),
        ("generation_temperature", "생성 온도"),
        ("generation_timeout_seconds", "생성 제한 시간 · 초"),
    )
    values = {
        title: model_name(profile[key])
        if key == "generation_model"
        else "공급자 기본값"
        if profile[key] is None
        else str(profile[key])
        for key, title in fields
        if key in profile
    }
    for name, prompt in profile.get("prompts", {}).items():
        if isinstance(prompt, dict):
            label = "검색 계획 프롬프트" if "planner" in name else "답변 프롬프트"
            values[label] = f"{prompt.get('name', name)} · v{prompt.get('version', '?')}"
    return values


def _rows(left, right):
    return [
        (
            key,
            left.get(key, "미기록"),
            right.get(key, "미기록"),
            left.get(key, "미기록") != right.get(key, "미기록"),
        )
        for key in dict.fromkeys((*left, *right))
    ]


def _selector(categories, label):
    if not categories:
        return pn.pane.Markdown("표시할 결과가 없습니다.")
    views = [
        pn.Column(content, visible=i == 0, sizing_mode="stretch_width")
        for i, (_, content) in enumerate(categories)
    ]
    select = pn.widgets.Select(
        label=label,
        options={name: str(i) for i, (name, _) in enumerate(categories)},
        value="0",
        sizing_mode="stretch_width",
    )
    select.jscallback(
        args={f"item_{i}": view for i, view in enumerate(views)}, value=item_switch_code(len(views))
    )
    return pn.Column(select, *views, sizing_mode="stretch_width")


def comparison_conditions(data):
    """Summarize changes, then display shared and changed A/B values by category."""
    left = data.inputs["baseline"]
    right = data.inputs["candidate"]
    a, b = left["bundle"], right["bundle"]
    ap, bp = a["qa_profile"], b["qa_profile"]
    changes = []
    endpoint_changed = False
    for item in data.report["configuration_changes"]:
        if item["path"] == "qa_profile.generation_model":
            changes.append(
                ("답변 모델", model_name(item["baseline"]), model_name(item["candidate"]))
            )
        elif item["path"] == "qa_profile.provider_identity":
            endpoint_changed = True
        else:
            changes.append((item["path"].removeprefix("qa_profile."), "A 설정", "B 설정"))
    for item in data.report.get("grading_profile_changes", []):
        changes.append((f"{_rater_name(item['rater'])} 채점 설정", "A 설정", "B 설정"))
    summary = (
        _ab_table([(name, old, new, True) for name, old, new in changes])
        if changes
        else pn.pane.Markdown("A/B에 기록된 설정 차이가 없습니다.")
    )
    endpoint_note = (
        pn.pane.Markdown("생성 API 주소도 모델과 함께 변경됐습니다. 실제 주소는 표시하지 않습니다.")
        if endpoint_changed
        else None
    )
    links = data.links
    dataset = {
        "데이터셋": "🥇 Gold" if a["kind"] == "gold" else "🦾 Iron",
        "평가 문항": f"{len(a['records'])}문항",
        "공통 문항": f"{len(data.report['coverage']['shared_record_ids'])}문항",
        "판정 기준": left["agreement"]["rubric"]["id"],
    }
    categories = [
        (
            "데이터셋",
            _ab_table(
                _rows(
                    dataset,
                    {
                        "데이터셋": "🥇 Gold" if b["kind"] == "gold" else "🦾 Iron",
                        "평가 문항": f"{len(b['records'])}문항",
                        "공통 문항": f"{len(data.report['coverage']['shared_record_ids'])}문항",
                        "판정 기준": right["agreement"]["rubric"]["id"],
                    },
                )
            ),
        )
    ]
    settings_a, settings_b = _settings(ap), _settings(bp)
    categories.append(
        (
            "답변·검색 설정",
            _ab_table(_rows(settings_a, settings_b)),
        )
    )
    for (title_a, rows_a), (title_b, rows_b) in zip(
        condition_sections(ap.get("experiment_conditions")),
        condition_sections(bp.get("experiment_conditions")),
        strict=True,
    ):
        if title_a != title_b:
            raise ValueError("A/B experiment condition categories differ")
        categories.append((title_a, _ab_table(_rows(dict(rows_a), dict(rows_b)))))
    raters = sorted({g["rater_id"] for g in left["gradings"] + right["gradings"]})
    subject_rows = []
    for rater in raters:
        counts = [
            sum(g["rater_id"] == rater for g in snapshot["gradings"]) for snapshot in (left, right)
        ]
        subject_rows.append(
            (
                Markup(_icon_markup(rater) + " " + escape(_rater_name(rater))),
                f"grading {counts[0]}회",
                f"grading {counts[1]}회",
                counts[0] != counts[1],
            )
        )
    categories.append(("평가 주체", _ab_table(subject_rows)))
    source_rows = []
    for label, key in (("원본 답변", "answers"), ("원본 Agreement", "agreement")):
        source_rows.append(
            (
                label,
                _run_reference(links.get("baseline", {}).get(key, {}), "A 기록"),
                _run_reference(links.get("candidate", {}).get(key, {}), "B 기록"),
                False,
            )
        )
    categories[0] = ("데이터셋", pn.Column(categories[0][1], _ab_table(source_rows)))
    return pn.Column(
        text_block("비교 가설", data.report["hypothesis"]),
        pn.pane.Markdown("### A/B에서 달라진 조건"),
        summary,
        *([endpoint_note] if endpoint_note is not None else []),
        pn.pane.Markdown("### 전체 평가 조건"),
        condition_selector(categories),
        sizing_mode="stretch_width",
    )


def _quality_matrix(rows, raters, dimensions):
    indexed = {(r["rater"], r["metric"].split(".")[1]): r for r in rows}
    labels = [_rater_name(rater) for rater in raters]
    cells = []
    for rater in raters:
        for dimension in dimensions:
            row = indexed.get((rater, dimension))
            delta = row["delta_mean"] if row else None
            valid = delta is not None and not pd.isna(delta)
            offset = max(0, min(100, round((float(delta) + 2) * 25))) if valid else None
            cells.append(
                dict(
                    x=metric_label(dimension),
                    y=_rater_name(rater),
                    color=DELTA_COLORS[offset] if valid else "#e4e9ee",
                    label=_display(delta, signed=True) if valid else "—",
                    rater=_rater_name(rater),
                    dimension=metric_label(dimension),
                    baseline=_display(row["baseline_mean"]) if row else "—",
                    candidate=_display(row["candidate_mean"]) if row else "—",
                    paired=str(row["paired_cases"]) if row else "0",
                    excluded=str(row["excluded_cases"]) if row else "—",
                    interval=(
                        f"{_display(row['confidence_interval'][0], signed=True)} ~ "
                        f"{_display(row['confidence_interval'][1], signed=True)}"
                        if row and row.get("confidence_interval")
                        else "계산 불가"
                    ),
                )
            )
    source = ColumnDataSource(pd.DataFrame(cells))
    plot = figure(
        x_range=[metric_label(d) for d in dimensions],
        y_range=list(reversed(labels)),
        width=830,
        height=max(190, 85 + len(raters) * 60),
        toolbar_location=None,
        tools="",
        sizing_mode="stretch_width",
        min_border_left=110,
        min_border_bottom=50,
    )
    rect = plot.rect(
        "x", "y", width=0.94, height=0.88, fill_color="color", line_color="white", source=source
    )
    plot.text(
        "x",
        "y",
        text="label",
        text_align="center",
        text_baseline="middle",
        text_color="#253449",
        text_font_style="bold",
        text_font_size="11pt",
        source=source,
    )
    plot.add_tools(
        HoverTool(
            renderers=[rect],
            tooltips=[
                ("평가 주체", "@rater"),
                ("평가 항목", "@dimension"),
                ("A 평균", "@baseline"),
                ("B 평균", "@candidate"),
                ("B−A", "@label"),
                ("대응 문항", "@paired"),
                ("제외 문항", "@excluded"),
                ("95% 재표본 구간", "@interval"),
            ],
        )
    )
    plot.grid.visible = False
    plot.outline_line_color = None
    plot.background_fill_color = "#fbfcfd"
    plot.xaxis.major_label_orientation = 0.25
    plot.xaxis.major_label_text_font_size = "10pt"
    return pn.pane.Bokeh(plot, sizing_mode="stretch_width")


def comparison_quality(data):
    rows = [row for row in data.report["quality"] if row["metric"].startswith("rubric.")]
    if not rows:
        return pn.pane.Markdown("비교 가능한 채점 결과가 없습니다.")
    raters = list(dict.fromkeys(row["rater"] for row in rows))
    dimensions = [
        d for d in DIMENSION_ORDER if any(r["metric"] == f"rubric.{d}.mean" for r in rows)
    ]
    legend = pn.pane.HTML(
        '<div style="display:flex;gap:15px;flex-wrap:wrap;align-items:center">'
        + "".join(_icon_markup(rater) + " " + escape(_rater_name(rater)) for rater in raters)
        + '</div><div style="color:#64748b;font:12px system-ui;margin-top:7px">'
        "값은 B−A · 청록은 상승, 붉은색은 하락 · —는 대응 점수 없음 · "
        "셀에 마우스를 올리면 원점수와 표본 수를 표시합니다.</div>",
        sizing_mode="stretch_width",
    )
    options = [("전체 평가 주체", raters)] + [(_rater_name(r), [r]) for r in raters]
    views = [
        pn.Column(_quality_matrix(rows, selected, dimensions), visible=i == 0)
        for i, (_, selected) in enumerate(options)
    ]
    select = pn.widgets.Select(
        label="평가 주체", options={name: str(i) for i, (name, _) in enumerate(options)}, value="0"
    )
    select.jscallback(
        args={f"item_{i}": view for i, view in enumerate(views)}, value=item_switch_code(len(views))
    )
    return pn.Column(legend, select, *views, sizing_mode="stretch_width")


def comparison_resources(data):
    rows = {row["metric"]: row for row in data.report["resources"]}
    categories = []
    for name, metrics in RESOURCE_GROUPS.items():
        table_rows = []
        for metric in metrics:
            if metric not in rows:
                continue
            row = rows[metric]
            table_rows.append(
                (
                    RESOURCE_LABELS[metric],
                    _display(row["baseline_mean"], metric=metric),
                    _display(row["candidate_mean"], metric=metric),
                    _display(row["delta_mean"], metric=metric, signed=True),
                    f"{row['paired_cases']} / {row['paired_cases'] + row['excluded_cases']}",
                )
            )
        if table_rows:
            html = (
                '<table class="resource-table"><thead><tr><th>지표</th>'
                "<th>A</th><th>B</th><th>B−A</th><th>대응 / 전체</th></tr></thead><tbody>"
            )
            html += "".join(
                "<tr>" + "".join(f"<td>{escape(str(v))}</td>" for v in item) + "</tr>"
                for item in table_rows
            )
            html += "</tbody></table>"
            categories.append(
                (
                    name,
                    pn.pane.HTML(
                        html,
                        stylesheets=[
                            ".resource-table{width:100%;border-collapse:collapse;"
                            "font:13px system-ui;}"
                            ".resource-table th,.resource-table td{padding:11px;"
                            "border-bottom:1px solid #e4e9ef;text-align:left;}"
                            ".resource-table th{color:#526071;background:#f3f6f8;}"
                        ],
                    ),
                )
            )
    return _selector(categories, "실행 지표 범주")


def comparison_reliability(data):
    pairs = data.report["reliability"]["pairs"]
    categories = []
    for dimension in DIMENSION_ORDER:
        selected = [row for row in pairs if row["dimension"] == dimension]
        if not selected:
            continue
        rows = []
        for row in selected:
            label = " ↔ ".join(_rater_name(rater) for rater in row["raters"])
            a = row["baseline"]["cohen_kappa"]
            b = row["candidate"]["cohen_kappa"]
            rows.append((label, _display(a), _display(b), a != b))
        categories.append((metric_label(dimension), _ab_table(rows)))
    return (
        pn.Column(
            pn.pane.Markdown(
                "같은 평가자 쌍의 **선형 가중 Cohen κ**를 비교합니다. "
                "유효한 판정 쌍이 적거나 등급이 한쪽으로 몰리면 계산되지 않을 수 있습니다."
            ),
            _selector(categories, "평가 항목"),
        )
        if categories
        else pn.pane.Markdown("비교 가능한 평가자 일치도가 없습니다.")
    )


def _case_scores(data, record_id):
    labels = {r: _rater_name(r) for r in {row["rater"] for row in data.report["quality"]}}
    rows = [row for row in data.report["quality"] if row["metric"].startswith("rubric.")]
    dimensions = [
        d for d in DIMENSION_ORDER if any(r["metric"] == f"rubric.{d}.mean" for r in rows)
    ]
    html = '<table class="case-scores"><thead><tr><th>평가 주체</th>'
    html += (
        "".join(f"<th>{escape(metric_label(d))}</th>" for d in dimensions) + "</tr></thead><tbody>"
    )
    for rater in labels:
        html += "<tr><th>" + _icon_markup(rater) + " " + escape(labels[rater]) + "</th>"
        for dimension in dimensions:
            row = next(
                (
                    r
                    for r in rows
                    if r["rater"] == rater and r["metric"] == f"rubric.{dimension}.mean"
                ),
                None,
            )
            case = (
                next((c for c in row["cases"] if c["record_id"] == record_id), None)
                if row
                else None
            )
            a, b = (case.get("baseline"), case.get("candidate")) if case else (None, None)
            change = case.get("delta") if case else None
            style = (
                ' style="background:#e9f5f2"'
                if change is not None and change > 0
                else ' style="background:#faebe8"'
                if change is not None and change < 0
                else ""
            )
            html += f"<td{style}>{escape(_display(a))} → {escape(_display(b))}</td>"
        html += "</tr>"
    html += "</tbody></table>"
    return pn.pane.HTML(
        html,
        stylesheets=[
            ".case-scores{width:100%;border-collapse:collapse;font:13px system-ui;}"
            ".case-scores th,.case-scores td{padding:10px;"
            "border-bottom:1px solid #e4e9ef;text-align:left;vertical-align:middle;}"
            ".case-scores th{color:#526071;background:#f3f6f8;}"
            ".case-scores img{vertical-align:middle;}"
        ],
    )


def comparison_questions(data):
    left = data.inputs["baseline"]["bundle"]
    right = data.inputs["candidate"]["bundle"]
    records = {r["id"]: r for r in left["records"]}
    cases = [{c["record_id"]: c for c in bundle["cases"]} for bundle in (left, right)]
    ids = data.report["coverage"]["shared_record_ids"]
    categories = []
    for index, record_id in enumerate(ids, 1):
        question = records[record_id]["inputs"]["question"]
        left_case, right_case = (case[record_id] for case in cases)
        answers = []
        evidence_categories = []
        for label, case, color in (
            ("A · 기준 답변", left_case, "#2563eb"),
            ("B · 변경 답변", right_case, "#0f766e"),
        ):
            result = case.get("result") or {}
            value = result.get("answer") or (
                f"생성 실패: {case['error']}" if case.get("error") else "답변 기록 없음"
            )
            answers.append(
                content_card(label, value, markdown=bool(result.get("answer")), accent=color)
            )
            for source_index, source in enumerate(result.get("generation_sources") or [], 1):
                heading = source.get("title") or source.get("url") or f"자료 {source_index}"
                evidence_categories.append(
                    (
                        f"{label[0]} · {source_index}. {heading[:75]}",
                        content_card(
                            heading,
                            source.get("text") or "자료 내용 미기록",
                            markdown=True,
                            accent=color,
                        ),
                    )
                )
        categories.append(
            (
                f"{index}. {question[:88]}{'…' if len(question) > 88 else ''}",
                pn.Column(
                    content_card("💬 질문", question, accent="#64748b"),
                    pn.Row(*answers, sizing_mode="stretch_width"),
                    pn.pane.Markdown("#### 평가자별 원점수 · A → B"),
                    _case_scores(data, record_id),
                    pn.pane.Markdown("#### 답변 작성에 제공된 자료"),
                    _selector(evidence_categories, "자료 선택")
                    if evidence_categories
                    else pn.pane.Markdown("제공된 자료가 기록되지 않았습니다."),
                    sizing_mode="stretch_width",
                ),
            )
        )
    return _selector(categories, "문항 선택")


def comparison_metric_guide(data):
    rubric = data.inputs["baseline"]["agreement"]["rubric"]
    return pn.Column(
        *rubric_guide(rubric).objects,
        pn.pane.Markdown("### 비교 지표"),
        _table(
            [
                (
                    "B−A",
                    "같은 문항에서 B 점수에서 A 점수를 뺀 값입니다. "
                    "대응 점수가 없는 문항은 평균 차이에서 제외합니다.",
                ),
                (
                    "95% 재표본 구간",
                    "저장된 대응 문항을 재표본 추출한 평균 차이 구간입니다. "
                    "대응 문항이 부족하면 계산하지 않습니다.",
                ),
                (
                    "답변 생성 실패율",
                    "전체 공통 문항 중 답변 생성에 실패한 비율입니다. "
                    "품질 등급의 0점과 구분합니다.",
                ),
                ("응답 시간·토큰·호출 수", "양쪽에 값이 기록된 문항만 대응시켜 평균을 비교합니다."),
            ]
        ),
        sizing_mode="stretch_width",
    )
