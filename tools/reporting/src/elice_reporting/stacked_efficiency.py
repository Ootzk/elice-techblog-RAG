"""Stacked views of measured token use and search tool calls."""

from collections import defaultdict
from numbers import Real
from statistics import mean

import panel as pn
from bokeh.models import ColumnDataSource, HoverTool, LabelSet
from bokeh.plotting import figure


def tool_counts(result):
    names = ("keyword_search", "meaning_search")
    steps, total = result.get("agent_steps"), result.get("tool_call_count")
    if (
        not isinstance(steps, list)
        or type(total) is not int
        or total < 0
        or any(
            not isinstance(step, dict) or not isinstance(step.get("action"), str) for step in steps
        )
    ):
        return None
    counts = {name: sum(step["action"] == name for step in steps) for name in names}
    return counts if sum(counts.values()) == total else None


def token_vector(usage, *, judge=False):
    if not isinstance(usage, dict):
        return None
    input_key, output_key = (
        ("input_tokens", "output_tokens") if judge else ("prompt_tokens", "completion_tokens")
    )
    cache_key = "cached_input_tokens" if judge else "cached_tokens"
    a, b, cached, total = (usage.get(k) for k in (input_key, output_key, cache_key, "total_tokens"))

    def valid(value):
        return isinstance(value, Real) and not isinstance(value, bool) and value >= 0

    if not valid(a) or not valid(b):
        return None
    if total is not None and (not valid(total) or total != a + b):
        return None
    return {"first": a, "second": b, "cached": cached if valid(cached) and cached <= a else None}


def _average(vectors):
    if not vectors:
        return None
    first = mean(item["first"] for item in vectors)
    second = mean(item["second"] for item in vectors)
    cache = (
        mean(item["cached"] for item in vectors)
        if all(item.get("cached") is not None for item in vectors)
        else None
    )
    return {"first": first, "second": second, "cached": cache}


def answer_vectors(snapshot, mode):
    values = {}
    for case in snapshot["bundle"]["cases"]:
        if case.get("error"):
            continue
        result = case.get("result") or {}
        if mode == "tokens":
            vector = token_vector(result.get("usage"))
        else:
            counts = tool_counts(result)
            vector = (
                {
                    "first": counts["keyword_search"],
                    "second": counts["meaning_search"],
                    "cached": None,
                }
                if counts is not None
                else None
            )
        if vector is not None:
            values[case["record_id"]] = vector
    return values


def answer_groups(snapshots, mode):
    points = []
    for index, snapshot in enumerate(snapshots, 1):
        values = answer_vectors(snapshot, mode)
        cases = snapshot["bundle"]["cases"]
        points.append(
            {
                "answer": index,
                "attempt": f"A{index}",
                "vector": _average(list(values.values())),
                "support": f"유효 {len(values)}/{len(cases)}문항",
            }
        )
    return [{"label": "답변 생성", "points": points}]


def paired_answer_groups(sides, mode):
    per_side = {}
    for side, snapshots in sides.items():
        buckets = defaultdict(list)
        for snapshot in snapshots:
            for record_id, vector in answer_vectors(snapshot, mode).items():
                buckets[record_id].append(vector)
        per_side[side] = {record_id: _average(vectors) for record_id, vectors in buckets.items()}
    paired = set(per_side["A"]) & set(per_side["B"])
    return [
        {
            "label": f"{side} · {'기준' if side == 'A' else '변경'}",
            "points": [
                {
                    "answer": 1,
                    "attempt": side,
                    "vector": _average([per_side[side][key] for key in sorted(paired)]),
                    "support": f"공통 {len(paired)}문항",
                }
            ],
        }
        for side in ("A", "B")
    ]


def judge_vector(calls, *, failed=False):
    if failed or not calls:
        return None
    vectors = [token_vector(call, judge=True) for call in calls]
    if any(value is None for value in vectors):
        return None
    return {
        "first": sum(value["first"] for value in vectors),
        "second": sum(value["second"] for value in vectors),
        "cached": sum(value["cached"] for value in vectors)
        if all(value["cached"] is not None for value in vectors)
        else None,
    }


def stack_chart(groups, mode, title, *, repeat=False):
    if not groups:
        return pn.pane.Markdown("표시할 실행 기록이 없습니다.")
    token = mode == "tokens"
    categories = [group["label"] for group in groups]
    rows = []
    for group in groups:
        valid = [point for point in group["points"] if point.get("vector") is not None]
        buckets = defaultdict(list)
        for point in valid:
            buckets[point["answer"]].append(point["vector"])
        vector = _average([_average(values) for values in buckets.values()])
        a, b = (vector["first"], vector["second"]) if vector else (None, None)
        cache = vector["cached"] if vector else None
        total = a + b if vector else None
        unit = "토큰" if token else "회"
        fmt = (lambda value: f"{value:,.0f}") if token else (lambda value: f"{value:,.1f}")
        cache_text = f"캐시 {fmt(cache)}" if cache is not None else "캐시 미기록"
        detail = (
            (f"입력 {fmt(a)} ({cache_text})  ·  출력 {fmt(b)}  ·  전체 {fmt(total)} {unit}")
            if token and vector
            else (
                f"전체 {fmt(total)}회  ·  키워드 {fmt(a)}  ·  의미 {fmt(b)}" if vector else "미기록"
            )
        )
        rows.append(
            {
                "category": group["label"],
                "first": a,
                "end": total,
                "cache_left": a - cache if cache is not None else None,
                "cache_right": a if cache is not None else None,
                "text": fmt(total) if vector else "미기록",
                "detail": detail,
                "position": total if total is not None else 0,
                "first_mid": a / 2 if a is not None else None,
                "second_mid": a + b / 2 if vector else None,
                "cache_mid": (a - cache / 2 if cache else a / 2) if vector and token else None,
                "first_label": fmt(a) if vector else "",
                "second_label": fmt(b) if vector else "",
                "cache_label": (fmt(cache) if cache is not None else "—")
                if vector and token
                else "",
                "support": f"{len(valid)}/{len(group['points'])}회 측정"
                if repeat
                else (valid[0]["support"] if valid else "미기록"),
            }
        )
    maximum = max([row["end"] for row in rows if row["end"] is not None] + [1])
    source = ColumnDataSource({key: [row[key] for row in rows] for key in rows[0]})
    plot = figure(
        y_range=list(reversed(categories)),
        x_range=(0, maximum * 1.8),
        height=max(230, 100 + len(categories) * 62),
        toolbar_location=None,
        tools="",
        sizing_mode="stretch_width",
        title=title,
    )
    first = plot.hbar(
        y="category",
        right="first",
        height=0.45,
        source=source,
        color="#d66c66" if token else "#668da4",
        legend_label="입력" if token else "키워드 검색",
    )
    second = plot.hbar(
        y="category",
        left="first",
        right="end",
        height=0.45,
        source=source,
        color="#5b8f89" if token else "#bc8a69",
        legend_label="출력" if token else "의미 검색",
    )
    if token:
        plot.hbar(
            y="category",
            left="cache_left",
            right="cache_right",
            height=0.45,
            source=source,
            fill_color="#d66c66",
            hatch_pattern="dot",
            hatch_color="white",
            hatch_weight=1.3,
            hatch_scale=7,
            line_color=None,
            legend_label="캐시 입력",
        )
    plot.add_layout(
        LabelSet(
            x="position",
            y="category",
            text="text",
            source=source,
            x_offset=8,
            y_offset=-5,
            text_font_size="9pt",
            text_color="black",
        )
    )
    for x, text, offset in (
        ("first_mid", "first_label", -9 if token else -5),
        ("cache_mid", "cache_label", 4),
        ("second_mid", "second_label", -5),
    ):
        plot.add_layout(
            LabelSet(
                x=x,
                y="category",
                text=text,
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
            renderers=[first, second],
            tooltips=[("대상", "@category"), ("값", "@detail"), ("관측", "@support")],
        )
    )
    plot.xaxis.axis_label = "토큰" if token else "회"
    plot.xgrid.grid_line_color = "#e8eef2"
    plot.ygrid.visible = False
    plot.outline_line_color = None
    plot.background_fill_color = "#fbfcfd"
    plot.legend.orientation = "horizontal"
    plot.legend.location = "top_right"
    plot.legend.background_fill_alpha = 0
    plot.legend.border_line_alpha = 0
    return pn.pane.Bokeh(plot, sizing_mode="stretch_width")
