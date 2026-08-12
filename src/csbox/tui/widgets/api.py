"""Render-only widgets for the API evidence workflow."""

from __future__ import annotations

from collections.abc import Iterable

from textual.widgets import Button, Static


class ApiListPanel(Static):
    """A text summary with keyboard-focusable rows mounted below it."""

    def __init__(self, *, empty_text: str, **kwargs: object) -> None:
        super().__init__(empty_text, markup=False, **kwargs)
        self.empty_text = empty_text

    async def set_rows(self, summary: str, rows: Iterable[Button]) -> None:
        self.update(summary)
        await self.remove_children(Button)
        buttons = tuple(rows)
        if buttons:
            await self.mount(*buttons)


class ApiScenarioList(ApiListPanel):
    """Scenario names and safe configuration errors."""


class ApiRunList(ApiListPanel):
    """Persisted run summaries only; never raw request or response data."""


class ApiDetail(Static):
    """Wrapped request, response and assertion detail."""

    can_focus = True


class ApiFooter(Static):
    """Visible keyboard hints for the API screen."""


__all__ = ["ApiDetail", "ApiFooter", "ApiRunList", "ApiScenarioList"]
