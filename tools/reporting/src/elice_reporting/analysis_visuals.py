"""Icon legends and heatmaps of frozen pairwise coefficients and per-item labels."""

import base64
from html import escape
from importlib.resources import files
from urllib.parse import urlsplit
from xml.etree import ElementTree as ET

import holoviews as hv
import pandas as pd
import panel as pn
from bokeh.models import ColorBar, FixedTicker, HoverTool, LinearColorMapper
from bokeh.palettes import RdBu11, interp_palette

KAPPA_PALETTE = tuple(interp_palette(RdBu11[::-1], 256))
KAPPA_LIMITS = (-1, 1)
KAPPA_MISSING = "#e2e8f0"
MATRIX_MIN_SIZE = 300
MATRIX_MARGIN = 8
MATRIX_TITLE_HEIGHT = 24

DIMENSIONS = ("correctness", "completeness", "groundedness", "relevance")
STATES = {
    "0": ("#d6604d", "0"),
    "1": ("#c59635", "1"),
    "2": ("#2166ac", "2"),
    "not_applicable": ("#cbd5e1", "—"),
    "error": ("#9385ba", "!"),
    "missing": ("#f7f9fb", "·"),
}


def subject_key(subject):
    return (
        subject.get("rater", subject.get("rater_id")),
        str(subject["round"]),
        subject.get("profile_digest", subject.get("rater_profile_digest", "")),
    )


def _icon(rater, color, badge, type_code="", model_names=()):
    name = (" ".join(model_names) if model_names else rater).lower()
    provider = (
        next(
            (
                brand
                for brand, words in (
                    ("claude-color", ("claude", "anthropic")),
                    ("gemini-color", ("gemini",)),
                    ("openai", ("openai", "chatgpt", "gpt")),
                )
                if any(word in name for word in words)
            ),
            None,
        )
        if rater.startswith("model:")
        else None
    )
    if provider:
        source = files("elice_reporting").joinpath("assets", provider + ".svg").read_text()
        root = ET.fromstring(source)
        root.set("x", "3")
        root.set("y", "3")
        root.set("width", "25")
        root.set("height", "25")
        glyph = ET.tostring(root, encoding="unicode")
    elif rater.startswith("human:"):
        glyph = (
            f'<g fill="{color}"><circle cx="15" cy="10" r="6"/>'
            '<path d="M4 29v-5a11 11 0 0 1 22 0v5z"/></g>'
        )
    else:
        glyph = '<rect x="4" y="5" width="24" height="24" rx="7" fill="#64748b"/>'
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="36" height="36" viewBox="0 0 36 36">'
        f"{glyph}"
        + (
            f'<rect x="0" y="0" width="13" height="13" rx="4" fill="{color}"/>'
            f'<text x="6.5" y="10" text-anchor="middle" fill="white" '
            f'font-family="sans-serif" font-size="9">{type_code}</text>'
            if type_code
            else ""
        )
        + '<circle cx="29" cy="29" r="7" fill="#172536" stroke="white"/>'
        f'<text x="29" y="32.2" text-anchor="middle" fill="white" '
        f'font-family="sans-serif" font-size="9">{badge}</text></svg>'
    )
    return "data:image/svg+xml;base64," + base64.b64encode(svg.encode()).decode()


def analysis_subjects(data):
    """One visual identity per rater, round and profile; preserve manifest order."""
    subjects = []
    seen = set()
    human_colors = {}
    palette = ("#49748c", "#9270a3", "#498e8a", "#bb7c62", "#a49456", "#6c8c60")
    candidates = list(data.inputs["gradings"])
    candidates += [s for row in data.report["comparisons"] for s in (row["left"], row["right"])]
    for candidate in candidates:
        key = subject_key(candidate)
        if key in seen:
            continue
        seen.add(key)
        rater, round_, profile = key
        if rater.startswith("human:") and rater not in human_colors:
            index = len(human_colors)
            human_colors[rater] = (
                palette[index] if index < len(palette) else f"hsl({index * 137.5 % 360},65%,45%)"
            )
        color = human_colors.get(rater, "#64748b")
        saved_profile = data.inputs.get("profiles", {}).get(profile, {})
        models = list(
            dict.fromkeys(
                spec["model"] for spec in saved_profile.get("scorers", []) if spec.get("model")
            )
        )
        links = [
            link
            for link in data.links.get("gradings", [])
            if (link.get("rater"), str(link.get("round")), link.get("profile_digest")) == key
        ]
        if len(links) > 1:
            raise ValueError("Multiple grading links for the same subject and round")
        link = links[0] if links else {}
        observed = sorted(
            {model for entry in link.get("model_observations", []) for model in entry["models"]}
        )
        actual = observed or [m for m in models if not m.startswith("gateway:/")]
        display_names = {
            "gpt-6-luna": "ChatGPT 6 Luna",
            "gpt-6-terra": "ChatGPT 6 Terra",
            "claude-haiku-5-5": "Claude Haiku 5.5",
            "gemini-3.5-flash-lite": "Gemini 3.5 Flash Lite",
        }
        label = " / ".join(display_names.get(m, m) for m in actual) or rater
        subjects.append(
            dict(
                key=key,
                rater=rater,
                round=round_,
                profile=profile,
                icon=_icon(rater, color, round_),
                color=color,
                models=models,
                actual_models=actual,
                model_source="직접 검수"
                if rater.startswith("human:")
                else "실행 trace 관측 (최대 3개)"
                if observed
                else "채점 프로필"
                if actual
                else "미기록",
                grading_link=link,
                label=f"{label} · {round_}회차 · {profile[:6]}",
            )
        )
    model_types = sorted(
        {
            tuple(s["actual_models"] or s["models"] or [s["rater"]])
            for s in subjects
            if s["rater"].startswith("model:")
        }
    )
    for subject in subjects:
        identity = tuple(subject["actual_models"] or subject["models"] or [subject["rater"]])
        index = model_types.index(identity) if subject["rater"].startswith("model:") else -1
        type_code = "" if index < 0 else chr(65 + index) if index < 26 else f"M{index + 1}"
        subject["type_code"] = type_code
        subject["icon"] = _icon(
            subject["rater"],
            subject["color"],
            subject["round"],
            type_code,
            subject["actual_models"],
        )
    return subjects


def rater_legend(subjects):
    rows = []
    for subject in subjects:
        model = " / ".join(subject.get("actual_models", []))
        if subject["rater"].startswith("model:"):
            model = model or "실제 모델 ID 미기록"
        else:
            model = "—"
        link = subject.get("grading_link", {})
        url = link.get("url")
        reference = (
            f'<a href="{escape(url, quote=True)}" target="_blank" rel="noopener noreferrer" '
            f'title="{escape(link["run_id"], quote=True)}">원본 grading ↗</a>'
            if url and urlsplit(url).scheme in {"http", "https"}
            else "Run 링크 미기록"
        )
        names = subject["label"].rsplit(" · ", 2)[0]
        model_display = (
            escape(names) + "<br><small>" + escape(model) + "</small>"
            if subject["rater"].startswith("model:")
            else "—"
        )
        aliases = " / ".join(subject.get("models", []))
        if aliases:
            model_display += "<br><small>" + escape(aliases) + "</small>"
        rows.append(
            "<tr>"
            f'<td><img src="{subject["icon"]}" width="32" height="32" alt="평가 주체"></td>'
            f"<td>{escape(subject['rater'])}</td><td>{escape(subject['round'])}회차</td>"
            f"<td>{escape(subject.get('type_code', '')) or '사람'}</td>"
            f"<td>{model_display}<br><small>{escape(subject['model_source'])}</small></td>"
            f'<td title="{escape(subject["profile"], quote=True)}">'
            f"{escape(subject['profile'][:12])}</td>"
            f"<td>{reference}</td></tr>"
        )
    return pn.pane.HTML(
        '<p style="font:12px system-ui;color:#64748b">로고: 모델 계열 · 왼쪽 위 문자: 모델 유형 · '
        "오른쪽 아래 숫자: 회차 · 사람 아이콘 색: 검수자</p>"
        '<table style="width:100%;table-layout:fixed;border-collapse:collapse;font:13px system-ui">'
        "<colgroup>"
        + "".join(f'<col style="width:{width}%">' for width in (5, 18, 7, 8, 36, 10, 16))
        + "</colgroup>"
        "<thead><tr>"
        + "".join(
            f"<th>{title}</th>"
            for title in (
                "아이콘",
                "평가 주체",
                "회차",
                "모델 유형",
                "관측 모델",
                "프로필",
                "원본 grading",
            )
        )
        + "</tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table>",
        stylesheets=[
            "th,td{text-align:left;vertical-align:top;padding:9px;border-bottom:1px solid #e4e9ef;"
            "overflow-wrap:anywhere;}th{color:#526071;}"
        ],
        sizing_mode="stretch_width",
    )


def kappa_color(value):
    """Use the same ordered bins as the displayed scale: coral -1, neutral 0, blue +1."""
    if value is None or pd.isna(value):
        return KAPPA_MISSING
    low, high = KAPPA_LIMITS
    index = int((value - low) / (high - low) * len(KAPPA_PALETTE))
    return KAPPA_PALETTE[min(len(KAPPA_PALETTE) - 1, max(0, index))]


def matrix_size(subjects):
    return max(MATRIX_MIN_SIZE, int((len(subjects) + 1.5) * 36))


def matrix_group_size(subjects):
    size = matrix_size(subjects)
    return (2 * (size + 2 * MATRIX_MARGIN), 2 * (size + MATRIX_TITLE_HEIGHT + 2 * MATRIX_MARGIN))


def kappa_colorbar(height=680):
    def decorate(plot, element):
        figure = plot.state
        figure.renderers = []
        figure.axis.visible = False
        figure.grid.visible = False
        figure.outline_line_color = None
        figure.tools = []
        mapper = LinearColorMapper(palette=KAPPA_PALETTE, low=-1, high=1, nan_color=KAPPA_MISSING)
        figure.add_layout(
            ColorBar(
                color_mapper=mapper,
                ticker=FixedTicker(ticks=[-1, -0.5, 0, 0.5, 1]),
                orientation="vertical",
                height=height - 64,
                width=14,
                title="선형 가중 Cohen κ",
                major_label_text_font_size="11px",
                title_text_font_size="12px",
                major_tick_line_color=None,
                bar_line_color=None,
                border_line_color=None,
                padding=8,
                label_standoff=8,
            ),
            "right",
        )

    return hv.Curve([]).opts(
        width=100,
        height=height,
        xlim=(0, 1),
        ylim=(0, 1),
        hooks=[decorate],
        default_tools=[],
        tools=[],
        toolbar=None,
        xaxis=None,
        yaxis=None,
        shared_axes=False,
    )


def kappa_cells(data, subjects, dimension):
    stored = {}
    for row in data.report["comparisons"]:
        if row["dimension"] != dimension:
            continue
        a, b = subject_key(row["left"]), subject_key(row["right"])
        for key in ((a, b), (b, a)):
            if key in stored:
                raise ValueError("Duplicate coefficient for the same rater rounds and dimension")
            stored[key] = row
    cells = []
    size = len(subjects)
    for y, a in enumerate(subjects):
        for x, b in enumerate(subjects):
            if x <= y:
                continue
            row = stored.get((a["key"], b["key"]))
            value = row["cohen_kappa"] if row else None
            fill = kappa_color(value)
            cells.append(
                dict(
                    x=x,
                    y=size - 1 - y,
                    value=value,
                    fill=fill,
                    ink="#fff" if value is not None and abs(value) >= 0.8 else "#192b3e",
                    display=f"{value:.2f}" if value is not None else "—" if x == y else "·",
                    left=f"{a['rater']} · {a['round']}회차",
                    right=f"{b['rater']} · {b['round']}회차",
                    reason=(row["kappa_undefined_reason"] or "")
                    if row
                    else "동일 평가 회차"
                    if x == y
                    else "미기록",
                    valid=row["valid_pairs"] if row else None,
                    excluded=row["excluded_pairs"] if row else None,
                )
            )
    return pd.DataFrame(
        cells,
        columns=[
            "x",
            "y",
            "value",
            "fill",
            "ink",
            "display",
            "left",
            "right",
            "reason",
            "valid",
            "excluded",
        ],
    )


def grading_cells(data, subjects, record_id, dimensions=DIMENSIONS):
    cells = []
    gradings = {subject_key(g): g for g in data.inputs["gradings"]}
    for x, subject in enumerate(subjects):
        grading = gradings.get(subject["key"], {})
        ratings = {(r["record_id"], r["dimension"]): r for r in grading.get("ratings", [])}
        rubric = grading.get("rubric", data.report["rubric"])
        for y, dimension in enumerate(dimensions):
            rating = ratings.get((record_id, dimension), {})
            status, score = rating.get("status", "missing"), rating.get("score")
            state = str(score) if status == "scored" else status
            fill, symbol = STATES[state]
            if rubric["kind"] == "iron" and score == 1:
                fill = STATES["2"][0]
            display = (
                f"{score} · {rubric['scale_labels'][str(score)]}"
                if status == "scored"
                else {"not_applicable": "N/A", "error": "채점 오류", "missing": "미기록"}[status]
            )
            cells.append(
                dict(
                    x=x,
                    y=len(dimensions) - 1 - y,
                    fill=fill,
                    ink="#fff" if status == "scored" else "#192b3e",
                    display=symbol,
                    rater=subject["rater"],
                    round=subject["round"],
                    dimension=dimension,
                    label=display,
                    status=status,
                    score=score,
                    score_display=display,
                    rationale=rating.get("rationale", ""),
                )
            )
    return pd.DataFrame(cells)


def _grid(cells, subjects, *, column_subjects=None, dimensions=None, tooltips):
    """HoloViews creates fresh Bokeh models for notebook display and HTML export."""
    nrows = len(subjects) if column_subjects is not None else len(dimensions)
    ncols = len(subjects)
    column_icons = column_subjects if column_subjects is not None else subjects
    frame = cells.assign(x0=cells.x - 0.43, y0=cells.y - 0.43, x1=cells.x + 0.43, y1=cells.y + 0.43)

    def decorate(plot, element):
        figure = plot.state
        figure.axis.visible = False
        figure.grid.visible = False
        figure.outline_line_color = None
        figure.tools = [tool for tool in figure.tools if isinstance(tool, HoverTool)]
        figure.toolbar.active_drag = None
        figure.toolbar.active_scroll = None
        figure.toolbar.active_tap = None
        for tool in figure.tools:
            if isinstance(tool, HoverTool):
                tool.tooltips = tooltips
                tool.renderers = [plot.handles["glyph_renderer"]]
        if column_subjects is not None:
            figure.image_url(
                url=[s["icon"] for s in subjects],
                x=[-0.92] * nrows,
                y=list(reversed(range(nrows))),
                w=28,
                h=28,
                w_units="screen",
                h_units="screen",
                anchor="center",
            )
        else:
            figure.text(
                x=[-0.65] * nrows,
                y=list(reversed(range(nrows))),
                text=list(dimensions),
                text_font_size="12px",
                text_baseline="middle",
                text_align="right",
                text_color="#526071",
            )
        figure.image_url(
            url=[s["icon"] for s in column_icons],
            x=list(range(ncols)),
            y=[nrows - 0.08] * ncols,
            w=28,
            h=28,
            w_units="screen",
            h_units="screen",
            anchor="center",
        )

    width = height = matrix_size(subjects)
    # Keep row labels readable without scaling their left gutter with the entire canvas.
    left = (
        -1.5
        if column_subjects is not None
        else -(0.65 * width + 105 * (ncols - 0.25)) / (width - 105)
    )
    bounds = dict(
        xlim=(left, ncols - 0.25),
        ylim=(-0.65, nrows + 0.65),
        width=width,
        height=height,
    )

    rectangles = hv.Rectangles(
        frame,
        kdims=["x0", "y0", "x1", "y1"],
        vdims=[c for c in cells.columns if c not in {"x", "y"}],
    ).opts(
        color=hv.dim("fill"), line_color="white", line_width=2, hooks=[decorate], tools=["hover"]
    )
    labels = hv.Labels(cells, kdims=["x", "y"], vdims=["display", "ink"]).opts(
        text_font_size="11px" if column_subjects is not None else "14px",
        text_color=hv.dim("ink"),
        text_font_style="bold",
        tools=[],
    )
    return (rectangles * labels).opts(
        **bounds,
        toolbar=None,
        default_tools=[],
        xaxis=None,
        yaxis=None,
        shared_axes=False,
        bgcolor="white",
        show_title=False,
    )


def kappa_heatmaps(data, subjects):
    if not subjects:
        return pn.pane.Markdown("기록된 평가 주체가 없습니다.")
    dimensions = DIMENSIONS if data.inputs["bundle"]["kind"] == "gold" else ("behavior",)
    maps = []
    for dimension in dimensions:
        cells = kappa_cells(data, subjects, dimension)
        heatmap = _grid(
            cells,
            subjects,
            column_subjects=subjects,
            tooltips=[
                ("선형 가중 Cohen κ", "@value"),
                ("유효", "@valid"),
                ("제외", "@excluded"),
                ("사유", "@reason"),
            ],
        )
        size = matrix_size(subjects)
        maps.append(
            pn.Column(
                pn.pane.Markdown(f"**{dimension}**", height=MATRIX_TITLE_HEIGHT, margin=0),
                pn.pane.HoloViews(heatmap, margin=0),
                width=size,
                height=size + MATRIX_TITLE_HEIGHT,
                margin=MATRIX_MARGIN,
            )
        )
    width, height = matrix_group_size(subjects)
    return pn.Column(
        pn.Row(
            pn.GridBox(*maps, ncols=2, width=width, height=height, margin=0),
            pn.pane.HoloViews(kappa_colorbar(height), margin=0),
            margin=0,
        ),
        pn.pane.Markdown("회색: 미정의·미기록 · 대각선과 하삼각은 생략", margin=(4, 8)),
    )


def agreement_browser(data, subjects):
    """Keep rounds separate and expose same-protocol repetitions without pooling votes."""
    grouped = {}
    for subject in subjects:
        grouped.setdefault((subject["rater"], subject["profile"]), []).append(subject)
    repeated = [group for group in grouped.values() if len(group) > 1]
    full = kappa_heatmaps(data, subjects)
    if not repeated:
        return pn.Column(
            full,
            pn.pane.Markdown(
                "회차별 판정과 κ는 평균내지 않습니다. 같은 평가자·같은 프로필의 여러 회차가 "
                "포함되면 반복 일관성 뷰가 추가됩니다."
            ),
        )
    options = {"전체 평가 주체 · 회차별 비교": "0"}
    views = [pn.Column(full)]
    for i, group in enumerate(repeated, 1):
        options[f"반복 일관성 · {group[0]['label'].rsplit(' · ', 2)[0]}"] = str(i)
        pairs = [
            row
            for row in data.report["comparisons"]
            if subject_key(row["left"]) in {s["key"] for s in group}
            and subject_key(row["right"]) in {s["key"] for s in group}
        ]
        rows = "".join(
            "<tr>"
            + "".join(
                f"<td>{escape(str(value))}</td>"
                for value in (
                    row["dimension"],
                    row["left"]["round"],
                    row["right"]["round"],
                    row["valid_pairs"],
                    row["excluded_pairs"],
                    row["agreement"] if row["agreement"] is not None else "미정의",
                    row["cohen_kappa"] if row["cohen_kappa"] is not None else "미정의",
                    row["kappa_undefined_reason"] or "—",
                )
            )
            + "</tr>"
            for row in pairs
        )
        table = pn.pane.HTML(
            '<table style="width:100%;font:13px system-ui"><thead><tr>'
            + "".join(
                f"<th>{name}</th>"
                for name in (
                    "항목",
                    "회차 A",
                    "회차 B",
                    "유효",
                    "제외",
                    "일치율",
                    "선형 가중 Cohen κ",
                    "사유",
                )
            )
            + "</tr></thead><tbody>"
            + rows
            + "</tbody></table>",
            stylesheets=["th,td{text-align:left;padding:8px;border-bottom:1px solid #e4e9ef;}"],
        )
        views.append(pn.Column(kappa_heatmaps(data, group), table, visible=False))
    selector = pn.widgets.Select(label="일치도 보기", options=options, value="0")
    selector.jscallback(
        args={f"view_{i}": view for i, view in enumerate(views)},
        value="const views=["
        + ",".join(f"view_{i}" for i in range(len(views)))
        + "];views.forEach((view,i)=>{view.visible=i===Number(cb_obj.value);});",
    )
    return pn.Column(
        selector,
        *views,
        pn.pane.Markdown(
            "반복 일관성은 동일 평가자·동일 프로필 회차끼리만 비교합니다. "
            "프로필 변경은 평가 조건 변경이며 반복 실행으로 합치지 않습니다. "
            "회차별 판정·κ를 평균내거나 회차를 독립 평가자로 세어 종합 일치도를 만들지 않습니다."
        ),
    )


def grading_heatmap(data, subjects, record_id):
    if not subjects:
        return pn.pane.Markdown("기록된 평가 주체가 없습니다.")
    dimensions = DIMENSIONS if data.inputs["bundle"]["kind"] == "gold" else ("behavior",)
    return _grid(
        grading_cells(data, subjects, record_id, dimensions),
        subjects,
        dimensions=dimensions,
        tooltips=[
            ("판정", "@label"),
            ("원점수", "@score_display"),
            ("평가자", "@rater"),
            ("회차", "@round"),
            ("항목", "@dimension"),
        ],
    )


def grading_legend(rubric=None):
    names = {
        "0": "0 · 부족",
        "1": "1 · 부분 충족",
        "2": "2 · 충분",
        "not_applicable": "N/A",
        "error": "채점 오류",
        "missing": "미기록",
    }
    if rubric and rubric["kind"] == "iron":
        names.update({"0": "0 · 위반", "1": "1 · 준수"})
    return pn.pane.HTML(
        '<div style="display:flex;gap:18px;flex-wrap:wrap;font:12px system-ui">'
        + "".join(
            f'<span><i style="display:inline-block;width:14px;height:14px;border-radius:3px;'
            f"background:{color};border:1px solid #d4dce5;"
            'vertical-align:middle;margin-right:5px"></i>'
            f"{escape(names.get(state, state))} ({symbol})</span>"
            for state, (color, symbol) in STATES.items()
            if not (rubric and rubric["kind"] == "iron" and state == "2")
        )
        + "</div>"
    )
