"""Result and recovery screen for Evidence Set report handoff."""

from __future__ import annotations

from collections.abc import Callable

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical, VerticalScroll
from textual.events import Resize
from textual.screen import Screen
from textual.widgets import Button, Static

from csbox.core.text_layout import wrap_cells
from csbox.evidence.exporter import ReportExportError, ReportExportResult
from csbox.locales import Translator


class ReportExportResultScreen(Screen[None]):
    """Show exact generated paths or a controlled retryable failure."""

    BINDINGS = (
        Binding("enter", "activate_primary", "确认", show=False, priority=True),
        Binding("escape", "go_back", "返回", show=False),
        Binding("q", "go_back", "返回", show=False),
    )

    def __init__(
        self,
        *,
        locale: Translator,
        result: ReportExportResult | None = None,
        error: ReportExportError | None = None,
        on_retry: Callable[[], None] | None = None,
        on_change_destination: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(name="report-export-result")
        self.locale = locale
        self.result = result
        self.error = error
        self.on_retry = on_retry
        self.on_change_destination = on_change_destination

    def compose(self) -> ComposeResult:
        buttons = (
            (
                Button(
                    self.locale("evidence.export.result.retry"),
                    id="report-result-retry",
                    variant="primary",
                ),
                Button(
                    self.locale("evidence.export.result.change_destination"),
                    id="report-result-change",
                ),
                Button(
                    self.locale("evidence.export.result.return"),
                    id="report-result-return",
                ),
            )
            if self.error is not None
            else (
                Button(
                    self.locale("evidence.export.result.return"),
                    id="report-result-return",
                    variant="primary",
                ),
            )
        )
        yield Container(
            Vertical(
                Static(id="report-result-heading", markup=False),
                VerticalScroll(
                    Static(id="report-result-body", markup=False),
                    Static(id="report-result-artifacts", markup=False),
                    id="report-result-scroll",
                ),
                Horizontal(*buttons, id="report-result-actions"),
                id="report-result-card",
            ),
            id="report-result-dialog",
        )

    def on_mount(self) -> None:
        self.call_after_refresh(self._refresh)
        self.call_after_refresh(self._focus_primary)

    def on_resize(self, event: Resize) -> None:
        del event
        self.call_after_refresh(self._refresh)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "report-result-retry":
            self.action_retry()
        elif event.button.id == "report-result-change":
            self.action_change_destination()
        elif event.button.id == "report-result-return":
            self.action_go_back()

    def action_activate_primary(self) -> None:
        if self.error is not None:
            self.action_retry()
        else:
            self.action_go_back()

    def action_retry(self) -> None:
        if self.on_retry is None:
            return
        retry = self.on_retry
        self.app.pop_screen()
        retry()

    def action_change_destination(self) -> None:
        if self.on_change_destination is None:
            return
        change_destination = self.on_change_destination
        self.app.pop_screen()
        change_destination()

    def action_go_back(self) -> None:
        self.app.pop_screen()

    def _refresh(self) -> None:
        heading = self.query_one("#report-result-heading", Static)
        body = self.query_one("#report-result-body", Static)
        artifacts = self.query_one("#report-result-artifacts", Static)
        width = max(2, body.content_region.width or self.size.width - 8)

        if self.error is not None or self.result is None:
            heading.update(self.locale("evidence.export.result.failed"))
            error = self.error
            next_step = (
                "evidence.export.result.next"
                if error is None or error.kind == "missing_source"
                else "evidence.export.result.next.profile"
                if error.kind == "profile"
                else "evidence.export.result.next.destination"
            )
            lines = [
                error_message(error),
                self.locale(next_step),
            ]
            if error is not None:
                if error.destination is not None:
                    lines.insert(
                        1,
                        self.locale(
                            "evidence.export.result.destination",
                            path=str(error.destination),
                        ),
                    )
                if error.unavailable_titles:
                    lines.insert(
                        1,
                        self.locale(
                            "evidence.export.result.unavailable",
                            titles="、".join(error.unavailable_titles),
                        ),
                    )
            body.update(self._fit("\n".join(lines), width))
            artifacts.update("")
            return

        result = self.result
        heading.update(self.locale("evidence.export.result.complete"))
        body.update(
            self._fit(
                "\n".join(
                    (
                        self.locale("evidence.export.result.status"),
                        self.locale(
                            "evidence.export.result.images",
                            count=len(result.images),
                        ),
                        self.locale(
                            "evidence.export.result.items",
                            count=result.evidence_item_count,
                        ),
                    )
                ),
                width,
            )
        )
        artifact_lines = [
            self.locale("evidence.export.result.markdown", path=str(result.markdown)),
            self.locale("evidence.export.result.docx", path=str(result.docx)),
        ]
        if result.warnings:
            artifact_lines.append(
                self.locale(
                    "evidence.export.result.warnings",
                    value="；".join(result.warnings),
                )
            )
        artifacts.update(self._fit("\n".join(artifact_lines), width))

    def _focus_primary(self) -> None:
        target = "#report-result-retry" if self.error is not None else "#report-result-return"
        self.query_one(target, Button).focus()

    @staticmethod
    def _fit(value: str, width: int) -> str:
        return "\n".join(
            wrapped for line in value.splitlines() for wrapped in wrap_cells(line, width)
        )


def error_message(error: ReportExportError | None) -> str:
    if error is None:
        return "报告材料导出没有返回结果，请重试。"
    return str(error)


__all__ = ["ReportExportResultScreen", "error_message"]
