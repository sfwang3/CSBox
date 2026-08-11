"""Small render-only widgets used by the Review screen."""

from __future__ import annotations

from textual.widgets import Static


class ReviewTerminal(Static):
    """Terminal snapshot viewport."""


class ReviewTimeline(Static):
    """Replay progress and state panel."""


class ReviewCaptureList(Static):
    """Capture list panel."""


class ReviewFooter(Static):
    """Keyboard hints and progress footer."""


__all__ = ["ReviewCaptureList", "ReviewFooter", "ReviewTerminal", "ReviewTimeline"]
