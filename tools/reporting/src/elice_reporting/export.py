"""Offline HTML export and browser-side table controls, independent of notebook kernels."""

import json
from html import escape
from pathlib import Path

import pandas as pd
import panel as pn
from bokeh.resources import INLINE
from jinja2 import Template

DOCUMENT = Template("""<!doctype html>
<html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{{ title | e }}</title>{{ bokeh_css }}{{ bokeh_js }}
<style>body { margin: 0; padding:36px 24px 64px; background: #f4f7fb; color: #182533;
font-family: system-ui, sans-serif; } pre { white-space: pre-wrap; overflow-wrap: anywhere; }
@media (max-width:700px) { body { padding:16px 10px; } }
@media print { body { margin: 0; padding:0; background: white; } }</style>
</head><body>{{ plot_div }}{{ plot_script }}</body></html>""")


TABLE_CALLBACK = """
const escape = value => String(value ?? '').replace(/[&<>"']/g,
    char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
const query = search.value.toLocaleLowerCase();
const filtered = rows.filter(row => row.some(value =>
    String(value ?? '').toLocaleLowerCase().includes(query)));
const index = columns.indexOf(sort.value);
if (index >= 0) {
    filtered.sort((left, right) => {
        const a = left[index], b = right[index];
        if (a == null) return b == null ? 0 : 1;
        if (b == null) return -1;
        const result = typeof a === 'number' && typeof b === 'number'
            ? a - b : String(a).localeCompare(String(b), 'ko', {numeric:true});
        return descending.active ? -result : result;
    });
}
const header = columns.map(name => '<th>' + escape(name) + '</th>').join('');
const body = filtered.map(row => '<tr>' + row.map(value =>
    '<td>' + escape(value) + '</td>').join('') + '</tr>').join('');
const markup = '<p>' + filtered.length + ' / ' + rows.length + '행</p>'
    + '<table><thead><tr>' + header + '</tr></thead><tbody>' + body + '</tbody></table>';
// Panel's HTML model stores escaped markup and decodes it when rendering.
table.text = escape(markup);
"""

TABLE_STYLE = """
table { border-collapse:collapse; width:100%; font:13px system-ui,sans-serif; }
th,td { text-align:left; vertical-align:top; border-bottom:1px solid #dce3ea;
        padding:10px; min-width:90px; max-width:480px; overflow-wrap:anywhere; }
th { position:sticky; top:0; background:#eef3f7; }
td { white-space:pre-wrap; }
p { font:13px system-ui,sans-serif; color:#526071; }
"""


def interactive_table(frame, *, height=360):
    """Searchable/sortable HTML, safe to render inside initially collapsed sections.

    Avoid SlickGrid DataTable: its layout hook can run before grid initialization
    in a collapsed Panel Card and abort rendering the entire document.
    """
    columns = [str(name) for name in frame.columns]
    rows = [
        [
            json.dumps(value, ensure_ascii=False)
            if isinstance(value, (dict, list, tuple))
            else None
            if pd.isna(value)
            else value
            for value in row
        ]
        for row in frame.itertuples(index=False, name=None)
    ]
    header = "".join(f"<th>{escape(name)}</th>" for name in columns)
    body = "".join(
        "<tr>"
        + "".join(f"<td>{escape(str(v)) if v is not None else ''}</td>" for v in row)
        + "</tr>"
        for row in rows
    )
    table = pn.pane.HTML(
        f"<p>{len(rows)} / {len(rows)}행</p>"
        f"<table><thead><tr>{header}</tr></thead><tbody>{body}</tbody></table>",
        height=height,
        sizing_mode="stretch_width",
        styles={"overflow": "auto"},
        stylesheets=[TABLE_STYLE],
    )
    search = pn.widgets.TextInput(label="표 검색", placeholder="질문·평가자·문항 ID 등")
    sort = pn.widgets.Select(label="정렬 기준", options=["원본 순서", *columns])
    descending = pn.widgets.Checkbox(label="내림차순", value=False)
    for widget in (search, sort, descending):
        widget.jscallback(
            args={
                "table": table,
                "search": search,
                "sort": sort,
                "descending": descending,
                "columns": columns,
                "rows": rows,
            },
            value=TABLE_CALLBACK,
        )
    return pn.Column(search, pn.Row(sort, descending), table, sizing_mode="stretch_width")


def export_html(view, output, *, title="Elice evaluation report", overwrite=False):
    """Export the explicitly authored view, not arbitrary Markdown cells in the notebook."""
    output = Path(output)
    if output.suffix.lower() != ".html":
        raise ValueError("Use an .html output path")
    if output.exists() and not overwrite:
        raise FileExistsError("Existing report is preserved; select a new filename")
    output.parent.mkdir(parents=True, exist_ok=True)
    pn.panel(view).save(str(output), title=title, resources=INLINE, embed=False, template=DOCUMENT)
    return output
