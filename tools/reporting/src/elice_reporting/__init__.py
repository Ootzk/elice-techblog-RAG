"""Small reporting helpers; report-specific input and plot configuration live in notebooks."""

from .data import (
    ReportData,
    agreement_frames,
    comparison_frames,
    demo,
    find_root,
    from_files,
    from_mlflow,
)
from .export import export_html, interactive_table
from .presentation import case_view, metric_card, report_purpose_callout, text_block

__all__ = [
    "ReportData",
    "agreement_frames",
    "comparison_frames",
    "case_view",
    "metric_card",
    "report_purpose_callout",
    "text_block",
    "demo",
    "export_html",
    "find_root",
    "from_files",
    "from_mlflow",
    "interactive_table",
]
