"""Interactive views of separately recorded answer and judge efficiency."""

import json
from collections import Counter, defaultdict
from statistics import mean

import panel as pn
from bokeh.models import ColumnDataSource, HoverTool, LabelSet
from bokeh.plotting import figure

from .agreement_visuals import agreement_subjects
from .comparison_sections import _selector
from .presentation import metric_label
from .stacked_efficiency import answer_groups, judge_vector, stack_chart

JUDGE_METRICS = {
    "전체 채점 경과 시간": ("evaluation_elapsed_ms", "초"),
    "호출 시간 합": ("duration_ms", "초"),
}

DETAIL_METRICS = {
    "항목 경과 시간": ("scorer_duration_ms", "초"),
    **{k: v for k, v in JUDGE_METRICS.items() if k != "전체 채점 경과 시간"},
}


def _value(value, unit):
    if value is None:
        return "미기록"
    if unit == "초":
        return f"{value:,.1f}초"
    return f"{value:,.1f} 회" if unit == "회" else f"{value:,.0f} 토큰"


def _chart(groups, unit, title, *, repeat=False):
    """Horizontal bars show observed means."""
    if not groups:
        return pn.pane.Markdown("표시할 실행 기록이 없습니다.")
    categories = [group["label"] for group in groups]
    bars = []
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
    maximum = max((row["value"] for row in bars if row["value"] is not None), default=0)
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
    points = []
    for index, snapshot in enumerate(snapshots, 1):
        cases = snapshot["bundle"]["cases"]
        successful = [case for case in cases if not case.get("error")]
        values = [
            case["duration_ms"] / 1000 for case in successful if case.get("duration_ms") is not None
        ]
        points.append(
            {
                "answer": index,
                "attempt": f"A{index}",
                "value": mean(values) if values else None,
                "support": f"성공 {len(successful)}/{len(cases)}문항 · 측정 {len(values)}문항",
            }
        )
    repeat = len(snapshots) > 1
    options = [
        (
            "답변 시간",
            _chart([{"label": "답변 생성", "points": points}], "초", "답변 시간", repeat=repeat),
        ),
        (
            "토큰 사용량",
            stack_chart(
                answer_groups(snapshots, "tokens"), "tokens", "질문당 토큰 사용량", repeat=repeat
            ),
        ),
        (
            "검색 도구 호출",
            stack_chart(
                answer_groups(snapshots, "tools"), "tools", "질문당 검색 도구 호출", repeat=repeat
            ),
        ),
    ]
    return pn.Column(
        pn.pane.Markdown(
            "성공한 답변의 문항별 평균입니다. 입력과 출력을 함께 측정한 문항만 "
            "토큰 그래프에 포함합니다. 캐시 입력은 입력의 일부이며 —은 캐시 미기록입니다. "
            "검색 도구 호출에는 실패한 도구 호출도 포함합니다."
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
            metric_specs = {**DETAIL_METRICS, "토큰 사용량": ("tokens", "토큰")}
            for metric, (field, unit) in metric_specs.items():
                entries = []
                for row in artifact["rows"]:
                    if row["record_id"] != record_id:
                        continue
                    vector = None
                    if field == "tokens":
                        vector = judge_vector(row["calls"], failed=row["status"] == "error")
                        value = vector["first"] + vector["second"] if vector else None
                        support = f"HTTP 응답 {len(row['calls'])}회 · 상태 {row['status']}"
                    elif field == "scorer_duration_ms":
                        raw = row.get(field)
                        value = raw / 1000 if raw is not None else None
                        support = f"판정 상태: {row['status']}"
                    else:
                        value, support = _call_value(
                            row["calls"], field, unit, failed=row["status"] == "error"
                        )
                    text = _value(value, unit)
                    detail = text
                    if vector:
                        cache_text = (
                            f"캐시 {vector['cached']:,.0f}"
                            if vector["cached"] is not None
                            else "캐시 미기록"
                        )
                        detail = (
                            f"입력 {vector['first']:,.0f} · 출력 {vector['second']:,.0f} "
                            f"· 전체 {value:,.0f} 토큰 · {cache_text}"
                        )
                        text = f"{value:,.0f}"
                    entries.append(
                        {
                            "category": metric_label(row["dimension"]),
                            "value": value,
                            "text": text,
                            "detail": detail,
                            "support": support,
                            "first": vector["first"] if vector else value,
                            "end": value,
                            "cache_left": vector["first"] - vector["cached"]
                            if vector and vector["cached"] is not None
                            else None,
                            "cache_right": vector["first"]
                            if vector and vector["cached"] is not None
                            else None,
                            "color": "#d66c66" if field == "tokens" else "#648ba0",
                            "first_mid": vector["first"] / 2 if vector else None,
                            "second_mid": vector["first"] + vector["second"] / 2
                            if vector
                            else None,
                            "cache_mid": (
                                vector["first"] - vector["cached"] / 2
                                if vector["cached"]
                                else vector["first"] / 2
                            )
                            if vector
                            else None,
                            "first_label": f"{vector['first']:,.0f}" if vector else "",
                            "second_label": f"{vector['second']:,.0f}" if vector else "",
                            "cache_label": (
                                f"{vector['cached']:,.0f}" if vector["cached"] is not None else "—"
                            )
                            if vector
                            else "",
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
                    "detail": [e["detail"] for e in entries],
                    "support": [e["support"] for e in entries],
                    "first": [e["first"] for e in entries],
                    "end": [e["end"] for e in entries],
                    "cache_left": [e["cache_left"] for e in entries],
                    "cache_right": [e["cache_right"] for e in entries],
                    "color": [e["color"] for e in entries],
                    "first_mid": [e["first_mid"] for e in entries],
                    "second_mid": [e["second_mid"] for e in entries],
                    "cache_mid": [e["cache_mid"] for e in entries],
                    "first_label": [e["first_label"] for e in entries],
                    "second_label": [e["second_label"] for e in entries],
                    "cache_label": [e["cache_label"] for e in entries],
                    "upper": upper,
                    "unit": unit,
                }
    grade = pn.widgets.Select(
        label="평가자 · 회차", options=grading_options, sizing_mode="stretch_width"
    )
    question = pn.widgets.Select(label="문항", options=question_labels, sizing_mode="stretch_width")
    metric = pn.widgets.Select(
        label="측정 항목", options=[*DETAIL_METRICS, "토큰 사용량"], sizing_mode="stretch_width"
    )
    initial = payload.get(grade.value, {}).get(question.value, {}).get(metric.value)
    empty = {
        "category": ["측정 결과 없음"],
        "value": [None],
        "position": [0.015],
        "text": ["미기록"],
        "detail": ["미기록"],
        "support": ["해당 시행에 기록 없음"],
        "first": [None],
        "end": [None],
        "cache_left": [None],
        "cache_right": [None],
        "color": ["#648ba0"],
        "first_mid": [None],
        "second_mid": [None],
        "cache_mid": [None],
        "first_label": [""],
        "second_label": [""],
        "cache_label": [""],
        "upper": 1,
        "unit": "",
    }
    initial = initial or empty
    fields = (
        "category",
        "value",
        "position",
        "text",
        "detail",
        "support",
        "first",
        "end",
        "cache_left",
        "cache_right",
        "color",
        "first_mid",
        "second_mid",
        "cache_mid",
        "first_label",
        "second_label",
        "cache_label",
    )
    source = ColumnDataSource({key: initial[key] for key in fields})
    plot = figure(
        y_range=list(reversed(initial["category"])),
        x_range=(0, initial["upper"]),
        height=330,
        toolbar_location=None,
        tools="",
        sizing_mode="stretch_width",
    )
    bar = plot.hbar(y="category", right="first", height=0.48, source=source, color="color")
    output_bar = plot.hbar(
        y="category", left="first", right="end", height=0.48, source=source, color="#5b8f89"
    )
    plot.hbar(
        y="category",
        left="cache_left",
        right="cache_right",
        height=0.48,
        source=source,
        fill_color="#d66c66",
        hatch_pattern="dot",
        hatch_color="white",
        hatch_weight=1.3,
        hatch_scale=7,
        line_color=None,
    )
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
    for x, label, offset in (
        ("first_mid", "first_label", -9),
        ("cache_mid", "cache_label", 4),
        ("second_mid", "second_label", -5),
    ):
        plot.add_layout(
            LabelSet(
                x=x,
                y="category",
                text=label,
                source=source,
                text_align="center",
                y_offset=offset,
                text_font_size="9pt",
                text_font_style="bold",
                text_color="black",
            )
        )
    plot.add_tools(
        HoverTool(
            renderers=[bar, output_bar],
            tooltips=[("평가 항목", "@category"), ("값", "@detail"), ("관측", "@support")],
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
        "text:item.text,detail:item.detail,support:item.support,first:item.first,end:item.end,"
        "cache_left:item.cache_left,cache_right:item.cache_right,color:item.color,"
        "first_mid:item.first_mid,second_mid:item.second_mid,cache_mid:item.cache_mid,"
        "first_label:item.first_label,second_label:item.second_label,cache_label:item.cache_label};"
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
        pn.pane.Markdown("토큰: 빨강은 입력, 점무늬는 캐시 입력, 초록은 출력입니다."),
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
    token_groups = []
    for group in groups:
        points = []
        for answer_index, subject, artifact in group["runs"]:
            rows = artifact["rows"] if artifact else []
            calls = [call for row in rows for call in row["calls"]]
            errors = sum(row["status"] == "error" for row in rows)
            vector = judge_vector(calls, failed=bool(errors)) if artifact else None
            points.append(
                {
                    "answer": answer_index,
                    "attempt": f"A{answer_index} · {subject['round']}회차",
                    "vector": vector,
                    "support": f"HTTP 응답 {len(calls)}회 · 오류 {errors}건"
                    if artifact
                    else "미기록",
                }
            )
        token_groups.append({"label": group["label"], "points": points})
    options.append(
        ("토큰 사용량", stack_chart(token_groups, "tokens", "Judge 토큰 사용량", repeat=repeat))
    )
    return pn.Column(
        pn.pane.Markdown(
            "전체 채점 경과 시간은 실제 대기 시간입니다. 호출 시간 합은 병렬 요청의 "
            "시간이 겹칠 수 있습니다. 응답 전에 실패하거나 토큰이 일부 미기록인 합계는 "
            "미기록으로 표시합니다. 전체 채점량이 다를 수 있으므로 관측 문항과 호출 수를 "
            "함께 확인하세요. —은 캐시 미기록입니다. 사람 검수 시간은 포함하지 않습니다."
        ),
        pn.pane.Markdown(
            "막대는 answers 시행별 채점 회차를 먼저 평균하고, 시행을 같은 비중으로 평균합니다."
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
