from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Static, TextArea

from csbox.locales import Translator
from csbox.tui.help import HELP_BINDINGS


class EvidenceSetTitleDialog(ModalScreen[str | None]):
    """Collect the required Evidence Set title without writing from Input events."""

    BINDINGS = [*HELP_BINDINGS, Binding("escape", "cancel", "取消")]

    def __init__(
        self,
        *,
        locale: Translator,
        initial_title: str = "",
        error: str = "",
    ) -> None:
        super().__init__()
        self.locale = locale
        self.initial_title = initial_title
        self.error = error

    def compose(self) -> ComposeResult:
        yield Container(
            Vertical(
                Static(
                    self.locale("evidence.create.title"), id="evidence-set-heading", markup=False
                ),
                Input(
                    value=self.initial_title,
                    placeholder=self.locale("evidence.create.placeholder"),
                    id="evidence-set-title-input",
                ),
                Static(id="evidence-set-title-error", markup=False),
                Horizontal(
                    Button(
                        self.locale("evidence.action.create"),
                        id="evidence-set-confirm",
                        variant="primary",
                    ),
                    Button(self.locale("evidence.action.cancel"), id="evidence-set-cancel"),
                    id="evidence-set-actions",
                ),
                id="evidence-set-card",
            ),
            id="evidence-set-dialog",
        )

    def on_mount(self) -> None:
        if self.error:
            self.query_one("#evidence-set-title-error", Static).update(self.error)
        self.query_one("#evidence-set-title-input", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "evidence-set-title-input":
            self._submit()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "evidence-set-confirm":
            self._submit()
        elif event.button.id == "evidence-set-cancel":
            self.action_cancel()

    def _submit(self) -> None:
        value = self.query_one("#evidence-set-title-input", Input).value
        if not value.strip():
            self.query_one("#evidence-set-title-error", Static).update(
                self.locale("evidence.validation.title")
            )
            return
        self.dismiss(value)

    def action_cancel(self) -> None:
        self.dismiss(None)


class EvidenceItemDialog(ModalScreen[tuple[str, str, str] | None]):
    """Collect Evidence Item presentation metadata with one explicit save."""

    BINDINGS = [
        *HELP_BINDINGS,
        Binding("escape", "cancel", "取消"),
        Binding("ctrl+s", "save", "保存", show=False, priority=True),
    ]

    def __init__(
        self,
        *,
        locale: Translator,
        title: str = "",
        caption: str = "",
        note: str = "",
        heading: str | None = None,
    ) -> None:
        super().__init__()
        self.locale = locale
        self.initial_title = title
        self.initial_caption = caption
        self.initial_note = note
        self.heading = heading or locale("evidence.item.edit")

    def compose(self) -> ComposeResult:
        yield Container(
            Vertical(
                Static(self.heading, id="evidence-item-heading", markup=False),
                Static(
                    self.locale("evidence.item.title.label"),
                    classes="evidence-field-label",
                    markup=False,
                ),
                Input(value=self.initial_title, id="evidence-item-title-input"),
                Static(
                    self.locale("evidence.item.caption.label"),
                    classes="evidence-field-label",
                    markup=False,
                ),
                Input(value=self.initial_caption, id="evidence-item-caption-input"),
                Static(
                    self.locale("evidence.item.note.label"),
                    classes="evidence-field-label",
                    markup=False,
                ),
                TextArea(self.initial_note, id="evidence-item-note-input", soft_wrap=True),
                Static(id="evidence-item-error", markup=False),
                Horizontal(
                    Button(
                        self.locale("evidence.action.save"),
                        id="evidence-item-save",
                        variant="primary",
                    ),
                    Button(self.locale("evidence.action.cancel"), id="evidence-item-cancel"),
                    id="evidence-item-actions",
                ),
                id="evidence-item-card",
            ),
            id="evidence-item-dialog",
        )

    def on_mount(self) -> None:
        self.query_one("#evidence-item-title-input", Input).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "evidence-item-save":
            self.action_save()
        elif event.button.id == "evidence-item-cancel":
            self.action_cancel()

    def action_save(self) -> None:
        title = self.query_one("#evidence-item-title-input", Input).value
        if not title.strip():
            self.query_one("#evidence-item-error", Static).update(
                self.locale("evidence.validation.item_title")
            )
            self.query_one("#evidence-item-title-input", Input).focus()
            return
        caption = self.query_one("#evidence-item-caption-input", Input).value
        note = self.query_one("#evidence-item-note-input", TextArea).text
        self.dismiss((title, caption, note))

    def action_cancel(self) -> None:
        self.dismiss(None)


__all__ = ["EvidenceItemDialog", "EvidenceSetTitleDialog"]
