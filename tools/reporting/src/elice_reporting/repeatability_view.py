"""Rubric boundaries with answers-run matrices in two-column grids."""

from html import escape

import panel as pn
from bokeh.models import ColumnDataSource, FixedTicker, HoverTool, Range1d
from bokeh.plotting import figure

from .presentation import metric_label
from .repeatability_sections import rater_display_names, rater_icons

PALETTE = ("#f1eee6", "#d9e7df", "#b4d3c9", "#83b8b1", "#4b9699", "#23727e", "#174b62")
BOUNDARY_NAMES = {
    "partial_or_better": "부분 충족 이상 · 1점 또는 2점",
    "sufficient": "충분 · 2점",
    "satisfied": "충족 · 1점",
}


def _rater_order(rater):
    family = {"chatgpt": 0, "claude": 1, "gemini": 2}
    return (
        0 if rater.startswith("human:") else 1,
        family.get(rater.removeprefix("model:"), 3),
        rater,
    )


def question_summaries(data, record_id):
    """Read one question's frozen analysis distributions without changing the report."""
    snapshots = {item["analysis_run_id"]: item for item in data.inputs["analyses"]}
    selected = []
    for summary in data.report["summaries"]:
        snapshot = snapshots[summary["analysis_run_id"]]
        distribution = next(
            (
                item
                for item in snapshot["analysis"]["distributions"]
                if item["record_id"] == record_id
                and item["rater"] == summary["rater"]
                and item["dimension"] == summary["dimension"]
            ),
            None,
        )
        failed = any(
            case["record_id"] == record_id and case["error"] for case in snapshot["bundle"]["cases"]
        )
        if distribution is None:
            selected.append(
                dict(
                    analysis_run_id=summary["analysis_run_id"],
                    rater=summary["rater"],
                    dimension=summary["dimension"],
                    score_counts={score: 0 for score in summary["score_counts"]},
                    status_counts={status: 0 for status in summary["status_counts"]},
                    boundaries={},
                    missing_records=1,
                    generation_failed_records=int(failed),
                )
            )
        else:
            selected.append(
                dict(
                    distribution,
                    analysis_run_id=summary["analysis_run_id"],
                    missing_records=0,
                    generation_failed_records=0,
                )
            )
    return selected


def _matrix_records(*, boundary, summaries, raters, dimensions, names):
    positions = {rater: len(raters) - index - 1 for index, rater in enumerate(raters)}
    dimension_positions = {dimension: index for index, dimension in enumerate(dimensions)}
    selected = {(item["rater"], item["dimension"]): item for item in summaries}
    records = []
    for rater in raters:
        for dimension in dimensions:
            item = selected.get((rater, dimension))
            result = item["boundaries"].get(boundary) if item else None
            rate = result["rate"] if result else None
            color = (
                PALETTE[min(int(rate * len(PALETTE)), len(PALETTE) - 1)]
                if rate is not None
                else "#e5eaf0"
            )
            records.append(
                dict(
                    x=dimension_positions[dimension],
                    y=positions[rater],
                    rater=names[rater],
                    dimension=metric_label(dimension),
                    label=f"{rate:.0%}" if rate is not None else "—",
                    color=color,
                    text_color="white" if rate is not None and rate >= 0.7 else "#18364a",
                    count=result["count"] if result else None,
                    valid=result["denominator"] if result else None,
                    scores=" · ".join(
                        f"{score}점 {count}" for score, count in item["score_counts"].items()
                    )
                    if item
                    else "미기록",
                    statuses=" · ".join(
                        f"{name} {count}" for name, count in item["status_counts"].items() if count
                    )
                    or "없음"
                    if item
                    else "미기록",
                    missing=item["missing_records"] if item else None,
                    failed=item["generation_failed_records"] if item else None,
                )
            )
    return {key: [item[key] for item in records] for key in records[0]}


def _matrix(*, run_index, source_data, raters, dimensions, icons):
    positions = {rater: len(raters) - index - 1 for index, rater in enumerate(raters)}
    dimension_positions = {dimension: index for index, dimension in enumerate(dimensions)}
    source = ColumnDataSource(source_data)
    icon_source = ColumnDataSource(
        dict(
            url=[icons[rater] for rater in raters],
            x=[-0.68] * len(raters),
            y=[positions[rater] for rater in raters],
        )
    )
    plot = figure(
        x_range=Range1d(-0.94, len(dimensions) - 0.48),
        y_range=Range1d(-0.55, len(raters) - 0.45),
        width=max(500, len(dimensions) * 108 + 115),
        height=max(270, len(raters) * 52 + 105),
        toolbar_location=None,
        tools="",
        title=f"A{run_index} · 답변 생성 {run_index}회차",
    )
    cells = plot.rect(
        x="x",
        y="y",
        width=0.94,
        height=0.88,
        fill_color="color",
        line_color="#ffffff",
        source=source,
    )
    plot.text(
        x="x",
        y="y",
        text="label",
        text_align="center",
        text_baseline="middle",
        text_color="text_color",
        text_font_size="12pt",
        text_font_style="bold",
        source=source,
    )
    plot.image_url(
        url="url",
        x="x",
        y="y",
        w=30,
        h=30,
        w_units="screen",
        h_units="screen",
        anchor="center",
        source=icon_source,
    )
    plot.add_tools(
        HoverTool(
            renderers=[cells],
            tooltips=[
                ("평가 주체", "@rater"),
                ("평가 항목", "@dimension"),
                ("충족 / 유효", "@count / @valid"),
                ("원점수", "@scores"),
                ("N/A·오류", "@statuses"),
                ("미기록 문항", "@missing"),
                ("답변 생성 실패", "@failed"),
            ],
        )
    )
    plot.xaxis.ticker = FixedTicker(ticks=list(range(len(dimensions))))
    plot.xaxis.major_label_overrides = {
        dimension_positions[dimension]: metric_label(dimension) for dimension in dimensions
    }
    plot.xaxis.major_label_orientation = 0.5
    plot.xaxis.major_label_text_font_size = "10pt"
    plot.yaxis.visible = False
    plot.grid.visible = False
    plot.outline_line_color = None
    plot.background_fill_color = "#fbfcfd"
    return plot, source


def summary_heatmaps(data):
    """Show both Gold boundaries; Iron has its single binary boundary."""
    summaries = data.report["summaries"]
    if not summaries:
        raise ValueError("Repeatability result has no summaries")
    analyses = data.report["analyses"]
    raters = sorted({item["rater"] for item in summaries}, key=_rater_order)
    dimensions = list(dict.fromkeys(item["dimension"] for item in summaries))
    boundaries = list(summaries[0]["boundaries"])
    names = rater_display_names(data)
    icons = rater_icons(data)
    records = data.inputs["analyses"][0]["bundle"]["records"]
    questions = {record["id"]: record["inputs"]["question"] for record in records}
    options = {"전체 질문": "all"}
    options.update(
        {
            f"{index}. {question[:70]}{'…' if len(question) > 70 else ''}": record_id
            for index, (record_id, question) in enumerate(questions.items(), 1)
        }
    )
    selector = pn.widgets.Select(
        label="질문 선택", options=options, value="all", sizing_mode="stretch_width"
    )
    question_panel = pn.pane.HTML(
        f'<div style="padding:12px 16px;border-radius:12px;background:#eef3f7;'
        f'color:#385165;font:14px system-ui">'
        f"전체 {len(records)}문항의 유효 판정을 합산합니다.</div>",
        sizing_mode="stretch_width",
    )
    descriptions = {
        "all": question_panel.object,
        **{
            record_id: (
                '<div style="padding:12px 16px;border-radius:12px;background:#eef3f7;'
                'color:#385165;font:14px system-ui">'
                f"<strong>질문 {index}</strong><br>{escape(question)}</div>"
            )
            for index, (record_id, question) in enumerate(questions.items(), 1)
        },
    }
    selections = {"all": summaries}
    selections.update({record_id: question_summaries(data, record_id) for record_id in questions})
    legend = pn.pane.HTML(
        '<div style="display:flex;flex-wrap:wrap;gap:12px 22px;align-items:center">'
        + "".join(
            '<div style="display:flex;align-items:center;gap:7px;white-space:nowrap">'
            f'<img src="{escape(icons[rater], quote=True)}" width="30" height="30" '
            f'alt="평가 주체"><span>{escape(names[rater])}</span></div>'
            for rater in raters
        )
        + "</div>",
        sizing_mode="stretch_width",
    )
    rows = [selector, question_panel, legend]
    sources = []
    payloads = {choice: [] for choice in selections}
    for boundary in boundaries:
        plots = []
        for index, run in enumerate(analyses):
            datasets = {
                choice: _matrix_records(
                    boundary=boundary,
                    summaries=[
                        item for item in chosen if item["analysis_run_id"] == run["analysis_run_id"]
                    ],
                    raters=raters,
                    dimensions=dimensions,
                    names=names,
                )
                for choice, chosen in selections.items()
            }
            plot, source = _matrix(
                run_index=index + 1,
                source_data=datasets["all"],
                raters=raters,
                dimensions=dimensions,
                icons=icons,
            )
            plots.append(plot)
            sources.append(source)
            for choice, dataset in datasets.items():
                payloads[choice].append(dataset)
        matrix_grid = pn.GridBox(
            *(
                pn.pane.Bokeh(plot, styles={"background": "white", "border-radius": "14px"})
                for plot in plots
            ),
            ncols=2,
            width=2 * max(plot.width for plot in plots) + 32,
            margin=0,
        )
        rows.extend(
            [
                pn.pane.HTML(
                    f'<strong style="color:#243e50;font:650 16px system-ui">'
                    f"{escape(BOUNDARY_NAMES[boundary])}</strong>"
                ),
                pn.Column(matrix_grid, styles={"overflow-x": "auto"}, sizing_mode="stretch_width"),
            ]
        )
    selector.jscallback(
        args={
            **{f"source_{index}": source for index, source in enumerate(sources)},
            "payloads": payloads,
            "question_panel": question_panel,
            "descriptions": descriptions,
        },
        value=(
            "const sources = [" + ",".join(f"source_{i}" for i in range(len(sources))) + "];\n"
            "const selected = cb_obj.value;\n"
            "sources.forEach((source, index) => {\n"
            "    source.data = payloads[selected][index];\n"
            "    source.change.emit();\n"
            "});\n"
            "const escaped = descriptions[selected].replace(/[&<>\"']/g, character => "
            "({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',\"'\":'&#39;'}[character]));\n"
            "question_panel.text = escaped;"
        ),
    )
    return pn.Column(*rows, sizing_mode="stretch_width")
