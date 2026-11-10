import base64
from copy import deepcopy

import holoviews as hv
import pandas as pd
import panel as pn
from bokeh.models import ColorBar, HoverTool, ImageURL

from elice_reporting import demo
from elice_reporting.analysis_visuals import (
    DIMENSIONS,
    KAPPA_MISSING,
    KAPPA_PALETTE,
    MATRIX_TITLE_HEIGHT,
    STATES,
    analysis_subjects,
    grading_cells,
    grading_heatmap,
    kappa_cells,
    kappa_color,
    kappa_colorbar,
    kappa_heatmaps,
    matrix_group_size,
    matrix_size,
    rater_legend,
    subject_key,
)


def test_legend_keeps_rounds_profiles_and_colored_people_distinct():
    data = demo("analysis")
    repeated = deepcopy(data.inputs["gradings"][0])
    repeated["round"] = 2
    other = deepcopy(repeated)
    other["rater_id"] = "human:<another>"
    data.inputs["gradings"].extend([repeated, other])
    subjects = analysis_subjects(data)
    assert len(subjects) == 5
    assert subjects[0]["color"] == subjects[3]["color"] != subjects[4]["color"]
    assert subjects[0]["icon"] != subjects[3]["icon"]
    assert len({s["key"] for s in subjects}) == len(subjects)
    legend = rater_legend(subjects).object
    assert "검수자 2" in legend and "human:" not in legend
    assert "<table" in legend and "<th>회차</th>" in legend
    assert "모델 유형" not in legend and "프로필" not in legend
    for subject in subjects:
        svg = base64.b64decode(subject["icon"].split(",", 1)[1]).decode()
        assert f">{subject['round']}</text>" in svg
    claude = next(s for s in subjects if s["rater"] == "model:claude")
    svg = base64.b64decode(claude["icon"].split(",", 1)[1]).decode()
    assert "Claude" in svg and "#D97757" in svg


def test_kappa_matrix_uses_saved_pairs_and_never_invents_diagonal_or_zero():
    data = demo("analysis")
    subjects = analysis_subjects(data)
    row = next(r for r in data.report["comparisons"] if r["dimension"] == "correctness")
    cells = kappa_cells(data, subjects, "correctness")
    assert len(cells) == len(subjects) * (len(subjects) - 1) // 2
    assert (cells.x + cells.y > len(subjects) - 1).all()
    a = next(i for i, s in enumerate(subjects) if s["key"] == subject_key(row["left"]))
    b = next(i for i, s in enumerate(subjects) if s["key"] == subject_key(row["right"]))
    low, high = sorted((a, b))
    match = (cells.x == high) & (cells.y == len(subjects) - 1 - low)
    assert cells[match].iloc[0]["value"] == row["cohen_kappa"]
    row["cohen_kappa"] = None
    row["kappa_undefined_reason"] = "constant labels"
    updated = kappa_cells(data, subjects, "correctness")
    missing = updated[(updated.x == high) & (updated.y == len(subjects) - 1 - low)].iloc[0]
    assert pd.isna(missing["value"]) and missing["reason"] == "constant labels"


def test_item_heatmap_has_four_rows_and_preserves_all_label_states():
    data = demo("analysis")
    subjects = analysis_subjects(data)
    human = data.inputs["gradings"][0]
    for row, (status, score) in zip(
        human["ratings"][:4],
        [("scored", 2), ("scored", 0), ("not_applicable", None), ("error", None)],
        strict=True,
    ):
        row.update(status=status, score=score)
    cells = grading_cells(data, subjects, "0")
    assert len(cells) == len(subjects) * 4
    assert tuple(cells[cells.x == 0]["dimension"]) == DIMENSIONS
    assert set(cells.x) == set(range(len(subjects)))
    assert set(cells.y) == {0, 1, 2, 3}
    assert set(cells[cells.x == 0]["status"]) == {"scored", "not_applicable", "error"}
    assert set(grading_cells(data, subjects, "missing-id")["status"]) == {"missing"}
    assert len({color for color, _ in STATES.values()}) == 6


def test_bokeh_heatmap_has_embedded_icons_and_real_hover_fields():
    pn.extension()
    hv.extension("bokeh")
    data = demo("analysis")
    subjects = analysis_subjects(data)
    figure = hv.render(grading_heatmap(data, subjects, "0"))
    source = figure.renderers[0].data_source.data
    assert {"label", "score", "rater", "dimension", "rationale"} <= source.keys()
    assert len(source["label"]) == len(subjects) * 4
    icons = [r for r in figure.renderers if isinstance(r.glyph, ImageURL)]
    assert len(icons) == 1
    assert all(
        url.startswith("data:image/svg+xml;base64,") for url in icons[0].data_source.data["url"]
    )


def test_kappa_scale_and_cells_share_exact_diverging_colormap():
    hv.extension("bokeh")
    assert kappa_color(-1) == KAPPA_PALETTE[0] == "#67001f"
    assert kappa_color(0) == KAPPA_PALETTE[len(KAPPA_PALETTE) // 2]
    assert kappa_color(1) == KAPPA_PALETTE[-1] == "#053061"
    assert kappa_color(None) == KAPPA_MISSING
    assert kappa_color(float("nan")) == KAPPA_MISSING
    legend = hv.render(kappa_colorbar())
    bar = next(model for model in legend.right if isinstance(model, ColorBar))
    assert tuple(bar.color_mapper.palette) == KAPPA_PALETTE
    assert (bar.color_mapper.low, bar.color_mapper.high) == (-1, 1)
    assert bar.orientation == "vertical"
    data = demo("analysis")
    for row in data.report["comparisons"]:
        row["cohen_kappa"] = 1
    cells = kappa_cells(data, analysis_subjects(data), "correctness")
    assert set(cells[cells.value == 1]["fill"]) == {KAPPA_PALETTE[-1]}


def test_heatmaps_are_fixed_hover_only_and_item_uses_one_matrix_size():
    hv.extension("bokeh")
    data = demo("analysis")
    subjects = analysis_subjects(data)
    item = hv.render(grading_heatmap(data, subjects, "0"))
    assert (item.width, item.height) == (
        matrix_size(subjects),
        matrix_size(subjects) + MATRIX_TITLE_HEIGHT,
    )
    assert item.tools and all(isinstance(tool, HoverTool) for tool in item.tools)
    assert item.toolbar.active_drag is None and item.toolbar.active_scroll is None
    assert {label for tool in item.tools for label, _ in tool.tooltips} == {
        "판정",
        "원점수",
        "평가자",
        "회차",
        "항목",
    }
    panel = kappa_heatmaps(data, subjects)
    grid, legend_pane = panel.objects[0].objects
    assert grid.ncols == 2 and len(grid.objects) == 4
    assert legend_pane.object.opts.get().kwargs["height"] == matrix_group_size(subjects)[1]
    for column in grid.objects:
        figure = hv.render(column.objects[0].object)
        assert (figure.width, figure.height) == (
            matrix_size(subjects),
            matrix_size(subjects) + MATRIX_TITLE_HEIGHT,
        )
        assert figure.title.text and "correctness" not in figure.title.text
        assert all(isinstance(tool, HoverTool) for tool in figure.tools)
        assert {label for tool in figure.tools for label, _ in tool.tooltips} == {
            "선형 가중 Cohen κ",
            "유효",
            "제외",
            "사유",
        }


def test_ordinal_scores_are_shared_by_humans_and_models_independent_of_diagnostics():
    data = demo("analysis")
    model = data.inputs["gradings"][1]
    model["measurements"] = []
    cells = grading_cells(data, analysis_subjects(data), "0")
    assert all(v in {0, 1, 2} for v in cells["score"])
    assert all("수치 점수 없음" not in v for v in cells["score_display"])


def test_rounds_keep_individual_links_icons_labels_and_repeat_views():
    from elice_reporting.analysis_visuals import agreement_browser

    data = demo("analysis")
    original = data.inputs["gradings"][1]
    repeated = deepcopy(original)
    repeated["round"] = 2
    data.inputs["gradings"].append(repeated)
    for round_, run in [("1", "grade-one"), ("2", "grade-two")]:
        data.links.setdefault("gradings", []).append(
            dict(
                rater=original["rater_id"],
                round=round_,
                profile_digest=original["rater_profile_digest"],
                run_id=run,
                url="https://mlflow.test/" + run,
                model_observations=[dict(trace_id=run, models=["gpt-6-luna"])],
            )
        )
    for dimension in DIMENSIONS:
        data.report["comparisons"].append(
            dict(
                dimension=dimension,
                left=dict(
                    rater=original["rater_id"],
                    round="1",
                    profile_digest=original["rater_profile_digest"],
                ),
                right=dict(
                    rater=original["rater_id"],
                    round="2",
                    profile_digest=original["rater_profile_digest"],
                ),
                comparison="repeat_consistency",
                cohen_kappa=0.25,
                kappa_undefined_reason=None,
                agreement=0.75,
                valid_pairs=4,
                excluded_pairs=0,
            )
        )
    subjects = analysis_subjects(data)
    rounds = [s for s in subjects if s["rater"] == original["rater_id"]]
    assert len(rounds) == 2 and rounds[0]["short_name"] == rounds[1]["short_name"]
    assert rounds[0]["icon"] != rounds[1]["icon"]
    legend = rater_legend(subjects).object
    assert "https://mlflow.test/grade-one" in legend
    assert "https://mlflow.test/grade-two" in legend
    assert legend.count("ChatGPT 6 Luna") == 2
    view = agreement_browser(data, subjects)
    assert isinstance(view.objects[0], pn.widgets.Select)
    assert len(view.objects[0].options) == 2
    assert len(grading_cells(data, subjects, "0")) == len(subjects) * 4
    # Profile changes are separate protocols, not repeated measurements of the same one.
    repeated["rater_profile_digest"] = "changed-protocol"
    for pair in data.report["comparisons"]:
        if pair["right"]["rater"] == original["rater_id"] and pair["right"]["round"] == "2":
            pair["right"]["profile_digest"] = "changed-protocol"
    revised = agreement_browser(data, analysis_subjects(data))
    assert not revised.select(pn.widgets.Select)


def test_luna_and_terra_have_readable_names_and_distinct_icons_without_type_codes():
    data = demo("analysis")
    original = deepcopy(data.inputs["gradings"][1])
    data.inputs["gradings"] = []
    data.inputs["profiles"] = {}
    data.report["comparisons"] = []
    for model, round_ in [("gpt-6-luna", 1), ("gpt-6-terra", 1), ("gpt-6-luna", 2)]:
        grading = deepcopy(original)
        grading.update(rater_id="model:chatgpt", round=round_, rater_profile_digest=model)
        data.inputs["gradings"].append(grading)
        data.inputs["profiles"][model] = {"scorers": [{"model": model}]}
    luna, terra, luna_repeat = analysis_subjects(data)
    assert luna["short_name"] == "Luna" and terra["short_name"] == "Terra"
    assert not luna["setting"] and not terra["setting"]
    assert luna["short_name"] == luna_repeat["short_name"]
    assert len({s["icon"] for s in [luna, terra, luna_repeat]}) == 3
    assert "ChatGPT 6 Luna" in luna["label"] and "ChatGPT 6 Terra" in terra["label"]
    for subject in [luna, terra, luna_repeat]:
        svg = base64.b64decode(subject["icon"].split(",", 1)[1]).decode()
        assert "type_code" not in subject
        assert subject["profile"] not in subject["label"]
        assert f">{subject['round']}</text>" in svg
