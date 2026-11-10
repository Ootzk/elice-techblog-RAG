"""Recorded experiment metadata, metric definitions and offline item selection."""

import json
from html import escape
from urllib.parse import urlsplit

import panel as pn
from markupsafe import Markup

from .analysis_visuals import (
    DIMENSIONS,
    grading_cells,
    grading_heatmap,
    grading_legend,
    matrix_size,
    rater_legend,
)
from .presentation import text_block


def _table(rows):
    return pn.pane.HTML(
        '<table style="width:100%;border-collapse:collapse;font:14px system-ui">'
        + "".join(
            '<tr><th style="width:220px;text-align:left;vertical-align:top;padding:9px;'
            'border-bottom:1px solid #e8edf2;color:#526071">' + escape(str(key)) + "</th>"
            '<td style="padding:9px;border-bottom:1px solid #e8edf2;overflow-wrap:anywhere">'
            + (str(value) if isinstance(value, Markup) else escape(str(value)))
            + "</td></tr>"
            for key, value in rows
        )
        + "</table>",
        sizing_mode="stretch_width",
    )


def _run_reference(link, label=None):
    identity = link.get("run_id", "미기록")
    url = link.get("url")
    label = label or identity
    if url and urlsplit(url).scheme in {"http", "https"}:
        return Markup(
            f'<a href="{escape(url, quote=True)}" target="_blank" rel="noopener noreferrer" '
            f'title="{escape(identity, quote=True)}">{escape(label)} ↗</a>'
        )
    return Markup(escape(label))


def evaluation_conditions(data, subjects):
    bundle = data.inputs["bundle"]
    profile = bundle["qa_profile"]
    models = list(
        dict.fromkeys(
            c["result"]["generation_model"]
            for c in bundle["cases"]
            if c.get("result") and c["result"].get("generation_model")
        )
    )
    answers = data.links.get("answers", {"run_id": bundle["source_run_id"]})
    dataset = [
        ("데이터셋 유형", bundle["kind"]),
        ("데이터셋 ID", bundle["dataset_id"]),
        ("데이터셋 내용 해시", bundle["dataset_digest"]),
        ("문항 수 / 생성 실패", f"{len(bundle['records'])} / {data.report['qa_failed_cases']}"),
        ("원본 answers", _run_reference(answers)),
    ]
    settings = [
        (key, json.dumps(value, ensure_ascii=False))
        for key, value in profile.items()
        if not isinstance(value, (dict, list))
    ]
    for name, prompt in profile.get("prompts", {}).items():
        if isinstance(prompt, dict):
            settings.append(
                (
                    f"프롬프트 · {name}",
                    json.dumps(
                        {
                            key: prompt[key]
                            for key in ("name", "version", "digest", "source")
                            if key in prompt
                        },
                        ensure_ascii=False,
                    ),
                )
            )
    return pn.Column(
        _table(dataset),
        pn.pane.Markdown("### 답변 생성·검색 설정"),
        _table([("기록된 응답 모델", ", ".join(models) or "미기록"), *settings]),
        pn.pane.Markdown("### 평가 주체"),
        rater_legend(subjects),
        sizing_mode="stretch_width",
    )


_METRIC_DOCS = {
    "FactualCorrectness.precision": "https://docs.ragas.io/en/latest/concepts/metrics/available_metrics/factual_correctness/",
    "FactualCorrectness.recall": "https://docs.ragas.io/en/latest/concepts/metrics/available_metrics/factual_correctness/",
    "Faithfulness": "https://docs.ragas.io/en/latest/concepts/metrics/available_metrics/faithfulness/",
    "DiscreteMetric": "https://docs.ragas.io/en/latest/references/metrics/",
}


def _metric_link(implementation):
    url = _METRIC_DOCS[implementation]
    return (
        f'<a href="{url}" target="_blank" rel="noopener noreferrer">'
        f"<code>{escape(implementation)}</code> ↗</a>"
    )


METRIC_DEFINITIONS = (
    (
        "correctness · 정확성",
        "FactualCorrectness.precision",
        "답변의 사실 주장 중 기준 답안의 사실과 일치하는 비율을 평가합니다. "
        "질문의 필수 내용을 빠뜨렸는지는 completeness에서 별도로 다룹니다.",
        "답변 · 기준 답안",
    ),
    (
        "completeness · 완전성",
        "FactualCorrectness.recall",
        "기대되는 필수 사실 중 답변이 포함한 비율을 평가합니다. "
        "필수 항목(required_points)이 기준이며 표현이 완전히 같은지를 비교하지 않습니다.",
        "답변 · 필수 항목",
    ),
    (
        "groundedness · 근거 충실도",
        "Faithfulness",
        "답변의 사실 주장이 최종 답변 생성에 전달된 문맥으로 뒷받침되는지 평가합니다. "
        "전체 검색 후보나 기준 답안, 일반 상식은 이 항목의 근거로 사용하지 않습니다. "
        "사실이 맞는 답변이라도 생성 문맥에 근거가 없으면 낮은 점수를 받을 수 있습니다.",
        "답변 · generation_sources",
    ),
    (
        "relevance · 질문 적합성",
        "DiscreteMetric",
        "답변이 질문과 고정된 대화의 실제 요청에 대응하는지 평가합니다. "
        "관련된 확인 질문, 근거 부족 설명, 정책에 맞는 거절도 요청에 대응할 수 있습니다. "
        "사실 정확도나 필수 내용 충족 여부는 다른 항목에서 평가합니다.",
        "질문·대화 · 답변 · 저장된 rubric",
    ),
)


def metric_guide(data):
    definitions = (
        METRIC_DEFINITIONS
        if data.inputs["bundle"]["kind"] == "gold"
        else (
            (
                "behavior · 응답 행동",
                "DiscreteMetric",
                "기대 행동(답변 보류·거절·범위 안내 등)과 응답의 일치를 "
                "저장된 rubric으로 평가합니다.",
                "질문·대화 · 답변 · expected_behavior · 저장된 rubric",
            ),
        )
    )
    description_rows = "".join(
        f"<tr><td>{escape(name)}</td><td>{_metric_link(implementation)}</td>"
        f"<td>{escape(inputs)}</td><td>{escape(definition)}</td></tr>"
        for name, implementation, definition, inputs in definitions
    )
    descriptions = pn.pane.HTML(
        '<table style="width:100%;border-collapse:collapse;font:14px system-ui">'
        "<thead><tr><th>지표</th><th>모델 평가 구현</th>"
        "<th>입력 범위</th><th>평가 기준</th></tr></thead>"
        "<tbody>" + description_rows + "</tbody></table>",
        stylesheets=[
            "th,td{text-align:left;vertical-align:top;padding:12px;"
            "border-bottom:1px solid #e4e9ef;}"
            "th{color:#526071;}td{line-height:1.65;}"
        ],
        sizing_mode="stretch_width",
    )
    thresholds = sorted(
        {
            str(p["ragas"]["pass_threshold"])
            for p in data.inputs.get("profiles", {}).values()
            if p.get("ragas", {}).get("pass_threshold") is not None
        }
    )
    threshold_note = (
        "저장된 모델 통과 임계값: " + ", ".join(thresholds) + "."
        if thresholds
        else "통과 임계값: 프로필 미기록."
    )
    if thresholds == ["1.0"]:
        threshold_note += (
            " 이 실행은 각 문항·항목별 원점수 1.0 이상만 pass이며 0.99도 fail입니다. "
            "Ragas가 강제하는 합격선이 아니라 프로젝트의 엄격한 이진 변환 기준입니다. "
            "지표의 반올림이 포함된 원점수를 기준으로 하므로 모든 개별 검증이 "
            "확인됐다는 뜻과 완전히 같지는 않습니다."
        )
    score_note = "모델 평가 점수는 0~1 범위입니다."
    grading_notes = pn.pane.Markdown(
        "- **원점수:** " + score_note + "\n"
        "- **통과 판정:** 저장된 pass/fail을 그대로 표시합니다. "
        "사람은 rubric에 따라 직접 판정합니다. " + threshold_note + "\n"
        "- **fail의 의미:** 저장된 통과 기준 미달입니다. 답변 전체가 틀렸다는 뜻은 아닙니다. "
        "원점수와 개별 주장 검증을 함께 확인하세요.\n"
        "- **사람–모델 비교:** 사람의 rubric 판정과 모델의 수치 임계값은 서로 다른 규칙입니다. "
        "κ에는 모델의 내용 판단뿐 아니라 이 통과 기준 차이도 영향을 줍니다.\n"
        "- **N/A:** 적용 대상이 아닌 항목이며 fail이나 0점으로 대체하지 않습니다.\n"
        "- **오류/미기록:** 채점 실패와 판정 부재를 구분하며 "
        "일치도 계산에서 유효 이진 판정으로 취급하지 않습니다."
    )
    rules = [
        (
            "Cohen κ",
            "κ = (관측 일치율 − 우연 일치율) / (1 − 우연 일치율). 두 평가자의 판정 일치도를 "
            "우연 일치에 대해 보정합니다. "
            "대칭이며 +1은 완전 일치, 0은 우연 수준, 음수도 가능합니다. "
            "답변 정확도와는 다른 지표입니다. 유효 표본과 미정의 사유는 hover에 표시합니다.",
        ),
        (
            "Fleiss κ",
            "여러 평가자의 범주 판정 일치도를 우연 일치에 대해 보정합니다. "
            "선택된 평가자 전원이 이진 판정을 제공한 문항을 사용합니다.",
        ),
        (
            "Krippendorff α · 명목형",
            "여러 평가자의 불일치 정도를 기대 불일치와 비교합니다. "
            "문항별로 최소 두 평가자의 판정이 있으면 사용하므로 일부 판정 누락을 허용합니다.",
        ),
    ]
    return pn.Column(
        pn.pane.Markdown("### Grading · 개별 답변 채점"),
        grading_notes,
        descriptions,
        pn.pane.Markdown("### Analysis · 평가자 간 일치도"),
        _table(rules),
        sizing_mode="stretch_width",
    )


def item_switch_code(count):
    return (
        "const items = [" + ",".join(f"item_{i}" for i in range(count)) + "];\n"
        "items.forEach((item, index) => {item.visible = index === Number(cb_obj.value);});"
    )


def item_browser(data, subjects, case_ids):
    if not case_ids:
        return pn.pane.Markdown("저장된 문항이 없습니다.")
    bundle = data.inputs["bundle"]
    records = {r["id"]: r for r in bundle["records"]}
    cases = {c["record_id"]: c for c in bundle["cases"]}
    items, options = [], {}
    for i, record_id in enumerate(case_ids):
        question = records[record_id]["inputs"]["question"]
        case = cases[record_id]
        result = case.get("result") or {}
        options[f"{i + 1}. {question[:100]}{'…' if len(question) > 100 else ''}"] = str(i)
        items.append(
            pn.Column(
                text_block("질문", question),
                text_block("답변", result.get("answer")),
                *([text_block("생성 오류", case["error"])] if case.get("error") else []),
                pn.Row(
                    pn.pane.HoloViews(grading_heatmap(data, subjects, record_id)),
                    rationale_browser(data, subjects, record_id),
                    sizing_mode="stretch_width",
                ),
                context_browser(result),
                visible=i == 0,
                sizing_mode="stretch_width",
            )
        )
    selector = pn.widgets.Select(
        label="문항 선택", options=options, value="0", sizing_mode="stretch_width"
    )
    selector.jscallback(
        args={f"item_{i}": item for i, item in enumerate(items)}, value=item_switch_code(len(items))
    )
    return pn.Column(selector, grading_legend(), *items, sizing_mode="stretch_width")


def context_browser(result):
    """Display frozen generation context and distinguish unused retrieval snapshots."""
    generated = result.get("generation_sources")
    retrieved = result.get("retrieved_sources", [])
    heading = pn.pane.Markdown("### 문맥 자료")
    note = pn.pane.Markdown(
        "**생성 문맥**은 최종 답변 생성에 전달된 본문이며 근거 충실도 검사의 대상입니다. "
        "**검색 전용**은 검색됐지만 같은 본문이 생성 문맥에 포함되지 않은 자료입니다. "
        "인용 번호는 답변의 출처 번호이며, 인용되지 않은 생성 문맥도 검사에 사용됩니다."
    )

    def identity(source):
        return (source.get("chunk_id", source.get("url")), source.get("text"))

    selected = {identity(s) for s in generated or []}
    entries = [("생성 문맥", i, source) for i, source in enumerate(generated or [], 1)]
    entries.extend(
        ("검색 전용", i, source)
        for i, source in enumerate(retrieved, 1)
        if identity(source) not in selected
    )
    missing = (
        "생성 문맥이 기록되지 않았습니다."
        if generated is None
        else "이 답변에 전달된 생성 문맥은 없습니다."
        if not generated
        else ""
    )
    if not entries:
        return pn.Column(heading, note, pn.pane.Markdown(missing), sizing_mode="stretch_width")
    views, options = [], {}
    for index, (scope, number, source) in enumerate(entries):
        citations = [
            str(c["source_id"])
            for c in result.get("citations", [])
            if scope == "생성 문맥"
            and source.get("chunk_id")
            and c.get("chunk_id") == source["chunk_id"]
        ]
        citation = " · 인용 [" + ", ".join(citations) + "]" if citations else ""
        title = source.get("title") or "제목 미기록"
        options[f"{scope} {number}{citation} · {title}"] = str(index)
        url = source.get("url", "")
        reference = (
            f'<a href="{escape(url, quote=True)}" target="_blank" rel="noopener noreferrer">'
            f"{escape(url)} ↗</a>"
            if urlsplit(url).scheme in {"http", "https"}
            else escape(url)
        )
        view = pn.pane.HTML(
            f'<h4 style="margin:0 0 8px">{escape(scope)} {number}{escape(citation)} · '
            f"{escape(title)}</h4>"
            f'<div style="overflow-wrap:anywhere">{reference}</div>'
            f'<p style="color:#64748b">{escape(source.get("heading_path") or "섹션 미기록")}</p>'
            '<div style="white-space:pre-wrap;overflow-wrap:anywhere;line-height:1.7">'
            + escape(source.get("text") or "본문 미기록")
            + "</div>",
            visible=index == 0,
            sizing_mode="stretch_width",
        )
        views.append(view)
    selector = pn.widgets.Select(
        label="문맥 선택", options=options, value="0", sizing_mode="stretch_width"
    )
    selector.jscallback(
        args={f"item_{i}": view for i, view in enumerate(views)},
        value=item_switch_code(len(views)),
    )
    return pn.Column(
        heading,
        note,
        *([pn.pane.Markdown(missing)] if missing else []),
        selector,
        pn.Column(*views, height=320, scroll=True, sizing_mode="stretch_width"),
        sizing_mode="stretch_width",
    )


def rationale_filter_code(counts):
    code = []
    for i, count in enumerate(counts):
        code.append(f"group_{i}.visible = rater.value === 'all' || rater.value === '{i}';")
        for j in range(count):
            code.append(
                f"card_{i}_{j}.visible = dimension.value === 'all' || dimension.value === '{j}';"
            )
    return "\n".join(code)


def rationale_markup(reason, dimension):
    """Format stored Ragas verifications; never summarize or regenerate reasons."""

    def plain(value):
        return (
            '<div style="white-space:pre-wrap;overflow-wrap:anywhere;line-height:1.65">'
            + escape(value)
            + "</div>"
        )

    try:
        steps = json.loads(reason)
    except (ValueError, TypeError):
        return plain(reason or "저장된 사유 없음")
    if not isinstance(steps, list) or not all(isinstance(step, dict) for step in steps):
        return plain(json.dumps(steps, ensure_ascii=False, indent=2))
    sections = []
    verification_index = 0
    for i, step in enumerate(steps):
        output = step.get("output", {})
        if not isinstance(output, dict):
            sections.append(plain(json.dumps(step, ensure_ascii=False, indent=2)))
            continue
        statements = output.get("statements")
        if (
            isinstance(statements, list)
            and statements
            and all(
                isinstance(s, dict) and {"statement", "reason", "verdict"} <= s.keys()
                for s in statements
            )
        ):
            verification_index += 1
            if dimension == "correctness":
                heading = "답변 내용 검증"
                explanation = (
                    "답변을 개별 주장으로 나누고, 각 주장을 기준 답안에서 "
                    "확인할 수 있는지 검사합니다. "
                    "기준 답안의 필수 내용을 빠뜨렸는지는 완전성에서 별도로 봅니다."
                )
            elif dimension == "completeness" and verification_index == 1:
                heading = "1. 답변 내용 검증"
                explanation = (
                    "답변에 쓴 각 주장을 기준 필수 내용에서 확인할 수 있는지 "
                    "검사한 중간 결과입니다. "
                    "예를 들어 트레이스 설명이 맞아도 감사 로그 설명을 빠뜨렸는지는 "
                    "이 검사만으로 알 수 없습니다."
                )
            elif dimension == "completeness":
                heading = "2. 필수 내용 누락 검사"
                explanation = (
                    "반드시 포함해야 할 내용을 하나씩 꺼내 답변이 실제로 담았는지 검사합니다. "
                    "예를 들어 트레이스와 감사 로그가 모두 필수인데 트레이스만 설명했다면, "
                    "감사 로그 내용은 답변에서 확인하지 못한 것으로 표시될 수 있습니다. "
                    "두 검사는 확인 대상과 대조 자료가 서로 다릅니다."
                )
            elif dimension == "groundedness":
                heading = "답변 내용의 근거 확인"
                explanation = (
                    "답변의 각 주장을 최종 답변 생성에 전달된 문맥이 뒷받침하는지 검사합니다. "
                    "기준 답안이나 전달되지 않은 검색 결과는 이 검사의 근거가 아닙니다."
                )
            else:
                heading, explanation = "주장 검증", "저장된 주장별 검증 결과입니다."
            rows = []
            for index, statement in enumerate(statements, 1):
                verdict = statement["verdict"]
                label, color = (
                    ("확인됨", "#2166ac")
                    if verdict == 1
                    else (("확인 못함", "#b64e3a") if verdict == 0 else (str(verdict), "#64748b"))
                )
                rows.append(
                    '<tr style="vertical-align:top">'
                    f'<td style="width:42%;padding:12px 9px;border-bottom:1px solid #e4e9ef">'
                    f'<small style="color:#64748b">{index}</small><br>'
                    f"{escape(statement['statement'])}</td>"
                    '<td style="width:14%;padding:12px 6px;border-bottom:1px solid #e4e9ef;'
                    f'color:{color}">'
                    f"{escape(label)}</td>"
                    '<td style="padding:12px 9px;border-bottom:1px solid #e4e9ef;'
                    'white-space:pre-wrap">'
                    f"{escape(statement['reason'])}</td></tr>"
                )
            sections.append(
                f'<h5 style="font-size:14px;margin:16px 0 8px">{heading}</h5>'
                f'<p style="font:13px/1.65 system-ui;color:#526071">{escape(explanation)}</p>'
                '<p style="font:12px/1.6 system-ui;color:#64748b">'
                "확인 못함은 해당 대조 자료에서 뒷받침되지 않았다는 Judge 판정이며, "
                "사실 자체가 반드시 거짓이라는 뜻은 아닙니다.</p>"
                '<table style="width:100%;table-layout:fixed;border-collapse:collapse;'
                'font:13px/1.6 system-ui;overflow-wrap:anywhere"><thead><tr>'
                '<th style="width:42%;text-align:left">주장</th>'
                '<th style="width:14%;text-align:left">판정</th>'
                '<th style="text-align:left">저장된 판정 사유</th></tr></thead><tbody>'
                + "".join(rows)
                + "</tbody></table>"
            )
            continue
        # Omit duplicate extraction text only when the next verification contains
        # every extracted statement in the same order. Unmatched data stays visible.
        extracted = output.get("claims", output.get("statements"))
        next_output = steps[i + 1].get("output", {}) if i + 1 < len(steps) else {}
        verified = next_output.get("statements", []) if isinstance(next_output, dict) else []
        if isinstance(extracted, list) and all(isinstance(s, str) for s in extracted):
            if extracted == [s.get("statement") for s in verified if isinstance(s, dict)]:
                continue
            sections.append(
                "<h5>추출된 주장</h5><ul>"
                + "".join('<li style="margin:8px 0">' + escape(s) + "</li>" for s in extracted)
                + "</ul>"
            )
            continue
        sections.append(plain(json.dumps(step, ensure_ascii=False, indent=2)))
    return "".join(sections) or plain(reason or "저장된 사유 없음")


def rationale_browser(data, subjects, record_id):
    """Preloaded, escaped reasons; both filters work without a Python server."""
    dimensions = DIMENSIONS if data.inputs["bundle"]["kind"] == "gold" else ("behavior",)
    cells = grading_cells(data, subjects, record_id, dimensions)
    raters = {"전체 평가자": "all"}
    raters.update({s["label"]: str(i) for i, s in enumerate(subjects)})
    initial = next((i for i, s in enumerate(subjects) if s["rater"].startswith("model:")), 0)
    rater = pn.widgets.Select(label="평가자 · 회차", options=raters, value=str(initial))
    dimension = pn.widgets.Select(
        label="Judge 항목",
        options={"전체 항목": "all", **{d: str(i) for i, d in enumerate(dimensions)}},
        value="0",
    )
    args, groups = {"rater": rater, "dimension": dimension}, []
    for i, subject in enumerate(subjects):
        cards = []
        for j, name in enumerate(dimensions):
            row = cells[(cells.x == i) & (cells.dimension == name)].iloc[0]
            card = pn.pane.HTML(
                '<section style="border-top:1px solid #e4e9ef;padding:10px 0">'
                f"<b>{escape(name)}</b> · {escape(row['label'])}"
                f'<span style="color:#64748b"> · {escape(row["score_display"])}</span>'
                '<div style="margin-top:12px">'
                + rationale_markup(row["rationale"], name)
                + "</div></section>",
                sizing_mode="stretch_width",
                visible=j == 0,
            )
            args[f"card_{i}_{j}"] = card
            cards.append(card)
        group = pn.Column(
            pn.pane.HTML(f'<h4 style="margin:12px 0">{escape(subject["label"])}</h4>'),
            *cards,
            sizing_mode="stretch_width",
            visible=i == initial,
        )
        args[f"group_{i}"] = group
        groups.append(group)
    code = rationale_filter_code([len(dimensions)] * len(subjects))
    for control in (rater, dimension):
        control.jscallback(args=args, value=code)
    return pn.Column(
        pn.Row(rater, dimension, sizing_mode="stretch_width"),
        pn.Column(
            *groups,
            height=max(560, matrix_size(subjects) - 55),
            scroll=True,
            sizing_mode="stretch_width",
        ),
        sizing_mode="stretch_width",
        min_width=380,
    )
