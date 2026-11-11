"""Small presentation primitives; display recorded evaluation values without adding conclusions."""

from html import escape

import panel as pn


def report_purpose_callout(kind):
    """Explain what the report measures without interpreting a particular run."""
    messages = {
        "agreement": (
            "평가자들의 판단은 얼마나 믿을 만한가?",
            "같은 답변을 사람과 모델이 채점한 결과를 비교합니다. 서로 판단이 얼마나 비슷한지, "
            "같은 평가자가 다시 채점해도 판단이 유지되는지 확인합니다. 사람 채점이 있으면 "
            "모델의 판단이 사람과 얼마나 맞는지도 볼 수 있습니다.",
        ),
        "repeatability": (
            "다시 답해도 품질이 유지되는가?",
            "같은 설정으로 답변을 여러 번 만들었을 때 품질이 얼마나 유지되는지 봅니다. "
            "질문별 점수 수준과 답변 사이의 변화폭을 보여줍니다. "
            "채점 자체의 흔들림도 변화폭에 포함됩니다.",
        ),
        "comparison": (
            "두 실험에서 무엇이 달라졌는가?",
            "두 실험의 답변 품질과 평가 결과를 나란히 비교합니다. "
            "공통으로 비교할 수 있는 질문을 기준으로 차이를 보여주며, "
            "차이의 원인까지 단정하지는 않습니다.",
        ),
    }
    title, description = messages[kind]
    return pn.pane.HTML(
        '<section style="background:#edf5f7;border-left:4px solid #16818a;'
        'border-radius:10px;padding:16px 20px;margin:4px 0 20px;'
        'color:#24404d;font:14px/1.7 system-ui,sans-serif">'
        '<div style="display:flex;align-items:center;gap:9px;flex-wrap:wrap;margin-bottom:7px">'
        '<span style="background:#d6ecec;color:#08636b;border-radius:5px;'
        'padding:2px 8px;font-size:12px;font-weight:750;letter-spacing:.04em">'
        f'{escape(kind.upper())}</span>'
        f'<strong style="font-size:16px">{escape(title)}</strong></div>'
        f'<p style="margin:0;color:#526571">{escape(description)}</p></section>',
        sizing_mode="stretch_width",
    )


def text_block(title, value):
    """Render model output and source text literally, without executing embedded HTML."""
    return pn.pane.HTML(
        f'<div style="margin:8px 0"><strong>{escape(str(title))}</strong>'
        '<div style="white-space:pre-wrap;overflow-wrap:anywhere;line-height:1.65;'
        f'margin-top:6px">{escape(str(value if value is not None else "기록 없음"))}</div></div>',
        sizing_mode="stretch_width",
    )


def metric_card(label, value, note):
    return pn.Column(
        pn.pane.HTML(
            f'<div style="color:#526071;font-size:14px">{escape(str(label))}</div>'
            f'<div style="font-size:28px;font-weight:650;margin:8px 0">{escape(str(value))}</div>'
            '<div style="color:#526071;font-size:13px;overflow-wrap:anywhere">'
            f"{escape(str(note))}</div>"
        ),
        styles={"background": "#f0f5f9", "border-radius": "12px", "padding": "16px"},
        width=240,
        min_width=210,
    )


def case_view(bundle, record_id, *, title, collapsed=False):
    """Show the frozen answer and selected context; avoid wide tables for reading prose."""
    record = next(r for r in bundle["records"] if r["id"] == record_id)
    case = next(c for c in bundle["cases"] if c["record_id"] == record_id)
    result = case.get("result") or {}
    evidence = result.get("generation_sources") or []
    source_views = []
    for i, source in enumerate(evidence, 1):
        metadata = " · ".join(str(source[k]) for k in ("title", "url") if source.get(k))
        content = str(source.get("text", source))
        source_views.append(
            text_block(f"근거 {i}", f"{metadata}\n{content}" if metadata else content)
        )
    return pn.Card(
        content_card("💬 질문", record["inputs"]["question"], accent="#2563eb"),
        content_card("✦ 답변", result.get("answer"), markdown=True, accent="#7c3aed"),
        *([text_block("생성 오류", case["error"])] if case.get("error") else []),
        pn.Accordion(
            ("답변 생성에 전달한 근거", pn.Column(*source_views)),
            active=[],
            sizing_mode="stretch_width",
        )
        if evidence
        else pn.pane.Markdown("답변 작성에 제공된 자료가 기록되어 있지 않습니다."),
        title=title,
        collapsed=collapsed,
        sizing_mode="stretch_width",
    )


METRICS = {
    "correctness": ("정확성", "🎯", "#2563eb"),
    "completeness": ("완전성", "🧩", "#0f766e"),
    "groundedness": ("근거 충실도", "🔗", "#7c3aed"),
    "relevance": ("질문 적합성", "💬", "#b45309"),
    "behavior": ("행동 준수", "🛡️", "#475569"),
}


def metric_label(dimension):
    title, icon, _ = METRICS[dimension]
    return f"{icon} {title}"


def metric_badge(dimension):
    title, icon, color = METRICS[dimension]
    return f'<span style="color:{color};font-weight:650;white-space:nowrap">{icon} {title}</span>'


def model_name(identity):
    return {
        "gpt-6-luna": "ChatGPT 6 Luna",
        "gpt-6-terra": "ChatGPT 6 Terra",
        "gpt-6.1-terra": "ChatGPT 6.1 Terra",
        "claude-haiku-5-5": "Claude Haiku 5.5",
        "gemini-3.5-flash-lite": "Gemini 3.5 Flash Lite",
    }.get(identity, identity)


PROSE_STYLE = """
.report-prose { color:#253449; font:14px/1.85 system-ui,sans-serif; overflow-wrap:anywhere; }
.report-prose :first-child { margin-top:0; }
.report-prose :last-child { margin-bottom:0; }
.report-prose h1,.report-prose h2,.report-prose h3 { font-size:17px; line-height:1.5;
    margin:20px 0 8px; }
.report-prose h4 { font-size:15px; }
.report-prose p { margin:8px 0; }
.report-prose a { color:#2563eb; text-underline-offset:3px; }
.report-prose code { background:#edf2f7; border-radius:4px; padding:2px 5px; font-size:12px; }
.report-prose pre { background:#f1f5f9; padding:14px; border-radius:10px; overflow:auto; }
.report-prose pre code { padding:0; background:none; }
.report-prose table { border-collapse:collapse; font-size:13px; width:100%; }
.report-prose th,.report-prose td { padding:8px 12px; border:1px solid #e2e8f0; text-align:left; }
.report-prose th { background:#f1f5f9; }
.report-prose blockquote { border-left:3px solid #a5b4fc; margin:12px 0; padding-left:14px;
    color:#64748b; }
.report-prose li { margin:4px 0; }
"""


def markdown_markup(value):
    """Render stored Markdown with raw HTML and remote images disabled."""
    from markdown_it import MarkdownIt

    parser = MarkdownIt("commonmark", {"html": False, "breaks": True}).enable("table")
    parser.disable("image")
    return '<div class="report-prose">' + parser.render(str(value or "기록 없음")) + "</div>"


def content_card(title, value, *, markdown=False, accent="#64748b"):
    body = (
        markdown_markup(value)
        if markdown
        else (
            '<div class="report-prose" style="white-space:pre-wrap">'
            + escape(str(value if value is not None else "기록 없음"))
            + "</div>"
        )
    )
    return pn.Column(
        pn.pane.HTML(
            f'<div style="font:650 12px system-ui;color:{accent};margin-bottom:12px">'
            + escape(title)
            + "</div>"
            + body,
            stylesheets=[PROSE_STYLE],
            sizing_mode="stretch_width",
            margin=0,
        ),
        styles={
            "background": "#ffffff",
            "border": "1px solid #e2e8f0",
            "border-left": f"3px solid {accent}",
            "border-radius": "14px",
            "padding": "18px 22px",
            "box-shadow": "0 3px 14px #17255405",
        },
        margin=(6, 0),
        sizing_mode="stretch_width",
    )
