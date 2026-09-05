from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Container, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Static

from csbox.locales import Translator
from csbox.tui.help import HELP_BINDINGS


class UnavailableDialog(ModalScreen[None]):
    BINDINGS = [*HELP_BINDINGS, ("escape", "close_dialog", "")]

    def __init__(
        self,
        *,
        title: str,
        locale: Translator,
        message_key: str = "home.entry.unavailable.body",
        close_key: str = "home.entry.close",
        message: str | None = None,
    ) -> None:
        super().__init__()
        self.title = title
        self.locale = locale
        self.message_key = message_key
        self.close_key = close_key
        self.message = message

    def compose(self) -> ComposeResult:
        yield Container(
            Vertical(
                Static(self.title, id="dialog-title", markup=False),
                Static(
                    self.message if self.message is not None else self.locale(self.message_key),
                    id="dialog-message",
                    markup=False,
                ),
                Button(
                    self.locale(self.close_key),
                    id="dialog-close",
                    variant="primary",
                ),
                id="dialog-card",
            ),
            id="unavailable-dialog",
        )

    def on_mount(self) -> None:
        self.call_after_refresh(self._focus_close)

    def _focus_close(self) -> None:
        self.query_one("#dialog-close", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "dialog-close":
            self.dismiss()

    def action_close_dialog(self) -> None:
        self.dismiss()
