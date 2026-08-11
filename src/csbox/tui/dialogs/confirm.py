from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Container, Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Static

from csbox.locales import Translator


class ConfirmDialog(ModalScreen[bool]):
    """Confirm an irreversible local UI action."""

    BINDINGS = [("escape", "cancel", "取消")]

    def __init__(self, *, locale: Translator, title: str, message: str) -> None:
        super().__init__()
        self.locale = locale
        self.dialog_title = title
        self.message = message

    def compose(self) -> ComposeResult:
        yield Container(
            Vertical(
                Static(self.dialog_title, id="confirm-heading", markup=False),
                Static(self.message, id="confirm-message", markup=False),
                Horizontal(
                    Button("确认", id="confirm-yes", variant="error"),
                    Button("取消", id="confirm-no"),
                    id="confirm-actions",
                ),
                id="confirm-card",
            ),
            id="confirm-dialog",
        )

    def on_mount(self) -> None:
        self.query_one("#confirm-no", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "confirm-yes":
            self.dismiss(True)
        elif event.button.id == "confirm-no":
            self.dismiss(False)

    def action_cancel(self) -> None:
        self.dismiss(False)


__all__ = ["ConfirmDialog"]
