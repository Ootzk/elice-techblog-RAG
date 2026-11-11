"""Repeatability condition panel using the analysis report's shared categories."""

from html import escape

import panel as pn
from markupsafe import Markup

from .analysis_sections import (
    _run_reference,
    _table,
    condition_categories,
    condition_selector,
    rubric_guide,
)
from .analysis_visuals import _icon, analysis_subjects
from .presentation import metric_label

BOUNDARY_LABELS = {
    "partial_or_better": "부분 충족 이상",
    "sufficient": "충분",
    "satisfied": "충족",
}


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


def repeatability_metric_guide(data):
    """Show the frozen rubric and definitions of the saved repeatability measures."""
    rubric = data.inputs["analyses"][0]["analysis"]["rubric"]
    criteria = {
        "partial_or_better": "유효 등급 중 1점 또는 2점의 비율. 0점은 미충족.",
        "sufficient": "유효 등급 중 2점의 비율. 0점과 1점은 미충족.",
        "satisfied": "유효 등급 중 1점의 비율. 0점은 미충족.",
    }
    criterion_rows = [
        (BOUNDARY_LABELS[item["boundary"]], criteria[item["boundary"]])
        for item in data.report["boundary_changes"]["overall"]
    ]
    return pn.Column(
        *rubric_guide(rubric, repeatability=True).objects,
        pn.pane.Markdown("### 시행별 경계 충족률"),
        _table(criterion_rows),
        pn.pane.Markdown("### 시행 간 평균 변화폭"),
        pn.pane.Markdown(
            "- **비교 단위:** 같은 질문·평가 항목·평가 주체의 서로 다른 answers 시행 쌍입니다. "
            "동일 주체의 grading 회차 번호를 시행 사이에 대응시키지 않습니다.\n"
            "- **계산:** 각 경계의 충족 여부를 0/1로 바꾸고, 시행별 유효 grading에서 "
            "충족률 `p`를 구합니다. 이 이진 분포 사이의 1차 Wasserstein 거리 "
            "`W₁(Bernoulli(pᵢ), Bernoulli(pⱼ)) = |pᵢ-pⱼ|`를 시행 쌍마다 계산해 "
            "질문 안에서 평균합니다. 질문·평가 항목·평가 주체를 "
            "차례로 동일 가중치 평균합니다.\n"
            "- **단위와 방향:** `%p`입니다. 0%p면 관측된 질문별 충족률이 시행마다 같으며, "
            "값이 클수록 변화가 큽니다. 품질 수준은 위 경계 충족률로 따로 확인합니다.\n"
            "- **제외:** 양쪽 시행 중 유효 등급이 없는 쌍은 계산에서 제외합니다. "
            "비교 가능/전체 건수를 함께 표시합니다.\n"
            "- **판정:** 합격 임계값은 설정하지 않았습니다. 작은 반복 표본의 기술 통계이며 "
            "Judge 자체의 채점 변동도 포함됩니다."
        ),
        sizing_mode="stretch_width",
    )


def boundary_change_summary(data):
    """Display saved question-paired changes in percentage points."""
    changes = data.report["boundary_changes"]
    overall = {item["boundary"]: item for item in changes["overall"]}
    rater_rows = {(item["rater"], item["boundary"]): item for item in changes["by_rater"]}
    dimension_rows = {
        (item["rater"], item["dimension"], item["boundary"]): item
        for item in changes["by_dimension"]
    }
    boundaries = list(overall)
    raters = sorted({item["rater"] for item in changes["by_rater"]})
    dimensions = sorted({item["dimension"] for item in changes["by_dimension"]})
    names, icons = rater_display_names(data), rater_icons(data)

    def amount(value):
        return "—" if value is None else f"{value * 100:.1f}%p"

    cards = "".join(
        '<div style="flex:1;min-width:250px;padding:16px 20px;border-radius:14px;'
        'background:#eef5f3;border:1px solid #d3e5df;font-family:system-ui">'
        f'<div style="font-size:13px;color:#526b68">{escape(BOUNDARY_LABELS[boundary])}'
        " · 시행 간 평균 변화폭</div>"
        f'<strong style="display:block;margin:7px 0;font-size:30px;color:#174b62">'
        f"{amount(item['mean_absolute_change'])}</strong>"
        f'<small style="color:#64748b">비교 가능 '
        f"{item['observed_pairs']}/{item['possible_pairs']}건</small></div>"
        for boundary, item in overall.items()
    )
    headers = "".join(f"<th>{escape(BOUNDARY_LABELS[boundary])}</th>" for boundary in boundaries)
    rows = []
    for rater in raters:
        rater_cells = "".join(
            f"<td><strong>{amount(rater_rows[(rater, boundary)]['mean_absolute_change'])}"
            "</strong></td>"
            for boundary in boundaries
        )
        coverage = rater_rows[(rater, boundaries[0])]
        rows.append(
            '<tr><td><img src="'
            + escape(icons[rater], quote=True)
            + '" width="30" height="30" alt="평가 주체"></td><td><strong>'
            + escape(names[rater])
            + "</strong></td><td>전체 항목</td>"
            + rater_cells
            + f"<td>{coverage['observed_pairs']}/{coverage['possible_pairs']}</td></tr>"
        )
        for dimension in dimensions:
            item = dimension_rows.get((rater, dimension, boundaries[0]))
            if item is None:
                continue
            values = [
                dimension_rows[(rater, dimension, boundary)]["mean_absolute_change"]
                for boundary in boundaries
            ]
            cells = "".join(f"<td>{amount(value)}</td>" for value in values)
            rows.append(
                "<tr><td></td><td></td><td>"
                + escape(metric_label(dimension))
                + "</td>"
                + cells
                + f"<td>{item['observed_pairs']}/{item['possible_pairs']}</td></tr>"
            )
    return pn.Column(
        pn.pane.HTML(
            '<div style="display:flex;gap:14px;flex-wrap:wrap">' + cards + "</div>",
            sizing_mode="stretch_width",
        ),
        pn.pane.Markdown(
            "같은 질문의 answers 시행 쌍에서 경계 충족률 차이의 절댓값을 구한 뒤 "
            "시행 쌍·질문·평가 항목·평가 주체 순서로 동일 가중치 평균한 값입니다. "
            "낮을수록 관측된 판정 비율 변화가 작습니다. 유효 등급이 없는 시행 쌍은 제외하며 "
            "답변 품질 수준은 아래 시행별 충족률에서 확인합니다."
        ),
        pn.pane.HTML(
            '<table style="width:100%;border-collapse:collapse;font:13px system-ui">'
            "<thead><tr><th></th><th>평가 주체</th><th>평가 항목</th>"
            + headers
            + "<th>비교 가능/전체</th></tr></thead><tbody>"
            + "".join(rows)
            + "</tbody></table>",
            stylesheets=[
                "th,td{text-align:left;vertical-align:middle;padding:10px;"
                "border-bottom:1px solid #e8edf3;}th{color:#64748b;font-size:12px;}"
            ],
        ),
        sizing_mode="stretch_width",
    )


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
