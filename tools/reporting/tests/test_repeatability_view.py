import panel as pn
from bokeh.models import Span
from bokeh.models.glyphs import ImageURL

from elice_reporting.data import ReportData, demo
from elice_reporting.repeatability_sections import (
    grading_breakdown,
    repeatability_metric_guide,
)
from elice_reporting.repeatability_view import (
    MODEL_MEAN,
    _change_records,
    change_heatmap,
    quality_heatmaps,
    quality_level_records,
)


def test_gold_report_shows_only_question_level_boundary_heatmaps_with_icons():
    agreements = [{"agreement_run_id": f"agreement-{i}"} for i in range(3)]
    summaries = []
    snapshots = []
    for run in agreements:
        distributions = []
        for rater in ("model:chatgpt", "model:claude"):
            for dimension in ("correctness", "completeness"):
                summary = {
                    "agreement_run_id": run["agreement_run_id"],
                    "rater": rater,
                    "dimension": dimension,
                    "score_counts": {"0": 0, "1": 1, "2": 2},
                    "status_counts": {"not_applicable": 0, "error": 0, "pending": 0},
                    "missing_records": 0,
                    "generation_failed_records": 0,
                    "boundaries": {
                        "partial_or_better": {"count": 3, "denominator": 3, "rate": 1.0},
                        "sufficient": {"count": 2, "denominator": 3, "rate": 2 / 3},
                    },
                }
                summaries.append(summary)
                distributions.append(dict(summary, record_id="q1", attempts=3, valid=3))
        snapshots.append(
            {
                "agreement_run_id": run["agreement_run_id"],
                "agreement": {"distributions": distributions},
                "bundle": {
                    "records": [
                        {"id": "q1", "inputs": {"question": "첫 번째 질문"}},
                        {"id": "q2", "inputs": {"question": "두 번째 질문"}},
                    ],
                    "cases": [
                        {"record_id": "q1", "error": None},
                        {"record_id": "q2", "error": "generation failed"},
                    ],
                },
            }
        )
    data = ReportData(
        kind="repeatability",
        report={
            "kind": "gold",
            "summaries": summaries,
            "agreements": agreements,
            "boundary_levels": [
                {
                    "record_id": "q1",
                    "rater": "model:chatgpt",
                    "dimension": "correctness",
                    "boundary": "sufficient",
                    "mean_rate": 2 / 3,
                    "observed_runs": 3,
                    "possible_runs": 3,
                    "valid_gradings": 9,
                }
            ],
            "model_boundary_levels": [
                {
                    "record_id": "q1",
                    "dimension": "correctness",
                    "boundary": "sufficient",
                    "mean_rate": 2 / 3,
                    "observed_models": 1,
                    "possible_models": 2,
                    "observed_model_runs": 3,
                    "possible_model_runs": 6,
                    "valid_gradings": 9,
                }
            ],
            "question_average_levels": [
                {
                    "rater": MODEL_MEAN,
                    "dimension": "correctness",
                    "boundary": "sufficient",
                    "mean_rate": 0.7,
                    "observed_questions": 1,
                    "possible_questions": 2,
                    "valid_gradings": 9,
                }
            ],
            "dimension_average_levels": [
                {
                    "record_id": record_id,
                    "rater": MODEL_MEAN,
                    "boundary": "sufficient",
                    "mean_rate": rate,
                    "observed_dimensions": 1,
                    "possible_dimensions": 2,
                    "valid_gradings": 9,
                }
                for record_id, rate in (("q1", 2 / 3), (None, 0.7))
            ],
        },
        inputs={"agreements": snapshots},
        source="local",
    )
    view = quality_heatmaps(data)
    assert len(view.objects) == 6  # selector, question, legend, heading, method, heatmaps
    selector, rater_selector = view.objects[0].objects
    assert isinstance(selector, pn.widgets.Select)
    assert list(selector.options.values()) == ["all", "q1", "q2"]
    assert isinstance(rater_selector, pn.widgets.Select)
    assert list(rater_selector.options.values()) == [
        MODEL_MEAN,
        "model:chatgpt",
        "model:claude",
    ]
    quality_grid = view.objects[5].objects[0]
    assert isinstance(quality_grid, pn.GridBox)
    assert quality_grid.ncols == 2
    assert len(quality_grid.objects) == 2
    assert quality_grid.objects[1].object.title.text == "충분 · 2점"
    labels = [
        renderer.data_source.data["label"]
        for renderer in quality_grid.objects[1].object.renderers
        if "label" in renderer.data_source.data
    ]
    assert ["67%", "—", "67%", "—", "—", "—", "70%", "—", "70%"] in labels
    assert quality_grid.objects[1].object.xaxis[0].major_label_overrides[2] == "전체 항목 평균"
    for matrix in quality_grid.objects:
        plot = matrix.object
        separators = [item for item in plot.center if isinstance(item, Span)]
        assert {(item.dimension, item.location) for item in separators} == {
            ("height", 1.5),
            ("width", 0.5),
        }
        assert all(item.line_dash for item in separators)
        icons = [
            renderer.glyph
            for renderer in plot.renderers
            if isinstance(getattr(renderer, "glyph", None), ImageURL)
        ]
        assert len(icons) == 1
        assert icons[0].w_units == icons[0].h_units == "screen"
        assert icons[0].w == icons[0].h == 28
        assert "A1" not in plot.title.text


def test_quality_level_heatmap_keeps_question_and_rater_rows_distinct():
    data = ReportData(
        kind="repeatability",
        report={
            "kind": "gold",
            "agreements": [{}, {}, {}],
            "model_boundary_levels": [],
            "question_average_levels": [],
            "dimension_average_levels": [],
            "boundary_levels": [
                {
                    "record_id": "q1",
                    "rater": "model:chatgpt",
                    "dimension": "correctness",
                    "boundary": "sufficient",
                    "mean_rate": 1,
                    "observed_runs": 2,
                    "possible_runs": 3,
                    "valid_gradings": 4,
                },
                {
                    "record_id": "q2",
                    "rater": "model:chatgpt",
                    "dimension": "correctness",
                    "boundary": "sufficient",
                    "mean_rate": 0,
                    "observed_runs": 3,
                    "possible_runs": 3,
                    "valid_gradings": 6,
                },
            ],
        },
        inputs={},
        source="local",
    )
    first, labels, count = quality_level_records(
        data,
        {"q1": "질문 1", "q2": "질문 2"},
        ["model:chatgpt"],
        ["correctness"],
        {"model:chatgpt": "ChatGPT"},
        {"model:chatgpt": "icon"},
        "sufficient",
        "q1",
    )
    second, _, _ = quality_level_records(
        data,
        {"q1": "질문 1", "q2": "질문 2"},
        ["model:chatgpt"],
        ["correctness"],
        {"model:chatgpt": "ChatGPT"},
        {"model:chatgpt": "icon"},
        "sufficient",
        "q2",
    )
    assert count == 1 and labels["question"] == ["Q1"]
    assert first["label"] == ["100%", "—"] and first["scope"][0] == "answers 시행 2/3회"
    assert first["valid"] == [4, 0]
    assert second["label"] == ["0%", "—"] and second["scope"][0] == "answers 시행 3/3회"
    assert second["valid"] == [6, 0]


def test_quality_level_heatmap_uses_saved_model_mean_without_human():
    data = ReportData(
        kind="repeatability",
        report={
            "agreements": [{}, {}],
            "boundary_levels": [],
            "model_boundary_levels": [
                {
                    "record_id": "q1",
                    "dimension": "correctness",
                    "boundary": "sufficient",
                    "mean_rate": 0.5,
                    "observed_models": 2,
                    "possible_models": 3,
                    "observed_model_runs": 3,
                    "possible_model_runs": 6,
                    "valid_gradings": 8,
                }
            ],
            "question_average_levels": [
                {
                    "rater": MODEL_MEAN,
                    "dimension": "correctness",
                    "boundary": "sufficient",
                    "mean_rate": 0.4,
                    "observed_questions": 1,
                    "possible_questions": 2,
                    "valid_gradings": 8,
                }
            ],
            "dimension_average_levels": [
                {
                    "record_id": record_id,
                    "rater": MODEL_MEAN,
                    "boundary": "sufficient",
                    "mean_rate": rate,
                    "observed_dimensions": 1,
                    "possible_dimensions": 1,
                    "valid_gradings": 8,
                }
                for record_id, rate in (("q1", 0.5), (None, 0.4))
            ],
        },
        inputs={},
        source="local",
    )
    cells, _, count = quality_level_records(
        data,
        {"q1": "질문 1"},
        [MODEL_MEAN],
        ["correctness"],
        {MODEL_MEAN: "모델 평균"},
        {MODEL_MEAN: "icon"},
        "sufficient",
        "all",
    )
    assert count == 2 and cells["label"] == ["50%", "50%", "40%", "40%"]
    assert "모델 2/3개" in cells["scope"][0]
    assert cells["scope"][1] == "유효 항목 1/1개"
    assert cells["scope"][2] == "유효 문항 1/2개"
    assert cells["valid"] == [8, 8, 8, 8]


def test_grading_breakdown_distinguishes_runs_from_rating_observations():
    snapshot = {
        "bundle": {"cases": [{"error": None}] * 4},
        "grading_runs": [{"rater": "model:chatgpt"}] * 3,
    }
    rows = [{"rater": "model:chatgpt", "attempts": 12, "valid": 12}] * 4
    result = grading_breakdown(snapshot, "model:chatgpt", rows)
    assert result["run_count"] == 3
    assert result["headline"] == "grading 3회 × 4문항 = 항목당 12건"
    assert result["detail"] == "4항목 총 48건 · 유효 등급 48건"
    snapshot["grading_runs"] = snapshot["grading_runs"][:2]
    changed = grading_breakdown(snapshot, "model:chatgpt", rows)
    assert changed["run_count"] == 2
    assert "판정 건수와 실행 기록 불일치" in changed["headline"]


def test_boundary_change_view_uses_saved_paired_metrics():
    data = ReportData(
        kind="repeatability",
        report={
            "summaries": [{"rater": "model:chatgpt"}],
            "boundary_changes": {
                "overall": [
                    {
                        "boundary": name,
                        "mean_absolute_change": value,
                        "observed_pairs": 4,
                        "possible_pairs": 6,
                    }
                    for name, value in (("partial_or_better", 0.5), ("sufficient", 5 / 6))
                ],
                "by_rater": [
                    {
                        "rater": "model:chatgpt",
                        "boundary": name,
                        "mean_absolute_change": value,
                        "observed_pairs": 4,
                        "possible_pairs": 6,
                    }
                    for name, value in (("partial_or_better", 0.5), ("sufficient", 5 / 6))
                ],
                "by_dimension": [
                    {
                        "rater": "model:chatgpt",
                        "dimension": "correctness",
                        "boundary": name,
                        "mean_absolute_change": value,
                        "observed_pairs": 4,
                        "possible_pairs": 6,
                    }
                    for name, value in (("partial_or_better", 0.5), ("sufficient", 5 / 6))
                ],
            },
            "model_boundary_changes": {
                "overall": [
                    {
                        "boundary": name,
                        "mean_absolute_change": value,
                        "observed_pairs": 2,
                        "possible_pairs": 6,
                    }
                    for name, value in (("partial_or_better", 0.25), ("sufficient", 0.75))
                ],
                "by_dimension": [
                    {
                        "dimension": "correctness",
                        "boundary": name,
                        "mean_absolute_change": value,
                        "observed_models": 1,
                        "possible_models": 2,
                        "observed_pairs": 2,
                        "possible_pairs": 6,
                    }
                    for name, value in (("partial_or_better", 0.25), ("sufficient", 0.75))
                ],
            },
        },
        inputs={},
        source="local",
    )
    view = change_heatmap(data)
    assert list(view.objects[0].options.values()) == [MODEL_MEAN, "model:chatgpt"]
    plot = view.objects[2].objects[0].object
    assert [(item.dimension, item.location) for item in plot.center if isinstance(item, Span)] == [
        ("height", 0.5)
    ]
    records = next(
        renderer.data_source.data
        for renderer in plot.renderers
        if "label" in renderer.data_source.data
    )
    assert records["label"] == ["25.0%p", "25.0%p", "75.0%p", "75.0%p"]
    assert records["pairs"] == ["2/6"] * 4
    individual = _change_records(
        data,
        "model:chatgpt",
        ["correctness"],
        ["partial_or_better", "sufficient"],
        {"model:chatgpt": "ChatGPT"},
    )
    assert individual["label"] == ["50.0%p", "50.0%p", "83.3%p", "83.3%p"]


def test_repeatability_guide_reuses_rubric_and_defines_both_boundaries():
    rubric = demo("agreement").report["rubric"]
    data = ReportData(
        kind="repeatability",
        report={
            "boundary_changes": {
                "overall": [{"boundary": "partial_or_better"}, {"boundary": "sufficient"}]
            }
        },
        inputs={"agreements": [{"agreement": {"rubric": rubric}}]},
        source="local",
    )
    guide = repeatability_metric_guide(data)
    headings = [
        item.object
        for item in guide.objects
        if isinstance(item, pn.pane.Markdown) and item.object.startswith("### ")
    ]
    assert headings == [
        "### 답변별 채점 기준",
        "### 시행별 경계 충족률",
        "### 문항별 품질 수준",
        "### 시행 간 평균 변화폭",
    ]
    rubric_table = guide.objects[2].object
    boundary_table = guide.objects[4].object
    level_method = guide.objects[6].object
    method = guide.objects[8].object
    assert "정확성" in rubric_table and "N/A" in rubric_table
    assert "부분 충족 이상" in boundary_table and "충분" in boundary_table
    assert "유효 answers 시행 수" in level_method and "동시 충족률" in level_method
    assert "Wasserstein" in method and "|pᵢ-pⱼ|" in method
    assert "%p" in method and "합격 임계값은 설정하지 않았습니다" in method
