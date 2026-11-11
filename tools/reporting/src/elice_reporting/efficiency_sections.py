"""Interactive views of separately recorded answer and judge efficiency."""

import json
from collections import Counter, defaultdict
from statistics import mean

import panel as pn
from bokeh.models import ColumnDataSource, HoverTool, LabelSet
from bokeh.plotting import figure
from bokeh.transform import jitter

from .agreement_visuals import agreement_subjects
from .comparison_sections import _selector
from .presentation import metric_label

ANSWER_METRICS = {
    "답변 시간": ("duration_ms", "초"),
    "입력 토큰": ("prompt_tokens", "토큰"),
    "캐시 입력 토큰": ("cached_tokens", "토큰"),
    "출력 토큰": ("completion_tokens", "토큰"),
    "전체 토큰": ("total_tokens", "토큰"),
}
JUDGE_METRICS = {
    "전체 채점 경과 시간": ("evaluation_elapsed_ms", "초"),
    "호출 시간 합": ("duration_ms", "초"),
    "입력 토큰": ("input_tokens", "토큰"),
    "캐시 입력 토큰": ("cached_input_tokens", "토큰"),
    "출력 토큰": ("output_tokens", "토큰"),
    "전체 토큰": ("total_tokens", "토큰"),
}
DETAIL_METRICS = {
    "항목 경과 시간": ("scorer_duration_ms", "초"),
    **{k: v for k, v in JUDGE_METRICS.items() if k != "전체 채점 경과 시간"},
}


def _value(value, unit):
    if value is None:
        return "미기록"
    return f"{value:,.1f}초" if unit == "초" else f"{value:,.0f} 토큰"


def _chart(groups, unit, title, *, repeat=False):
    """Horizontal bars show observed means; points retain individual attempts."""
    if not groups:
        return pn.pane.Markdown("표시할 실행 기록이 없습니다.")
    categories = [group["label"] for group in groups]
    bars, points = [], []
    for group in groups:
        valid = [point for point in group["points"] if point["value"] is not None]
        # Within each answer generation, average grading rounds first; then weight
        # independent answers equally, even when round counts differ.
        by_answer = defaultdict(list)
        for point in valid:
            by_answer[point["answer"]].append(point["value"])
        value = mean(mean(values) for values in by_answer.values()) if by_answer else None
        bars.append(
            {
                "category": group["label"],
                "value": value,
                "text": _value(value, unit),
                "support": (
                    valid[0]["support"]
                    if not repeat and valid
                    else f"{len(valid)}/{len(group['points'])}회 측정"
                ),
            }
        )
        for point in valid:
            points.append(
                {
                    "category": group["label"],
                    "value": point["value"],
                    "attempt": point["attempt"],
                    "support": point["support"],
                    "text": _value(point["value"], unit),
                }
            )
    maximum = max((row["value"] for row in [*bars, *points] if row["value"] is not None), default=0)
    upper = max(maximum * 1.38, 1)
    plot = figure(
        y_range=list(reversed(categories)),
        x_range=(0, upper),
        height=max(220, 80 + len(categories) * 58),
        min_border_left=15,
        toolbar_location=None,
        tools="",
        sizing_mode="stretch_width",
        title=title,
    )
    bar_source = ColumnDataSource({key: [row[key] for row in bars] for key in bars[0]})
    bars_renderer = plot.hbar(
        y="category",
        right="value",
        height=0.48,
        source=bar_source,
        color="#648ba0",
        alpha=0.84,
        legend_label="시행 평균" if repeat else "측정값",
    )
    plot.add_layout(
        LabelSet(
            x="value",
            y="category",
            text="text",
            source=bar_source,
            x_offset=7,
            y_offset=-5,
            text_font_size="10pt",
            text_color="#334e5c",
        )
    )
    # Missing values have no bar or zero-valued observation.
    missing = [row for row in bars if row["value"] is None]
    if missing:
        source = ColumnDataSource(
            {
                "category": [r["category"] for r in missing],
                "position": [upper * 0.015] * len(missing),
                "text": ["미기록"] * len(missing),
            }
        )
        plot.add_layout(
            LabelSet(
                x="position",
                y="category",
                text="text",
                source=source,
                y_offset=-5,
                text_color="#8795a0",
                text_font_size="10pt",
            )
        )
    plot.add_tools(
        HoverTool(
            renderers=[bars_renderer],
            tooltips=[
                ("대상", "@category"),
                ("평균" if repeat else "측정값", "@text"),
                ("관측", "@support"),
            ],
        )
    )
    if repeat and points:
        point_source = ColumnDataSource({key: [row[key] for row in points] for key in points[0]})
        dots = plot.scatter(
            x="value",
            y=jitter("category", width=0.28, range=plot.y_range),
            source=point_source,
            size=9,
            color="#efaa7e",
            line_color="white",
            line_width=1.3,
            legend_label="개별 시행",
        )
        plot.add_tools(
            HoverTool(
                renderers=[dots],
                tooltips=[
                    ("대상", "@category"),
                    ("시행", "@attempt"),
                    ("값", "@text"),
                    ("관측", "@support"),
                ],
            )
        )
    plot.xaxis.axis_label = unit
    plot.xgrid.grid_line_color = "#e8eef2"
    plot.ygrid.visible = False
    plot.outline_line_color = None
    plot.background_fill_color = "#fbfcfd"
    plot.legend.orientation = "horizontal"
    plot.legend.location = "top_right"
    plot.legend.background_fill_alpha = 0
    plot.legend.border_line_alpha = 0
    return pn.pane.Bokeh(plot, sizing_mode="stretch_width")


def _answer_view(snapshots):
    options = []
    for label, (field, unit) in ANSWER_METRICS.items():
        points = []
        for index, snapshot in enumerate(snapshots, 1):
            cases = snapshot["bundle"]["cases"]
            successful = [case for case in cases if not case.get("error")]
            values = []
            for case in successful:
                raw = (
                    case.get("duration_ms")
                    if field == "duration_ms"
                    else ((case.get("result") or {}).get("usage") or {}).get(field)
                )
                if raw is not None:
                    values.append(raw / 1000 if unit == "초" else raw)
            points.append(
                {
                    "answer": index,
                    "attempt": f"A{index}",
                    "value": mean(values) if values else None,
                    "support": f"성공 {len(successful)}/{len(cases)}문항 · 측정 {len(values)}문항",
                }
            )
        options.append(
            (
                label,
                _chart(
                    [{"label": "답변 생성", "points": points}],
                    unit,
                    label,
                    repeat=len(snapshots) > 1,
                ),
            )
        )
    return pn.Column(
        pn.pane.Markdown(
            "성공한 답변의 문항별 평균입니다. 미기록과 실패는 제외합니다. "
            "여러 시행은 각 시행 평균을 같은 비중으로 평균합니다. "
            "캐시 입력은 입력 토큰에 포함됩니다."
        ),
        _selector(options, "측정 항목"),
        sizing_mode="stretch_width",
    )


def _call_value(calls, field, unit, *, failed=False):
    if not calls:
        return None, "호출 기록 없음"
    values = [c[field] for c in calls if c.get(field) is not None]
    support = f"{len(values)}/{len(calls)}개 응답에서 측정"
    if len(values) != len(calls) or failed:
        return None, support + (" · 채점 오류" if failed else " · 일부 미기록")
    value = sum(values)
    return (value / 1000 if unit == "초" else value), support


def _judge_subjects(sources, repeat):
    groups = {}
    details = []
    for answer_index, source in enumerate(sources, 1):
        record_numbers = {
            r["id"]: (i, r.get("inputs", {}).get("question", ""))
            for i, r in enumerate(source.inputs["bundle"]["records"], 1)
        }
        for subject in agreement_subjects(source):
            if not subject["rater"].startswith("model:"):
                continue
            artifact = subject["grading_link"].get("efficiency")
            key = (subject["rater"], subject["profile"], subject["display_name"])
            if not repeat:
                key += (str(subject["round"]),)
            title = subject["display_name"]
            if subject.get("setting"):
                title += " · " + subject["setting"]
            if not repeat:
                title += f" · {subject['round']}회차"
            group = groups.setdefault(key, {"label": title, "runs": []})
            group["runs"].append((answer_index, subject, artifact))
            if artifact:
                details.append((answer_index, subject, artifact, record_numbers))
    result = list(groups.values())
    counts = Counter(group["label"] for group in result)
    seen = defaultdict(int)
    for group in result:
        label = group["label"]
        if counts[label] > 1:
            seen[label] += 1
            group["label"] = f"{label} · 설정 {seen[label]}"
    return result, details


def _judge_metric(group, field, unit):
    points = []
    for answer_index, subject, artifact in group["runs"]:
        value, support = None, "채점 효율성 미기록"
        if artifact:
            rows = artifact["rows"]
            calls = [call for row in rows for call in row["calls"]]
            errors = sum(row["status"] == "error" for row in rows)
            if field == "evaluation_elapsed_ms":
                raw = artifact.get(field)
                value = raw / 1000 if raw is not None else None
                question_count = len({row["record_id"] for row in rows})
                support = f"{question_count}문항 · HTTP 응답 {len(calls)}회 · 오류 {errors}건"
            else:
                value, support = _call_value(calls, field, unit, failed=bool(errors))
                support = f"{len({row['record_id'] for row in rows})}문항 · {support}"
        points.append(
            {
                "answer": answer_index,
                "attempt": f"A{answer_index} · {subject['round']}회차",
                "value": value,
                "support": support,
            }
        )
    return {"label": group["label"], "points": points}


def _detail_view(details):
    if not details:
        return pn.pane.Markdown("문항별 채점 효율성은 새 Judge 실행부터 기록됩니다.")
    question_labels = {}
    payload = {}
    grading_options = {}
    for index, (answer_index, subject, artifact, records) in enumerate(details):
        grading = str(index)
        grading_options[f"A{answer_index} · {subject['label']}"] = grading
        payload[grading] = {}
        for record_id in dict.fromkeys(row["record_id"] for row in artifact["rows"]):
            number, question = records.get(record_id, ("?", ""))
            key = str(number)
            question_labels[f"Q{number} · {question[:65]}"] = key
            payload[grading][key] = {}
            for metric, (field, unit) in DETAIL_METRICS.items():
                entries = []
                for row in artifact["rows"]:
                    if row["record_id"] != record_id:
                        continue
                    if field == "scorer_duration_ms":
                        raw = row.get(field)
                        value = raw / 1000 if raw is not None else None
                        support = f"판정 상태: {row['status']}"
                    else:
                        value, support = _call_value(
                            row["calls"], field, unit, failed=row["status"] == "error"
                        )
                    entries.append(
                        {
                            "category": metric_label(row["dimension"]),
                            "value": value,
                            "text": _value(value, unit),
                            "support": support,
                        }
                    )
                maximum = max((e["value"] for e in entries if e["value"] is not None), default=0)
                upper = max(maximum * 1.38, 1)
                payload[grading][key][metric] = {
                    "category": [e["category"] for e in entries],
                    "value": [e["value"] for e in entries],
                    "position": [
                        e["value"] if e["value"] is not None else upper * 0.015 for e in entries
                    ],
                    "text": [e["text"] for e in entries],
                    "support": [e["support"] for e in entries],
                    "upper": upper,
                    "unit": unit,
                }
    grade = pn.widgets.Select(
        label="평가자 · 회차", options=grading_options, sizing_mode="stretch_width"
    )
    question = pn.widgets.Select(label="문항", options=question_labels, sizing_mode="stretch_width")
    metric = pn.widgets.Select(
        label="측정 항목", options=list(DETAIL_METRICS), sizing_mode="stretch_width"
    )
    initial = payload.get(grade.value, {}).get(question.value, {}).get(metric.value)
    empty = {
        "category": ["측정 결과 없음"],
        "value": [None],
        "position": [0.015],
        "text": ["미기록"],
        "support": ["해당 시행에 기록 없음"],
        "upper": 1,
        "unit": "",
    }
    initial = initial or empty
    fields = ("category", "value", "position", "text", "support")
    source = ColumnDataSource({key: initial[key] for key in fields})
    plot = figure(
        y_range=list(reversed(initial["category"])),
        x_range=(0, initial["upper"]),
        height=330,
        toolbar_location=None,
        tools="",
        sizing_mode="stretch_width",
    )
    bar = plot.hbar(y="category", right="value", height=0.48, source=source, color="#648ba0")
    plot.add_layout(
        LabelSet(
            x="position",
            y="category",
            text="text",
            source=source,
            x_offset=7,
            y_offset=-5,
            text_font_size="10pt",
        )
    )
    plot.add_tools(
        HoverTool(
            renderers=[bar],
            tooltips=[("평가 항목", "@category"), ("값", "@text"), ("관측", "@support")],
        )
    )
    plot.xaxis.axis_label = initial["unit"]
    plot.xgrid.grid_line_color = "#e8eef2"
    plot.ygrid.visible = False
    plot.outline_line_color = None
    plot.background_fill_color = "#fbfcfd"
    code = (
        "const data=" + json.dumps(payload, ensure_ascii=True) + ";"
        "const empty=" + json.dumps(empty, ensure_ascii=True) + ";"
        "const item=data[grade.value]?.[question.value]?.[metric.value] || empty;"
        "source.data={category:item.category,value:item.value,position:item.position,"
        "text:item.text,support:item.support};"
        "yrange.factors=[...item.category].reverse(); xrange.end=item.upper;"
        "axis.axis_label=item.unit; source.change.emit();"
    )
    args = {
        "source": source,
        "grade": grade,
        "question": question,
        "metric": metric,
        "yrange": plot.y_range,
        "xrange": plot.x_range,
        "axis": plot.xaxis[0],
    }
    for widget in (grade, question, metric):
        widget.jscallback(args=args, value=code)
    return pn.Column(
        pn.pane.Markdown("### 문항·평가 항목별 채점"),
        pn.Row(grade, question, metric, sizing_mode="stretch_width"),
        pn.pane.Bokeh(plot, sizing_mode="stretch_width"),
        sizing_mode="stretch_width",
    )


def _judge_view(sources, repeat):
    groups, details = _judge_subjects(sources, repeat)
    options = []
    for metric, (field, unit) in JUDGE_METRICS.items():
        options.append(
            (
                metric,
                _chart(
                    [_judge_metric(g, field, unit) for g in groups], unit, metric, repeat=repeat
                ),
            )
        )
    return pn.Column(
        pn.pane.Markdown(
            "전체 채점 경과 시간은 실제 대기 시간입니다. 호출 시간 합은 병렬 요청의 "
            "시간이 겹칠 수 있습니다. 응답 전에 실패하거나 토큰이 일부 미기록인 합계는 "
            "미기록으로 표시합니다. 전체 채점량이 다를 수 있으므로 관측 문항과 호출 수를 "
            "함께 확인하세요. 사람 검수 시간은 포함하지 않습니다."
        ),
        pn.pane.Markdown(
            "막대는 answers 시행별 채점 회차를 먼저 평균하고, 시행을 같은 비중으로 평균합니다. "
            "점은 개별 채점 회차이며, 마우스를 올리면 시행과 회차를 확인할 수 있습니다."
            if repeat
            else ""
        ),
        _selector(options, "측정 항목"),
        _detail_view(details),
        sizing_mode="stretch_width",
    )


def efficiency_view(data):
    if data.kind == "agreement":
        snapshots, sources = [data.inputs], [data]
    else:
        snapshots = data.inputs["agreements"]
        sources = data.links.get("source_agreements", [])
    return _selector(
        [
            ("답변 생성", _answer_view(snapshots)),
            ("Judge 채점", _judge_view(sources, len(snapshots) > 1)),
        ],
        "효율성 범주",
    )
