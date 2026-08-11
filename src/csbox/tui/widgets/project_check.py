from __future__ import annotations

from textual.widgets import Static


class CheckSummary(Static):
    """Project and aggregate check status."""


class CheckFindings(Static):
    """Render-only finding list."""


class CheckFooter(Static):
    """Check screen keyboard hints."""


__all__ = ["CheckFindings", "CheckFooter", "CheckSummary"]
