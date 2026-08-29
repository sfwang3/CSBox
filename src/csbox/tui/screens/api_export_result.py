"""Exact, API-specific export result presentation."""

from __future__ import annotations

from pathlib import Path

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical
from textual.events import Resize
from textual.screen import Screen
from textual.widgets import Button, Static

from csbox.api.exporter import ApiExportResult
from csbox.api.models import ApiRun
from csbox.core.text_layout import wrap_cells
from csbox.locales import Translator


class ApiExportResultScreen(Screen[None]):
    """Show the complete destination and only exporter-reported artifacts."""

    BINDINGS = (
        Binding("enter", "go_back", "返回", show=False, priority=True),
        Binding("escape", "go_back", "返回", show=False),
        Binding("q", "go_back", "返回", show=False),
    )

    def __init__(self, *, locale: Translator, result: ApiExportResult, run: ApiRun) -> None:
        super().__init__(name="api-export-result")
        self.locale = locale
        self.result = result
        self.run = run

    def compose(self) -> ComposeResult:
        yield Container(
            Vertical(
                Static(self.locale("api.export.result.title"), id="api-export-result-heading"),
                Static(id="api-export-result-body", markup=False),
                Static(id="api-export-result-artifacts", markup=False),
                Horizontal(
                    Button(
                        self.locale("api.export.result.return"),
                        id="api-export-result-return",
                        variant="primary",
                    ),
                    id="api-export-result-actions",
                ),
                id="api-export-result-card",
            ),
            id="api-export-result-dialog",
        )

    def on_mount(self) -> None:
        self.call_after_refresh(self._refresh)
        self.call_after_refresh(self._focus_return)

    def on_resize(self, event: Resize) -> None:
        del event
        self.call_after_refresh(self._refresh)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "api-export-result-return":
            self.action_go_back()

    def action_go_back(self) -> None:
        self.app.pop_screen()

    def _refresh(self) -> None:
        body = self.query_one("#api-export-result-body", Static)
        artifacts = self.query_one("#api-export-result-artifacts", Static)
        width = max(1, body.content_region.width or self.size.width - 8)
        body.update(
            self._fit(
                "\n".join(
                    (
                        self.locale(
                            "api.export.result.run",
                            id=self.run.id,
                            name=self.run.scenario.name,
                        ),
                        self.locale("api.export.result.location"),
                        str(self.result.destination),
                    )
                ),
                width,
            )
        )
        relative_files = "\n".join(self._relative(path) for path in self.result.files)
        artifacts.update(
            self._fit(
                f"{self.locale('api.export.result.artifacts')}\n{relative_files}",
                width,
            )
        )

    def _focus_return(self) -> None:
        self.query_one("#api-export-result-return", Button).focus()

    def _relative(self, path: Path) -> str:
        try:
            return str(path.relative_to(self.result.destination)).replace("\\", "/")
        except ValueError:
            return str(path)

    @staticmethod
    def _fit(value: str, width: int) -> str:
        return "\n".join(part for line in value.splitlines() for part in wrap_cells(line, width))


__all__ = ["ApiExportResultScreen"]
