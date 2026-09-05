from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Container, Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Static

from csbox.locales import Translator
from csbox.tui.help import HELP_BINDINGS


class CaptureTitleDialog(ModalScreen[str | None]):
    """Ask for a Capture title without putting input parsing in the TUI screen."""

    BINDINGS = [*HELP_BINDINGS, ("escape", "cancel", "取消")]

    def __init__(
        self,
        *,
        locale: Translator,
        title: str = "",
        heading: str = "关键画面标题",
    ) -> None:
        super().__init__()
        self.locale = locale
        self.initial_title = title
        self.heading = heading

    def compose(self) -> ComposeResult:
        yield Container(
            Vertical(
                Static(self.heading, id="capture-title-heading", markup=False),
                Input(
                    value=self.initial_title,
                    placeholder="可留空，使用默认标题",
                    id="capture-title-input",
                ),
                Horizontal(
                    Button("确定", id="capture-title-confirm", variant="primary"),
                    Button("取消", id="capture-title-cancel"),
                    id="capture-title-actions",
                ),
                id="capture-title-card",
            ),
            id="capture-title-dialog",
        )

    def on_mount(self) -> None:
        self.query_one("#capture-title-input", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "capture-title-input":
            self.dismiss(event.value.strip())

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "capture-title-confirm":
            value = self.query_one("#capture-title-input", Input).value.strip()
            self.dismiss(value)
        elif event.button.id == "capture-title-cancel":
            self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)


__all__ = ["CaptureTitleDialog"]
