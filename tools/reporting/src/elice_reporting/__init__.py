"""Small reporting helpers; report-specific input and plot configuration live in notebooks."""

from .data import (
    ReportData,
    analysis_frames,
    comparison_frames,
    demo,
    find_root,
    from_files,
    from_mlflow,
)
from .export import export_html, interactive_table
from .presentation import case_view, metric_card, text_block

__all__ = [
    "ReportData",
    "analysis_frames",
    "comparison_frames",
    "case_view",
    "metric_card",
    "text_block",
    "demo",
    "export_html",
    "find_root",
    "from_files",
    "from_mlflow",
    "interactive_table",
]
