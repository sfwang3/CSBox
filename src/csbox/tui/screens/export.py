from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical
from textual.events import Resize
from textual.screen import Screen
from textual.widgets import Button, Static

from csbox.api.redaction import Redactor
from csbox.core.display_width import display_width, truncate_cells
from csbox.lab.exporter import LabExportResult

_EXPORT_RESULT_REDACTOR = Redactor.with_configured_values(())


class ExportResultScreen(Screen[None]):
    """Show an actionable, artifact-accurate outcome for a Lab export."""

    BINDINGS = (
        Binding("enter", "activate_primary", "确认", show=False, priority=True),
        Binding("escape", "go_back", "返回", show=False),
        Binding("q", "go_back", "返回", show=False),
    )

    def __init__(
        self,
        *,
        result: LabExportResult | None = None,
        failure_message: str | None = None,
        on_retry: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(name="export-result")
        self.result = result
        self.failure_message = failure_message
        self.on_retry = on_retry

    def compose(self) -> ComposeResult:
        action_buttons = (
            (
                Button("重新导出", id="export-result-retry", variant="primary"),
                Button("返回实验记录", id="export-result-return"),
            )
            if self.failure_message is not None
            else (Button("返回实验记录", id="export-result-return", variant="primary"),)
        )
        yield Container(
            Vertical(
                Static(id="export-result-heading", markup=False),
                Static(id="export-result-body", markup=False),
                Static(id="export-result-artifacts", markup=False),
                Horizontal(*action_buttons, id="export-result-actions"),
                id="export-result-card",
            ),
            id="export-result-dialog",
        )

    def on_mount(self) -> None:
        self.call_after_refresh(self._refresh)
        self.call_after_refresh(self._focus_primary)

    def on_resize(self, event: Resize) -> None:
        del event
        self.call_after_refresh(self._refresh)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "export-result-return":
            self.action_go_back()
        elif event.button.id == "export-result-retry":
            self.action_retry()

    def action_activate_primary(self) -> None:
        if self.on_retry is not None:
            self.action_retry()
        else:
            self.action_go_back()

    def action_retry(self) -> None:
        retry = self.on_retry
        if retry is None:
            return
        self.app.pop_screen()
        retry()

    def action_go_back(self) -> None:
        self.app.pop_screen()

    def _refresh(self) -> None:
        heading = self.query_one("#export-result-heading", Static)
        body = self.query_one("#export-result-body", Static)
        artifacts = self.query_one("#export-result-artifacts", Static)
        width = max(1, body.content_region.width or self.size.width - 8)

        if self.failure_message is not None:
            heading.update("导出失败")
            body.update(
                self._fit(
                    f"{self.failure_message}\n请检查目标目录权限和实验记录后重试。",
                    width,
                )
            )
            artifacts.update("")
        else:
            result = self.result
            if result is None:
                heading.update("导出失败")
                body.update("导出没有返回结果，请返回后重试。")
                artifacts.update("")
                return
            heading.update("导出完成")
            warning_line = (
                f"导出完成，但有 {len(result.warnings)} 条提示" if result.warnings else "导出完成"
            )
            body.update(
                self._fit(
                    f"{warning_line}\n关键画面：{len(result.evidence)}\n"
                    f"位置：{_truncate_path_for_label(str(result.destination), width)}",
                    width,
                )
            )
            generated = [
                *(_artifact_label(result.destination, path) for path in result.evidence),
                _artifact_label(result.destination, result.markdown),
                _artifact_label(result.destination, result.cast),
                *(
                    [_artifact_label(result.destination, result.commands)]
                    if result.commands is not None
                    else []
                ),
            ]
            if result.warnings:
                generated.append(
                    "提示：" + "；".join(_safe_text(warning) for warning in result.warnings)
                )
            artifacts.update(self._fit("已生成：\n" + "\n".join(generated), width))

    def _focus_primary(self) -> None:
        button = self.query_one(
            "#export-result-retry" if self.on_retry is not None else "#export-result-return",
            Button,
        )
        button.focus()

    @staticmethod
    def _fit(value: str, width: int) -> str:
        return "\n".join(truncate_cells(line, width, ellipsis="…") for line in value.splitlines())


def _safe_text(value: str) -> str:
    return _EXPORT_RESULT_REDACTOR.text(value).replace("\n", " ").replace("\r", " ")


def _artifact_label(destination: Path, artifact: Path) -> str:
    try:
        relative = artifact.relative_to(destination)
    except ValueError:
        relative = Path(artifact.name)
    return _safe_text(str(relative).replace("\\", "/"))


def _truncate_path_for_label(value: str, width: int) -> str:
    prefix = "位置："
    available = max(1, width - display_width(prefix))
    safe_value = _safe_text(value)
    if display_width(safe_value) <= available:
        return safe_value
    separator_index = max(safe_value.rfind("/"), safe_value.rfind("\\"))
    if separator_index >= 0:
        separator = safe_value[separator_index]
        tail = safe_value[separator_index + 1 :]
        suffix = "…" + separator + tail
        if display_width(suffix) <= available:
            return suffix
    return truncate_cells(safe_value, available, ellipsis="…")


__all__ = ["ExportResultScreen"]
