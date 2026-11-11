"""Question-level rubric boundary heatmaps across answers runs."""

from html import escape

import panel as pn
from bokeh.models import ColumnDataSource, FixedTicker, HoverTool, Range1d, Span
from bokeh.plotting import figure

from .analysis_visuals import _icon
from .presentation import metric_label
from .repeatability_sections import rater_display_names, rater_icons

MODEL_MEAN = "aggregate:models"
PALETTE = ("#f1eee6", "#d9e7df", "#b4d3c9", "#83b8b1", "#4b9699", "#23727e", "#174b62")
CHANGE_PALETTE = ("#d7eee8", "#b7dcd2", "#91c6bc", "#e8d9a8", "#eca478", "#cb6b66")
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


def quality_level_records(data, questions, raters, dimensions, names, icons, boundary, choice):
    """Build heatmap cells from saved per-rater rates or the separate model mean."""
    levels = {
        (row["record_id"], row["rater"], row["dimension"], row["boundary"]): row
        for row in data.report["boundary_levels"]
    }
    levels.update(
        {
            (row["record_id"], MODEL_MEAN, row["dimension"], row["boundary"]): row
            for row in data.report["model_boundary_levels"]
        }
    )
    question_averages = {
        (row["rater"], row["dimension"], row["boundary"]): row
        for row in data.report["question_average_levels"]
    }
    dimension_averages = {
        (row["record_id"], row["rater"], row["boundary"]): row
        for row in data.report["dimension_average_levels"]
    }
    chosen = [record_id for record_id in questions if choice in ("all", record_id)]
    row_count = len(chosen) * len(raters) + (len(raters) if choice == "all" else 0)
    cells, labels = [], []
    for question_index, record_id in enumerate(questions, 1):
        if record_id not in chosen:
            continue
        for rater_index, rater in enumerate(raters):
            y = row_count - (chosen.index(record_id) * len(raters) + rater_index) - 1
            labels.append(
                dict(
                    x=-0.74, question_x=-1.38, y=y, icon=icons[rater], question=f"Q{question_index}"
                )
            )
            for x, dimension in enumerate((*dimensions, "overall")):
                item = (
                    dimension_averages.get((record_id, rater, boundary))
                    if dimension == "overall"
                    else levels.get((record_id, rater, dimension, boundary))
                )
                rate = item["mean_rate"] if item else None
                cells.append(
                    dict(
                        x=x,
                        y=y,
                        question=f"Q{question_index}: {questions[record_id]}",
                        rater=names[rater],
                        dimension=(
                            "전체 항목 평균" if dimension == "overall" else metric_label(dimension)
                        ),
                        label=f"{rate:.0%}" if rate is not None else "—",
                        color=(
                            PALETTE[min(int(rate * len(PALETTE)), len(PALETTE) - 1)]
                            if rate is not None
                            else "#e5eaf0"
                        ),
                        text_color="white" if rate is not None and rate >= 0.7 else "#18364a",
                        scope=(
                            f"유효 항목 {item['observed_dimensions']}/"
                            f"{item['possible_dimensions']}개"
                            if item and dimension == "overall"
                            else f"모델 {item['observed_models']}/{item['possible_models']}개 · "
                            "모델×시행 "
                            f"{item['observed_model_runs']}/{item['possible_model_runs']}건"
                            if item and rater == MODEL_MEAN
                            else f"answers 시행 {item['observed_runs']}/{item['possible_runs']}회"
                            if item
                            else "유효 관측 없음"
                        ),
                        valid=item["valid_gradings"] if item else 0,
                    )
                )
    if choice == "all":
        for rater_index, rater in enumerate(raters):
            y = len(raters) - rater_index - 1
            labels.append(dict(x=-0.74, question_x=-1.38, y=y, icon=icons[rater], question="평균"))
            for x, dimension in enumerate((*dimensions, "overall")):
                item = (
                    dimension_averages.get((None, rater, boundary))
                    if dimension == "overall"
                    else question_averages.get((rater, dimension, boundary))
                )
                rate = item["mean_rate"] if item else None
                cells.append(
                    dict(
                        x=x,
                        y=y,
                        question="전체 문항 평균",
                        rater=names[rater],
                        dimension=(
                            "전체 항목 평균" if dimension == "overall" else metric_label(dimension)
                        ),
                        label=f"{rate:.0%}" if rate is not None else "—",
                        color=(
                            PALETTE[min(int(rate * len(PALETTE)), len(PALETTE) - 1)]
                            if rate is not None
                            else "#e5eaf0"
                        ),
                        text_color="white" if rate is not None and rate >= 0.7 else "#18364a",
                        scope=(
                            f"유효 항목 {item['observed_dimensions']}/"
                            f"{item['possible_dimensions']}개"
                            if item and dimension == "overall"
                            else f"유효 문항 {item['observed_questions']}/"
                            f"{item['possible_questions']}개"
                            if item
                            else "유효 문항 없음"
                        ),
                        valid=item["valid_gradings"] if item else 0,
                    )
                )
    return (
        {key: [item[key] for item in cells] for key in cells[0]},
        {key: [item[key] for item in labels] for key in labels[0]},
        row_count,
    )


def _quality_matrix(*, boundary, cell_data, label_data, row_count, dimensions):
    source, label_source = ColumnDataSource(cell_data), ColumnDataSource(label_data)
    plot = figure(
        x_range=Range1d(-1.65, len(dimensions) + 0.52),
        y_range=Range1d(-0.55, row_count - 0.45),
        width=max(500, (len(dimensions) + 1) * 90 + 110),
        height=max(270, row_count * 42 + 100),
        toolbar_location=None,
        tools="",
        title=BOUNDARY_NAMES[boundary],
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
        text_font_size="11pt",
        text_font_style="bold",
        source=source,
    )
    plot.image_url(
        url="icon",
        x="x",
        y="y",
        w=28,
        h=28,
        w_units="screen",
        h_units="screen",
        anchor="center",
        source=label_source,
    )
    plot.text(
        x="question_x",
        y="y",
        text="question",
        text_align="center",
        text_baseline="middle",
        text_color="#526b68",
        text_font_size="10pt",
        source=label_source,
    )
    plot.add_layout(
        Span(
            location=len(dimensions) - 0.5,
            dimension="height",
            line_color="#64748b",
            line_dash="dashed",
            line_width=2,
        )
    )
    mean_separator = Span(
        location=0.5,
        dimension="width",
        line_color="#64748b",
        line_dash="dashed",
        line_width=2,
    )
    plot.add_layout(mean_separator)
    plot.add_tools(
        HoverTool(
            renderers=[cells],
            tooltips=[
                ("질문", "@question"),
                ("평가 주체", "@rater"),
                ("평가 항목", "@dimension"),
                ("시행 평균 충족률", "@label"),
                ("관측 범위", "@scope"),
                ("유효 채점", "@valid"),
            ],
        )
    )
    plot.xaxis.ticker = FixedTicker(ticks=list(range(len(dimensions) + 1)))
    plot.xaxis.major_label_overrides = {
        **{index: metric_label(dimension) for index, dimension in enumerate(dimensions)},
        len(dimensions): "전체 항목 평균",
    }
    plot.xaxis.major_label_orientation = 0.5
    plot.xaxis.major_label_text_font_size = "10pt"
    plot.yaxis.visible = False
    plot.grid.visible = False
    plot.outline_line_color = None
    plot.background_fill_color = "#fbfcfd"
    return plot, source, label_source, mean_separator


def quality_heatmaps(data):
    """Show question-level quality once, with one heatmap per rubric boundary."""
    summaries = data.report["summaries"]
    if not summaries:
        raise ValueError("Repeatability result has no summaries")
    raters = sorted({item["rater"] for item in summaries}, key=_rater_order)
    observed_dimensions = {item["dimension"] for item in summaries}
    preferred = ("correctness", "completeness", "groundedness", "relevance")
    dimensions = [dimension for dimension in preferred if dimension in observed_dimensions]
    dimensions.extend(sorted(observed_dimensions - set(preferred)))
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
        f"전체 {len(records)}문항을 각각 표시합니다.</div>",
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
    selections = ("all", *questions)
    names[MODEL_MEAN] = "모델 평균"
    icons[MODEL_MEAN] = _icon(MODEL_MEAN, "#64748b", None, ["모델 평균"])
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
    rater_options = {"전체 모델 평균": MODEL_MEAN}
    rater_options.update({names[rater]: rater for rater in raters})
    rater_selector = pn.widgets.Select(
        label="평가 주체 선택",
        options=rater_options,
        value=MODEL_MEAN,
        sizing_mode="stretch_width",
    )
    quality_sources, quality_label_sources, quality_plots, mean_separators = [], [], [], []
    rater_choices = tuple(rater_options.values())
    quality_payloads = {choice: {rater: [] for rater in rater_choices} for choice in selections}
    quality_label_payloads = {
        choice: {rater: [] for rater in rater_choices} for choice in selections
    }
    quality_row_counts = {
        choice: {rater: len(questions) + 1 if choice == "all" else 1 for rater in rater_choices}
        for choice in selections
    }
    for boundary in boundaries:
        datasets = {
            choice: {
                rater: quality_level_records(
                    data,
                    questions,
                    [rater],
                    dimensions,
                    names,
                    icons,
                    boundary,
                    choice,
                )
                for rater in rater_choices
            }
            for choice in selections
        }
        cell_data, label_data, row_count = datasets["all"][MODEL_MEAN]
        plot, cell_source, label_source, mean_separator = _quality_matrix(
            boundary=boundary,
            cell_data=cell_data,
            label_data=label_data,
            row_count=row_count,
            dimensions=dimensions,
        )
        quality_plots.append(plot)
        quality_sources.append(cell_source)
        quality_label_sources.append(label_source)
        mean_separators.append(mean_separator)
        for choice, by_rater in datasets.items():
            for rater, (cells, labels, _) in by_rater.items():
                quality_payloads[choice][rater].append(cells)
                quality_label_payloads[choice][rater].append(labels)
    quality_grid = pn.GridBox(
        *(
            pn.pane.Bokeh(plot, styles={"background": "white", "border-radius": "14px"})
            for plot in quality_plots
        ),
        ncols=2,
        width=2 * max(plot.width for plot in quality_plots) + 32,
        margin=0,
    )
    view = [
        pn.Row(selector, rater_selector, sizing_mode="stretch_width"),
        question_panel,
        legend,
        pn.pane.Markdown("### 문항별 품질 수준"),
        pn.pane.Markdown(
            "평가 주체·항목별로 각 answers 시행의 경계 충족률을 같은 비중으로 평균합니다. "
            "유효 answers 시행 수는 독립 답변 생성 횟수이며, 유효 채점 수와 구분합니다. "
            "전체 모델 평균은 모델별 충족률을 같은 비중으로 평균합니다. "
            "사람 판정은 포함하지 않습니다. "
            "세로 점선 오른쪽은 유효 평가 항목의 평균, 가로 점선 아래는 전체 문항 평균입니다. "
            "오른쪽 열은 네 항목 동시 충족률이 아닙니다. "
            "개별 평가 주체는 드롭다운에서 확인할 수 있습니다."
        ),
        pn.Column(quality_grid, styles={"overflow-x": "auto"}, sizing_mode="stretch_width"),
    ]
    callback_args = {
        "question_selector": selector,
        "rater_selector": rater_selector,
        "question_panel": question_panel,
        "descriptions": descriptions,
        **{f"quality_source_{index}": source for index, source in enumerate(quality_sources)},
        **{
            f"quality_label_source_{index}": source
            for index, source in enumerate(quality_label_sources)
        },
        **{f"quality_plot_{index}": plot for index, plot in enumerate(quality_plots)},
        **{f"mean_separator_{index}": span for index, span in enumerate(mean_separators)},
        "quality_payloads": quality_payloads,
        "quality_label_payloads": quality_label_payloads,
        "quality_row_counts": quality_row_counts,
    }
    callback = (
        "const qualitySources = ["
        + ",".join(f"quality_source_{i}" for i in range(len(quality_sources)))
        + "];\n"
        "const qualityLabelSources = ["
        + ",".join(f"quality_label_source_{i}" for i in range(len(quality_label_sources)))
        + "];\n"
        "const qualityPlots = ["
        + ",".join(f"quality_plot_{i}" for i in range(len(quality_plots)))
        + "];\n"
        "const meanSeparators = ["
        + ",".join(f"mean_separator_{i}" for i in range(len(mean_separators)))
        + "];\n"
        "const question = question_selector.value;\n"
        "const rater = rater_selector.value;\n"
        "const escaped = descriptions[question].replace(/[&<>\"']/g, character => "
        "({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',\"'\":'&#39;'}[character]));\n"
        "question_panel.text = escaped;\n"
        "qualitySources.forEach((source, index) => {\n"
        "    source.data = quality_payloads[question][rater][index];\n"
        "    source.change.emit();\n"
        "    qualityLabelSources[index].data = "
        "quality_label_payloads[question][rater][index];\n"
        "    qualityLabelSources[index].change.emit();\n"
        "    const rows = quality_row_counts[question][rater];\n"
        "    qualityPlots[index].height = Math.max(270, rows * 42 + 100);\n"
        "    qualityPlots[index].y_range.end = rows - 0.45;\n"
        "    meanSeparators[index].visible = question === 'all';\n"
        "});"
    )
    selector.jscallback(args=callback_args, value=callback)
    rater_selector.jscallback(args=callback_args, value=callback)
    return pn.Column(*view, sizing_mode="stretch_width")


def _change_records(data, rater, dimensions, boundaries, names):
    changes = data.report["boundary_changes"]
    if rater == MODEL_MEAN:
        selected = data.report["model_boundary_changes"]
        dimensions_by_key = {
            (item["dimension"], item["boundary"]): item for item in selected["by_dimension"]
        }
        overall_by_key = {item["boundary"]: item for item in selected["overall"]}
    else:
        dimensions_by_key = {
            (item["dimension"], item["boundary"]): item
            for item in changes["by_dimension"]
            if item["rater"] == rater
        }
        overall_by_key = {
            item["boundary"]: item for item in changes["by_rater"] if item["rater"] == rater
        }
    records = []
    for boundary_index, boundary in enumerate(boundaries):
        for x, dimension in enumerate((*dimensions, "overall")):
            item = (
                overall_by_key.get(boundary)
                if dimension == "overall"
                else dimensions_by_key.get((dimension, boundary))
            )
            rate = item["mean_absolute_change"] if item else None
            records.append(
                dict(
                    x=x,
                    y=len(boundaries) - boundary_index - 1,
                    boundary=BOUNDARY_NAMES[boundary],
                    dimension="전체 항목" if dimension == "overall" else metric_label(dimension),
                    rater=names[rater],
                    label=f"{rate * 100:.1f}%p" if rate is not None else "—",
                    color=(
                        CHANGE_PALETTE[
                            min(int(rate * len(CHANGE_PALETTE)), len(CHANGE_PALETTE) - 1)
                        ]
                        if rate is not None
                        else "#e5eaf0"
                    ),
                    text_color="white" if rate is not None and rate >= 0.8 else "#18364a",
                    pairs=(f"{item['observed_pairs']}/{item['possible_pairs']}" if item else "0/0"),
                    models=(
                        f"{item['observed_models']}/{item['possible_models']}"
                        if item and "observed_models" in item
                        else "—"
                    ),
                )
            )
    return {key: [item[key] for item in records] for key in records[0]}


def change_heatmap(data):
    """Show saved Wasserstein changes as one boundary-by-dimension matrix."""
    changes = data.report["boundary_changes"]
    boundaries = [item["boundary"] for item in changes["overall"]]
    raters = sorted({item["rater"] for item in changes["by_rater"]}, key=_rater_order)
    observed_dimensions = {item["dimension"] for item in changes["by_dimension"]}
    preferred = ("correctness", "completeness", "groundedness", "relevance")
    dimensions = [dimension for dimension in preferred if dimension in observed_dimensions]
    dimensions.extend(sorted(observed_dimensions - set(preferred)))
    names = rater_display_names(data)
    names[MODEL_MEAN] = "모델 평균"
    options = {"전체 모델 평균": MODEL_MEAN}
    options.update({names[rater]: rater for rater in raters})
    selector = pn.widgets.Select(
        label="평가 주체 선택", options=options, value=MODEL_MEAN, sizing_mode="stretch_width"
    )
    datasets = {
        rater: _change_records(data, rater, dimensions, boundaries, names)
        for rater in options.values()
    }
    source = ColumnDataSource(datasets[MODEL_MEAN])
    plot = figure(
        x_range=Range1d(-0.6, len(dimensions) + 0.55),
        y_range=Range1d(-0.55, len(boundaries) - 0.45),
        width=max(630, (len(dimensions) + 1) * 125),
        height=max(265, len(boundaries) * 80 + 120),
        toolbar_location=None,
        tools="",
        title="시행 간 평균 변화폭 · 1차 Wasserstein 거리",
    )
    cells = plot.rect(
        x="x",
        y="y",
        width=0.94,
        height=0.82,
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
        text_font_size="11pt",
        text_font_style="bold",
        source=source,
    )
    plot.add_layout(
        Span(
            location=len(dimensions) - 0.5,
            dimension="height",
            line_color="#64748b",
            line_dash="dashed",
            line_width=2,
        )
    )
    plot.add_tools(
        HoverTool(
            renderers=[cells],
            tooltips=[
                ("평가 주체", "@rater"),
                ("경계", "@boundary"),
                ("평가 항목", "@dimension"),
                ("평균 변화폭", "@label"),
                ("비교 가능/전체 쌍", "@pairs"),
                ("참여 모델", "@models"),
            ],
        )
    )
    plot.xaxis.ticker = FixedTicker(ticks=list(range(len(dimensions) + 1)))
    plot.xaxis.major_label_overrides = {
        **{index: metric_label(dimension) for index, dimension in enumerate(dimensions)},
        len(dimensions): "전체 항목",
    }
    plot.xaxis.major_label_orientation = 0.45
    plot.yaxis.ticker = FixedTicker(ticks=list(range(len(boundaries))))
    plot.yaxis.major_label_overrides = {
        len(boundaries) - index - 1: BOUNDARY_NAMES[boundary]
        for index, boundary in enumerate(boundaries)
    }
    plot.grid.visible = False
    plot.outline_line_color = None
    plot.background_fill_color = "#fbfcfd"
    selector.jscallback(
        args={"source": source, "datasets": datasets},
        value=("source.data = datasets[cb_obj.value]; source.change.emit();"),
    )
    return pn.Column(
        selector,
        pn.pane.Markdown(
            "같은 질문의 answers 시행 쌍에서 구한 경계 충족률의 1차 Wasserstein 거리를 "
            "질문·항목별로 평균했습니다. 낮을수록 시행 간 변화가 작습니다. "
            "전체 모델 평균은 모델별 변화폭을 같은 비중으로 평균하며 사람 평가는 제외합니다."
            " 세로 점선 오른쪽은 네 평가 항목의 평균 변화폭입니다."
        ),
        pn.Column(pn.pane.Bokeh(plot), styles={"overflow-x": "auto"}, sizing_mode="stretch_width"),
        sizing_mode="stretch_width",
    )
