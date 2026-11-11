"""A/B report over two frozen repeatability runs."""

from html import escape

import panel as pn
from bokeh.models import (
    ColumnDataSource,
    CustomJSTickFormatter,
    HoverTool,
    LabelSet,
    NumeralTickFormatter,
)
from bokeh.plotting import figure
from bokeh.transform import dodge
from markupsafe import Markup

from .agreement_sections import (
    _run_reference,
    _table,
    condition_selector,
    rubric_guide,
)
from .agreement_visuals import agreement_subjects
from .comparison_sections import _ab_table, _rows, _selector, _settings
from .conditions import condition_sections
from .presentation import metric_label, model_name
from .stacked_efficiency import paired_answer_groups, stack_chart

BOUNDARIES = {
    "partial_or_better": "부분 충족 이상 · 1점 또는 2점",
    "sufficient": "충분 · 2점",
    "satisfied": "충족 · 1점",
}
DIMENSIONS = ("correctness", "completeness", "groundedness", "relevance", "behavior")
COLORS = {"A": "#51758e", "B": "#d07b58"}


def _profiles(data):
    return {
        side: data.inputs[side]["inputs"]["agreements"][0]["bundle"]["qa_profile"]
        for side in ("baseline", "candidate")
    }


def subject_info(data):
    """Use model names and icons observed in the original Agreement traces."""
    raters = sorted(data.report["rater_profiles"])
    sources = data.links.get("source_repeatabilities", {})
    subjects = {}
    for side in ("baseline", "candidate"):
        details = sources.get(side)
        source_agreements = details.links.get("source_agreements", []) if details else []
        subjects[side] = (
            {subject["rater"]: subject for subject in agreement_subjects(source_agreements[0])}
            if source_agreements
            else {}
        )
    result = {}
    for index, rater in enumerate(raters):
        a, b = subjects["baseline"].get(rater), subjects["candidate"].get(rater)
        names = [s["display_name"] for s in (a, b) if s]
        label = (
            names[0]
            if len(set(names)) == 1
            else " / ".join(names)
            if names
            else (
                f"검수자 {index + 1}"
                if rater.startswith("human:")
                else rater.removeprefix("model:").title()
            )
        )
        result[rater] = dict(label=label, icon=(a or b or {}).get("icon"))
    result["aggregate:models"] = dict(label="전체 모델 평균", icon=None)
    return result


def _subject_markup(name, item):
    icon = item["icon"]
    image = (
        f'<img src="{escape(icon, quote=True)}" width="36" height="36" '
        'style="width:36px;height:36px;flex:none;object-fit:contain" alt="">'
        if icon
        else '<span style="width:36px;flex:none;text-align:center">∑</span>'
    )
    return Markup(
        '<span style="display:inline-flex;align-items:center;gap:9px;min-height:38px;'
        'vertical-align:middle;line-height:1.35">' + image + escape(name) + "</span>"
    )


def conditions(data, subjects):
    report = data.report
    ap, bp = (_profiles(data)[side] for side in ("baseline", "candidate"))
    left, right = (data.inputs[side] for side in ("baseline", "candidate"))
    answers_a, answers_b = report["baseline"]["answers_runs"], report["candidate"]["answers_runs"]
    model_a, model_b = model_name(ap["generation_model"]), model_name(bp["generation_model"])
    changed = ap["generation_model"] != bp["generation_model"]
    summary = _ab_table([("답변 모델", model_a, model_b, changed)])
    dataset_a = left["inputs"]["agreements"][0]["bundle"]
    dataset_b = right["inputs"]["agreements"][0]["bundle"]
    categories = [
        (
            "데이터셋",
            _ab_table(
                [
                    (
                        "데이터셋",
                        "🥇 Gold" if dataset_a["kind"] == "gold" else "🦾 Iron",
                        "🥇 Gold" if dataset_b["kind"] == "gold" else "🦾 Iron",
                        False,
                    ),
                    (
                        "평가 문항",
                        f"{len(dataset_a['records'])}문항",
                        f"{len(dataset_b['records'])}문항",
                        False,
                    ),
                    ("독립 답변 생성", f"{answers_a}회", f"{answers_b}회", answers_a != answers_b),
                    (
                        "판정 기준",
                        left["inputs"]["agreements"][0]["agreement"]["rubric"]["id"],
                        right["inputs"]["agreements"][0]["agreement"]["rubric"]["id"],
                        False,
                    ),
                    (
                        "원본 반복성",
                        _run_reference(
                            data.links.get("baseline", {}).get("repeatability", {}), "A 기록"
                        ),
                        _run_reference(
                            data.links.get("candidate", {}).get("repeatability", {}), "B 기록"
                        ),
                        False,
                    ),
                ]
            ),
        ),
        ("답변·검색 설정", _ab_table(_rows(_settings(ap), _settings(bp)))),
    ]
    for (title_a, rows_a), (title_b, rows_b) in zip(
        condition_sections(ap.get("experiment_conditions")),
        condition_sections(bp.get("experiment_conditions")),
        strict=True,
    ):
        if title_a != title_b:
            raise ValueError("A/B experiment condition categories differ")
        categories.append((title_a, _ab_table(_rows(dict(rows_a), dict(rows_b)))))
    raters = [r for r in subjects if r != "aggregate:models"]
    rater_rows = []
    for rater in raters:
        counts_a = report["grading_counts"][rater]
        counts_b = report["grading_counts"][rater]
        rater_rows.append(
            (
                _subject_markup(subjects[rater]["label"], subjects[rater]),
                f"{len(counts_a)}개 answers × {counts_a[0]}회 채점",
                f"{len(counts_b)}개 answers × {counts_b[0]}회 채점",
                counts_a != counts_b,
            )
        )
    categories.append(("평가 주체", _ab_table(rater_rows)))
    return pn.Column(
        pn.pane.Markdown("### A/B에서 달라진 조건"),
        summary,
        pn.pane.Markdown("### 전체 평가 조건"),
        condition_selector(categories),
        sizing_mode="stretch_width",
    )


def metric_guide(data):
    rubric = data.inputs["baseline"]["inputs"]["agreements"][0]["agreement"]["rubric"]
    return pn.Column(
        *rubric_guide(rubric, repeatability=True).objects,
        pn.pane.Markdown("### 조건 비교 지표"),
        _table(
            [
                (
                    "경계 충족률",
                    "같은 질문의 A/B 비율을 대응시킨 뒤 질문을 같은 비중으로 평균합니다. "
                    "유효 답변이나 판정이 없는 질문은 제외합니다.",
                ),
                (
                    "시행 간 변화폭",
                    "각 조건의 Repeatability가 저장한 같은 질문의 답변 시행 간 평균 변화폭입니다. "
                    "단위는 %p이며, 0에 가까울수록 관측된 경계 충족률이 안정적입니다.",
                ),
                (
                    "생성 실패",
                    "독립 답변 생성 시행의 전체 문항 중 실패한 건수입니다. 채점 0점과 구분합니다.",
                ),
                (
                    "답변 시간·토큰",
                    "성공한 답변의 질문별 시행 평균을 구하고, 양쪽에서 측정된 질문을 같은 비중으로 "
                    "평균합니다. 캐시 입력은 입력 토큰에 포함되며 미기록 값과 실패는 제외합니다.",
                ),
                (
                    "검색 도구 호출",
                    "성공한 답변의 질문당 키워드·의미 검색 횟수입니다. 실패한 도구 호출도 세며, "
                    "단계 기록이 총 호출 수와 다르면 미기록으로 처리합니다.",
                ),
                (
                    "표본 범위",
                    "양쪽에 유효한 질문 수와 조건별 비교 가능 시행 쌍 수를 표시합니다. "
                    "이 보고서는 유의성 검정이나 인과 해석을 하지 않습니다.",
                ),
            ]
        ),
        sizing_mode="stretch_width",
    )


def _bars(values, *, unit, title):
    """Fixed grouped bars with consistent A/B colors and stored support on hover."""
    ordered = [d for d in (*DIMENSIONS, "generation_failure") if d in values]
    dimensions = ["생성 실패율" if d == "generation_failure" else metric_label(d) for d in ordered]
    if not dimensions:
        return pn.pane.Markdown("표시할 지표가 없습니다.")
    rows = [values[d] for d in ordered]
    top = max((v for row in rows for v in (row["A"], row["B"]) if v is not None), default=0)
    upper = max(1.05 if unit == "rate" else 0.1, top * 1.22)
    source = ColumnDataSource(
        dict(
            dimension=dimensions,
            baseline=[row["A"] for row in rows],
            candidate=[row["B"] for row in rows],
            baseline_label=[
                f"{row['A'] * 100:.0f}{'%' if unit == 'rate' else '%p'}"
                if row["A"] is not None
                else "—"
                for row in rows
            ],
            candidate_label=[
                f"{row['B'] * 100:.0f}{'%' if unit == 'rate' else '%p'}"
                if row["B"] is not None
                else "—"
                for row in rows
            ],
            baseline_support=[row["A_support"] for row in rows],
            candidate_support=[row["B_support"] for row in rows],
        )
    )
    plot = figure(
        x_range=dimensions,
        y_range=(0, upper),
        width=910,
        height=350,
        toolbar_location=None,
        tools="",
        sizing_mode="stretch_width",
        title=title,
        min_border_bottom=62,
    )
    a = plot.vbar(
        x=dodge("dimension", -0.19, range=plot.x_range),
        top="baseline",
        width=0.34,
        source=source,
        color=COLORS["A"],
        legend_label="A · 기준",
    )
    b = plot.vbar(
        x=dodge("dimension", 0.19, range=plot.x_range),
        top="candidate",
        width=0.34,
        source=source,
        color=COLORS["B"],
        legend_label="B · 변경",
    )
    for offset, position, label in (
        (-0.19, "baseline", "baseline_label"),
        (0.19, "candidate", "candidate_label"),
    ):
        plot.add_layout(
            LabelSet(
                x=dodge("dimension", offset, range=plot.x_range),
                y=position,
                text=label,
                source=source,
                y_offset=5,
                text_align="center",
                text_font_size="9pt",
                text_color="#364958",
            )
        )
    for renderer, value, support, label in (
        (a, "baseline_label", "baseline_support", "A"),
        (b, "candidate_label", "candidate_support", "B"),
    ):
        plot.add_tools(
            HoverTool(
                renderers=[renderer],
                tooltips=[
                    ("조건", label),
                    ("평가 항목", "@dimension"),
                    ("값", f"@{value}_label"),
                    ("관측 범위", f"@{support}"),
                ],
            )
        )
    plot.yaxis.formatter = NumeralTickFormatter(format="0%")
    plot.yaxis.axis_label = "경계 충족률" if unit == "rate" else "변화폭 · %p"
    if unit == "point":
        plot.yaxis.formatter = CustomJSTickFormatter(code="return (tick * 100).toFixed(0) + ' %p';")
    plot.xaxis.major_label_orientation = 0.18
    plot.xaxis.major_label_text_font_size = "10pt"
    plot.ygrid.grid_line_color = "#e9eef2"
    plot.xgrid.visible = False
    plot.outline_line_color = None
    plot.background_fill_color = "#fbfcfd"
    plot.legend.orientation = "horizontal"
    plot.legend.location = "top_left"
    plot.legend.background_fill_alpha = 0
    plot.legend.border_line_alpha = 0
    return pn.pane.Bokeh(plot, sizing_mode="stretch_width")


def _pair_selector(data, subjects, maker):
    report = data.report
    boundaries = list(dict.fromkeys(row["boundary"] for row in report["quality"]))
    raters = ["aggregate:models", *sorted(report["rater_profiles"])]
    selects = (
        pn.widgets.Select(
            label="판정 경계",
            options={BOUNDARIES[b]: b for b in boundaries},
            value=boundaries[0],
            sizing_mode="stretch_width",
        ),
        pn.widgets.Select(
            label="평가 주체 선택",
            options={subjects[r]["label"]: r for r in raters},
            value="aggregate:models",
            sizing_mode="stretch_width",
            stylesheets=["select{min-height:38px;line-height:1.4;font-size:14px;}"],
        ),
    )
    views = {}
    for boundary in boundaries:
        for rater in raters:
            key = f"{boundary}|{rater}"
            views[key] = pn.Column(
                maker(boundary, rater),
                visible=(boundary == boundaries[0] and rater == "aggregate:models"),
                sizing_mode="stretch_width",
            )
    args = {f"item_{i}": view for i, view in enumerate(views.values())}
    keys = list(views)
    code = (
        "const items=[" + ",".join(f"item_{i}" for i in range(len(keys))) + "];"
        "const keys=" + repr(keys) + ";"
        "items.forEach((item,i)=>{item.visible=keys[i]===boundary.value+'|'+rater.value;});"
    )
    selects[0].jscallback(args={**args, "boundary": selects[0], "rater": selects[1]}, value=code)
    selects[1].jscallback(args={**args, "boundary": selects[0], "rater": selects[1]}, value=code)
    return pn.Column(
        pn.Row(*selects, sizing_mode="stretch_width"),
        *views.values(),
        sizing_mode="stretch_width",
    )


def quality_view(data, subjects):
    def make(boundary, rater):
        rows = {
            row["dimension"]: row
            for row in data.report["quality"]
            if row["boundary"] == boundary and row["rater"] == rater
        }
        values = {
            dimension: dict(
                A=row["baseline_mean"],
                B=row["candidate_mean"],
                A_support=f"대응 {row['paired_questions']}/{row['possible_questions']}문항",
                B_support=f"대응 {row['paired_questions']}/{row['possible_questions']}문항",
            )
            for dimension, row in rows.items()
        }
        return pn.Column(
            _bars(values, unit="rate", title="같은 질문에서 대응 가능한 경계 충족률"),
            pn.pane.Markdown(
                "양쪽에 유효한 질문만 포함합니다. 막대 높이는 질문별 비율의 평균입니다."
            ),
        )

    return _pair_selector(data, subjects, make)


def stability_view(data, subjects):
    def make(boundary, rater):
        rows = {
            row["dimension"]: row
            for row in data.report["stability"]
            if row["boundary"] == boundary and row["rater"] == rater
        }
        values = {
            dimension: dict(
                A=row["baseline"],
                B=row["candidate"],
                A_support=f"{row['baseline_pairs']}/{row['baseline_possible_pairs']} 시행 쌍",
                B_support=f"{row['candidate_pairs']}/{row['candidate_possible_pairs']} 시행 쌍",
            )
            for dimension, row in rows.items()
        }
        return pn.Column(
            _bars(values, unit="point", title="조건별 답변 시행 간 평균 변화폭"),
            pn.pane.Markdown(
                "값이 작을수록 관측된 채점 경계가 시행 사이에서 덜 흔들렸습니다. "
                "양쪽의 비교 가능 시행 쌍 수가 다르면 hover의 관측 범위를 함께 확인하세요."
            ),
        )

    return _pair_selector(data, subjects, make)


def failure_view(data):
    failures = data.report["failures"]
    a, b = failures["baseline"], failures["candidate"]
    values = {
        "generation_failure": dict(
            A=a["failed"] / a["total"] if a["total"] else None,
            B=b["failed"] / b["total"] if b["total"] else None,
            A_support=f"{a['failed']}/{a['total']} 실패",
            B_support=f"{b['failed']}/{b['total']} 실패",
        )
    }
    return pn.Column(
        _bars(values, unit="rate", title="독립 답변 생성의 실패율"),
        pn.pane.Markdown(
            "생성 실패는 답변 품질 점수의 0점이 아닙니다. "
            "실패 문항은 품질 막대그래프의 유효 문항 수에서 빠집니다."
        ),
    )


def efficiency_view(data):
    """Show matched-question resource means saved in the comparison run."""
    rows = {row["metric"]: row for row in data.report["resources"]}
    labels = {
        "qa_duration_ms": "답변 시간",
        "input_tokens": "입력 토큰",
        "cached_input_tokens": "캐시 입력 토큰",
        "output_tokens": "출력 토큰",
        "total_tokens": "전체 토큰",
        "tool_call_count": "검색 도구 전체 호출",
        "tool_calls.keyword_search": "키워드 검색 호출",
        "tool_calls.meaning_search": "의미 검색 호출",
    }
    unavailable = [labels[key] for key, row in rows.items() if not row["paired_questions"]]
    sources = {
        "A": data.inputs["baseline"]["inputs"]["agreements"],
        "B": data.inputs["candidate"]["inputs"]["agreements"],
    }

    def chart(items, title, divisor, suffix):
        available = [(metric, label, rows[metric]) for metric, label in items if metric in rows]
        if not available:
            return pn.pane.Markdown("측정된 값이 없습니다.")
        source = ColumnDataSource(
            {
                "label": [label for _, label, _ in available],
                "a": [
                    row["baseline_mean"] / divisor if row["baseline_mean"] is not None else None
                    for _, _, row in available
                ],
                "b": [
                    row["candidate_mean"] / divisor if row["candidate_mean"] is not None else None
                    for _, _, row in available
                ],
                "a_text": [
                    f"{row['baseline_mean'] / divisor:,.1f}{suffix}"
                    if row["baseline_mean"] is not None
                    else "—"
                    for _, _, row in available
                ],
                "b_text": [
                    f"{row['candidate_mean'] / divisor:,.1f}{suffix}"
                    if row["candidate_mean"] is not None
                    else "—"
                    for _, _, row in available
                ],
                "a_support": [
                    f"{row['paired_questions']}/{row['possible_questions']}문항 · "
                    f"{row['baseline_observations']}회"
                    for _, _, row in available
                ],
                "b_support": [
                    f"{row['paired_questions']}/{row['possible_questions']}문항 · "
                    f"{row['candidate_observations']}회"
                    for _, _, row in available
                ],
            }
        )
        top = max(
            (v for field in ("a", "b") for v in source.data[field] if v is not None), default=0
        )
        plot = figure(
            x_range=source.data["label"],
            y_range=(0, max(top * 1.25, 1)),
            height=320,
            toolbar_location=None,
            tools="",
            sizing_mode="stretch_width",
            title=title,
            min_border_bottom=48,
        )
        for offset, field, label, color in (
            (-0.19, "a", "A · 기준", COLORS["A"]),
            (0.19, "b", "B · 변경", COLORS["B"]),
        ):
            position = dodge("label", offset, range=plot.x_range)
            bar = plot.vbar(
                x=position, top=field, width=0.34, source=source, color=color, legend_label=label
            )
            plot.add_layout(
                LabelSet(
                    x=position,
                    y=field,
                    text=f"{field}_text",
                    source=source,
                    y_offset=5,
                    text_align="center",
                    text_font_size="9pt",
                )
            )
            plot.add_tools(
                HoverTool(
                    renderers=[bar],
                    tooltips=[
                        ("조건", label),
                        ("항목", "@label"),
                        ("평균", f"@{field}_text"),
                        ("관측 범위", f"@{field}_support"),
                    ],
                )
            )
        plot.xgrid.visible = False
        plot.ygrid.grid_line_color = "#e9eef2"
        plot.outline_line_color = None
        plot.background_fill_color = "#fbfcfd"
        plot.legend.orientation = "horizontal"
        plot.legend.location = "top_left"
        plot.legend.background_fill_alpha = 0
        plot.legend.border_line_alpha = 0
        return pn.pane.Bokeh(plot, sizing_mode="stretch_width")

    return pn.Column(
        chart([("qa_duration_ms", "답변 시간")], "성공한 답변의 평균 소요 시간", 1000, "초"),
        stack_chart(paired_answer_groups(sources, "tokens"), "tokens", "질문당 토큰 사용량"),
        stack_chart(paired_answer_groups(sources, "tools"), "tools", "질문당 검색 도구 호출"),
        pn.pane.Markdown(
            "양쪽에서 측정된 질문만 비교하며, 각 질문의 답변 시행 평균을 같은 비중으로 합산합니다. "
            "관측한 문항과 시행 수는 막대에 마우스를 올리면 확인할 수 있습니다. "
            "캐시 입력은 입력 토큰에 포함되며 —은 캐시 미기록입니다. "
            "답변 생성이 실패한 문항과 미기록 값은 "
            "제외합니다. 검색 도구 호출에는 실패한 도구 호출도 포함합니다."
        ),
        pn.pane.Markdown("공통 측정값 없음: " + ", ".join(unavailable) if unavailable else ""),
        sizing_mode="stretch_width",
    )


def question_view(data):
    records = data.inputs["baseline"]["inputs"]["agreements"][0]["bundle"]["records"]
    questions = {record["id"]: record["inputs"]["question"] for record in records}
    options = []
    for index, record_id in enumerate(data.report["record_ids"], 1):
        values = {}
        for boundary in BOUNDARIES:
            rows = {
                row["dimension"]: next(
                    (case for case in row["cases"] if case["record_id"] == record_id), None
                )
                for row in data.report["quality"]
                if row["rater"] == "aggregate:models" and row["boundary"] == boundary
            }
            if rows:
                values[BOUNDARIES[boundary]] = _bars(
                    {
                        dimension: dict(
                            A=case["baseline"],
                            B=case["candidate"],
                            A_support=f"관측 {case['baseline_answers']} 모델·시행",
                            B_support=f"관측 {case['candidate_answers']} 모델·시행",
                        )
                        for dimension, case in rows.items()
                        if case
                    },
                    unit="rate",
                    title="문항별 전체 모델 평균",
                )
        options.append(
            (
                f"{index}. {questions[record_id][:75]}"
                f"{'…' if len(questions[record_id]) > 75 else ''}",
                pn.Column(
                    pn.pane.Markdown("**질문**  " + escape(questions[record_id])),
                    _selector(list(values.items()), "판정 경계"),
                    sizing_mode="stretch_width",
                ),
            )
        )
    return _selector(options, "문항 선택")
