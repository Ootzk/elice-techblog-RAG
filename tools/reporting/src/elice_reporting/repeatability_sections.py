"""Repeatability condition panel using the analysis report's shared categories."""

from html import escape

import panel as pn
from markupsafe import Markup

from .analysis_sections import (
    _run_reference,
    _table,
    condition_categories,
    condition_selector,
)
from .analysis_visuals import _icon, analysis_subjects


def rater_display_names(data):
    raters = sorted({row["rater"] for row in data.report["summaries"]})
    human_ids = [rater for rater in raters if rater.startswith("human:")]
    observations = {rater: set() for rater in raters}
    for source in data.links.get("source_analyses", []):
        for subject in analysis_subjects(source):
            if subject["rater"] in observations:
                observations[subject["rater"]].add(subject["display_name"])
    labels = {}
    for rater in raters:
        names = observations[rater]
        labels[rater] = (
            next(iter(names))
            if len(names) == 1
            else f"검수자 {human_ids.index(rater) + 1}"
            if rater in human_ids
            else {
                "model:chatgpt": "ChatGPT",
                "model:claude": "Claude",
                "model:gemini": "Gemini",
            }.get(rater, rater)
        )
    return labels


def rater_icons(data):
    labels = rater_display_names(data)
    human_ids = [rater for rater in labels if rater.startswith("human:")]
    palette = ("#49748c", "#9270a3", "#498e8a", "#bb7c62", "#a49456", "#6c8c60")
    return {
        rater: _icon(
            rater,
            palette[human_ids.index(rater) % len(palette)] if rater in human_ids else "#64748b",
            None,
            [name],
        )
        for rater, name in labels.items()
    }


def grading_breakdown(snapshot, rater, summaries):
    """Describe recorded grading runs separately from per-dimension rating observations."""
    rows = [row for row in summaries if row["rater"] == rater]
    attempts = [row["attempts"] for row in rows]
    total = sum(attempts)
    valid = sum(row["valid"] for row in rows)
    eligible = sum(not case["error"] for case in snapshot["bundle"]["cases"])
    dimensions = len(rows)
    metadata = snapshot.get("grading_runs")
    run_count = (
        sum(item["rater"] == rater for item in metadata)
        if metadata is not None and (metadata or total == 0)
        else None
    )
    same_per_dimension = len(set(attempts)) == 1
    if run_count is not None and same_per_dimension and total == run_count * eligible * dimensions:
        headline = f"grading {run_count}회 × {eligible}문항 = 항목당 {attempts[0]}건"
    elif run_count is not None:
        reason = (
            "항목별 판정 건수 불균일" if not same_per_dimension else "판정 건수와 실행 기록 불일치"
        )
        headline = f"grading {run_count}회 기록 · {reason}"
    else:
        headline = "grading 실행 횟수 미기록"
    detail = f"{dimensions}항목 총 {total}건 · 유효 등급 {valid}건"
    if run_count is not None and total != run_count * eligible * dimensions:
        detail += " · 일부 채점 또는 실행 기록 누락"
    return {"run_count": run_count, "headline": headline, "detail": detail}


def repeatability_conditions(data):
    snapshots = data.inputs["analyses"]
    first = snapshots[0]
    links = data.links.get("analyses", [])
    answer_refs = []
    analysis_refs = []
    failed = []
    for index, snapshot in enumerate(snapshots):
        link = links[index] if index < len(links) else {}
        source_id = snapshot["bundle"]["source_run_id"]
        analysis_id = snapshot["analysis_run_id"]
        answer_refs.append(
            f"A{index + 1}: "
            + str(_run_reference(link.get("answers", {"run_id": source_id}), "답변 실행 기록"))
        )
        analysis_refs.append(
            f"A{index + 1}: "
            + str(_run_reference(link.get("analysis", {"run_id": analysis_id}), "분석 기록"))
        )
        errors = sum(bool(case["error"]) for case in snapshot["bundle"]["cases"])
        failed.append(f"A{index + 1} {errors}문항")
    categories = condition_categories(
        first["bundle"],
        failed=" / ".join(failed),
        rubric_id=first["analysis"]["rubric"]["id"],
        answers_reference=Markup("<br>".join(answer_refs)),
    )
    dataset = categories[0][1]
    categories[0] = (
        "데이터셋",
        pn.Column(dataset, _table([("원본 분석", Markup("<br>".join(analysis_refs)))])),
    )

    raters = sorted({row["rater"] for row in data.report["summaries"]})
    summary = data.report["summaries"]
    subject_rows = []
    display_names = rater_display_names(data)
    icons = rater_icons(data)
    unequal = []
    for rater in raters:
        counts = []
        grading_counts = []
        for index, analysis in enumerate(data.report["analyses"]):
            selected = [
                row
                for row in summary
                if row["rater"] == rater and row["analysis_run_id"] == analysis["analysis_run_id"]
            ]
            breakdown = grading_breakdown(snapshots[index], rater, selected)
            grading_counts.append(breakdown["run_count"])
            counts.append(
                "<strong>" + escape(breakdown["headline"]) + "</strong>"
                '<br><small style="color:#64748b">' + escape(breakdown["detail"]) + "</small>"
            )
        known = [count for count in grading_counts if count is not None]
        if len(known) == len(grading_counts) and len(set(known)) > 1:
            unequal.append(display_names[rater])
        subject_rows.append(
            '<tr><td><img src="'
            + escape(icons[rater], quote=True)
            + '" width="36" height="36" alt="평가 주체"></td><td><strong>'
            + escape(display_names[rater])
            + "</strong></td>"
            + "".join(f"<td>{count}</td>" for count in counts)
            + "</tr>"
        )
    headers = "".join(f"<th>A{index + 1}</th>" for index in range(len(snapshots)))
    warning = (
        "시행 간 grading 횟수가 다른 평가 주체: "
        + ", ".join(unequal)
        + ". Repeatability는 실행을 허용하며 유효 등급 분모를 함께 확인해야 합니다."
        if unequal
        else ""
    )
    categories.append(
        (
            "평가 주체",
            pn.Column(
                pn.pane.HTML(
                    '<table style="width:100%;border-collapse:collapse;font:14px system-ui">'
                    "<thead><tr><th></th><th>평가 주체 · 모델</th>"
                    + headers
                    + "</tr></thead><tbody>"
                    + "".join(subject_rows)
                    + "</tbody></table>",
                    stylesheets=[
                        "th,td{text-align:left;vertical-align:middle;padding:12px;"
                        "border-bottom:1px solid #e8edf3;}th{color:#64748b;font-size:12px;}"
                    ],
                ),
                *([pn.pane.Markdown(warning)] if warning else []),
            ),
        )
    )
    return condition_selector(categories)
