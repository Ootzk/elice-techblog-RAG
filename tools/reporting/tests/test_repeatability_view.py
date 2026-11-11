import panel as pn
from bokeh.models.glyphs import ImageURL

from elice_reporting.data import ReportData
from elice_reporting.repeatability_sections import grading_breakdown
from elice_reporting.repeatability_view import question_summaries, summary_heatmaps


def test_gold_report_shows_both_boundaries_for_every_answers_run_with_icons():
    analyses = [{"analysis_run_id": f"analysis-{i}"} for i in range(3)]
    summaries = []
    snapshots = []
    for run in analyses:
        distributions = []
        for rater in ("model:chatgpt", "model:claude"):
            for dimension in ("correctness", "completeness"):
                summary = {
                    "analysis_run_id": run["analysis_run_id"],
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
                "analysis_run_id": run["analysis_run_id"],
                "analysis": {"distributions": distributions},
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
        report={"summaries": summaries, "analyses": analyses},
        inputs={"analyses": snapshots},
        source="local",
    )
    view = summary_heatmaps(data)
    assert len(view.objects) == 7  # selector, question, legend, then title + grid per boundary
    selector = view.objects[0]
    assert isinstance(selector, pn.widgets.Select)
    assert list(selector.options.values()) == ["all", "q1", "q2"]
    for section in (view.objects[4], view.objects[6]):
        grid = section.objects[0]
        assert isinstance(grid, pn.GridBox)
        assert grid.ncols == 2
        matrices = grid.objects
        assert len(matrices) == 3
        for matrix in matrices:
            plot = matrix.object
            icons = [
                renderer.glyph
                for renderer in plot.renderers
                if isinstance(getattr(renderer, "glyph", None), ImageURL)
            ]
            assert len(icons) == 1
            assert icons[0].w_units == icons[0].h_units == "screen"
            assert icons[0].w == icons[0].h == 30
    selected = question_summaries(data, "q1")
    assert len(selected) == len(summaries)
    assert selected[0]["boundaries"]["sufficient"]["denominator"] == 3
    missing = question_summaries(data, "q2")
    assert all(row["missing_records"] == 1 for row in missing)
    assert all(row["generation_failed_records"] == 1 for row in missing)
    assert all(row["boundaries"] == {} for row in missing)


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
