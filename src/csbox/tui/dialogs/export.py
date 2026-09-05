from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from pathlib import Path

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical, VerticalScroll
from textual.reactive import reactive
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Select, Static

from csbox.api.redaction import Redactor
from csbox.core.display_width import truncate_cells
from csbox.lab.repository import SessionSummary
from csbox.locales import Translator
from csbox.tui.help import HELP_BINDINGS

_EXPORT_REDACTOR = Redactor.with_configured_values(())


@dataclass(frozen=True, slots=True)
class ExportRequest:
    session_id: str
    destination: Path
    theme: str = "dark"
    force: bool = False


class ExportOverwriteDialog(ModalScreen[bool]):
    """Confirm the existing exporter force contract without exposing path details."""

    BINDINGS = [
        *HELP_BINDINGS,
        Binding("escape", "cancel", "取消"),
        Binding("q", "cancel", "取消", show=False),
    ]

    def __init__(self) -> None:
        super().__init__(name="export-overwrite")

    def compose(self) -> ComposeResult:
        yield Container(
            Vertical(
                Static("导出目录已存在", id="export-overwrite-heading", markup=False),
                Static(
                    "该导出目录已经存在。\n是否使用已有安全刷新语义更新导出材料？",
                    id="export-overwrite-message",
                    markup=False,
                ),
                Horizontal(
                    Button("刷新已有导出", id="export-overwrite-confirm", variant="primary"),
                    Button("取消", id="export-overwrite-cancel"),
                    id="export-overwrite-actions",
                ),
                id="export-overwrite-card",
            ),
            id="export-overwrite-dialog",
        )

    def on_mount(self) -> None:
        self.query_one("#export-overwrite-confirm", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "export-overwrite-confirm":
            self.dismiss(True)
        elif event.button.id == "export-overwrite-cancel":
            self.dismiss(False)

    def action_cancel(self) -> None:
        self.dismiss(False)


class ExportDestinationInput(Input):
    """Keep destination typing local to the Input without triggering layout work."""

    value = reactive("", layout=False, init=False)


class ExportDialog(ModalScreen[ExportRequest | None]):
    """Collect only the stable Lab export options needed by the beginner workflow."""

    BINDINGS = [
        *HELP_BINDINGS,
        Binding("up", "focus_previous_control", "上一个控件", show=False, priority=True),
        Binding("down", "focus_next_control", "下一个控件", show=False, priority=True),
        Binding("escape", "cancel", "取消"),
        Binding("q", "cancel", "取消", show=False),
    ]

    def __init__(
        self,
        *,
        locale: Translator,
        summary: SessionSummary,
        destination: Path,
    ) -> None:
        super().__init__(name="export")
        self.locale = locale
        self.summary = summary
        self.default_destination = Path(destination)
        self.destination = self.default_destination
        self.theme = "dark"

    def compose(self) -> ComposeResult:
        yield Container(
            Vertical(
                VerticalScroll(
                    Static("导出实验材料", id="export-heading", markup=False),
                    Static(
                        f"实验\n{_safe_text(self.summary.metadata.experiment_name)}",
                        id="export-experiment",
                        markup=False,
                    ),
                    Static(
                        f"关键画面\n{self.summary.capture_count}",
                        id="export-captures",
                        markup=False,
                    ),
                    Static("导出位置", classes="field-label", markup=False),
                    ExportDestinationInput(
                        value=str(self.default_destination),
                        id="export-destination-input",
                        classes="export-control",
                    ),
                    Static(id="export-validation", markup=False),
                    Button("恢复默认", id="export-restore-default", classes="export-control"),
                    Static("主题", classes="field-label", markup=False),
                    Static("深色", id="export-theme-display", markup=False),
                    Button("高级设置", id="export-advanced", classes="export-control"),
                    Select(
                        [("深色", "dark"), ("浅色", "light")],
                        value="dark",
                        allow_blank=False,
                        id="export-theme-select",
                        classes="export-control",
                    ),
                    id="export-fields",
                ),
                Horizontal(
                    Button(
                        "导出材料",
                        id="export-confirm",
                        classes="export-control",
                        variant="primary",
                    ),
                    Button("取消", id="export-cancel", classes="export-control"),
                    id="export-actions",
                ),
                id="export-card",
            ),
            id="export-dialog",
        )

    def on_mount(self) -> None:
        self.query_one("#export-theme-select", Select).display = False
        self.call_after_refresh(self._refresh_experiment)
        self._refresh_destination_validation()
        self.query_one("#export-destination-input", Input).focus()

    def on_resize(self) -> None:
        self.call_after_refresh(self._refresh_experiment)

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id != "export-destination-input":
            return
        self.destination = Path(event.value)
        self._refresh_destination_validation()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "export-destination-input":
            self._submit()

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id != "export-theme-select" or event.value is Select.BLANK:
            return
        self.theme = str(event.value)
        self.query_one("#export-theme-display", Static).update(
            "浅色" if self.theme == "light" else "深色"
        )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "export-confirm":
            self._submit()
        elif event.button.id == "export-cancel":
            self.dismiss(None)
        elif event.button.id == "export-restore-default":
            self.destination = self.default_destination
            self.query_one("#export-destination-input", Input).value = str(self.default_destination)
        elif event.button.id == "export-advanced":
            theme_select = self.query_one("#export-theme-select", Select)
            theme_select.display = not theme_select.display

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_focus_next_control(self) -> None:
        self.focus_next(
            "#export-destination-input, #export-restore-default, #export-advanced, "
            "#export-theme-select, #export-confirm, #export-cancel"
        )

    def action_focus_previous_control(self) -> None:
        self.focus_previous(
            "#export-destination-input, #export-restore-default, #export-advanced, "
            "#export-theme-select, #export-confirm, #export-cancel"
        )

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if (
            action in {"focus_next_control", "focus_previous_control"}
            and self.query_one("#export-theme-select", Select).expanded
        ):
            return False
        return super().check_action(action, parameters)

    def _submit(self) -> None:
        destination_input = self.query_one("#export-destination-input", Input)
        if not destination_input.value.strip():
            self._refresh_destination_validation()
            destination_input.focus()
            return
        self.destination = Path(destination_input.value)
        try:
            existing_directory = self.destination.is_dir() and not self.destination.is_symlink()
        except (OSError, ValueError):
            existing_directory = False
        if existing_directory:
            self.app.push_screen(
                ExportOverwriteDialog(),
                self._handle_overwrite_confirmation,
            )
            return
        self.dismiss(self._request(force=False))

    def _handle_overwrite_confirmation(self, confirmed: bool) -> None:
        if confirmed:
            self.dismiss(self._request(force=True))
        else:
            self.dismiss(None)

    def _request(self, *, force: bool) -> ExportRequest:
        return ExportRequest(
            session_id=self.summary.metadata.session_id,
            destination=self.destination,
            theme=self.theme,
            force=force,
        )

    def _refresh_destination_validation(self) -> None:
        destination_input = self.query_one("#export-destination-input", Input)
        empty = not destination_input.value.strip()
        self.query_one("#export-confirm", Button).disabled = empty
        self.query_one("#export-validation", Static).update("请输入导出位置" if empty else "")

    def _refresh_experiment(self) -> None:
        experiment = self.query_one("#export-experiment", Static)
        width = experiment.content_region.width or max(1, self.size.width - 8)
        experiment.update(
            "实验\n"
            + truncate_cells(
                _safe_text(self.summary.metadata.experiment_name),
                width,
                ellipsis="…",
            )
        )


def _safe_text(value: str) -> str:
    redacted = _EXPORT_REDACTOR.text(value)
    cleaned = "".join(
        " " if unicodedata.category(character).startswith("C") else character
        for character in redacted
    )
    return " ".join(cleaned.split())


__all__ = ["ExportDialog", "ExportOverwriteDialog", "ExportRequest"]
