"""Textual screens."""

from csbox.tui.screens.evidence import EvidenceSetEditorScreen, EvidenceSetsScreen
from csbox.tui.screens.evidence_sources import EvidenceCaptureBrowserScreen
from csbox.tui.screens.home import HomeScreen
from csbox.tui.screens.project_check import CheckScreen, ProjectCheckScreen
from csbox.tui.screens.records import RecordsScreen
from csbox.tui.screens.report import ReportExportResultScreen
from csbox.tui.screens.review import ReviewController, ReviewScreen

__all__ = [
    "CheckScreen",
    "EvidenceCaptureBrowserScreen",
    "EvidenceSetEditorScreen",
    "EvidenceSetsScreen",
    "HomeScreen",
    "ProjectCheckScreen",
    "ReportExportResultScreen",
    "RecordsScreen",
    "ReviewController",
    "ReviewScreen",
]
