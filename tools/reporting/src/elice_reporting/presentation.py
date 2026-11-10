"""Small presentation primitives; display recorded evaluation values without adding conclusions."""

from html import escape

import panel as pn


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
        text_block("질문", record["inputs"]["question"]),
        text_block("답변", result.get("answer")),
        *([text_block("생성 오류", case["error"])] if case.get("error") else []),
        pn.Accordion(
            ("답변 생성에 전달한 근거", pn.Column(*source_views)),
            active=[],
            sizing_mode="stretch_width",
        )
        if evidence
        else pn.pane.Markdown("생성 문맥이 기록되어 있지 않습니다."),
        title=title,
        collapsed=collapsed,
        sizing_mode="stretch_width",
    )
