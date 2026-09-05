from __future__ import annotations

from pathlib import Path

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical, VerticalScroll
from textual.events import Resize
from textual.reactive import reactive
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Select, Static

from csbox.api.redaction import Redactor
from csbox.core.display_width import truncate_cells
from csbox.locales import Translator
from csbox.tui.help import HELP_BINDINGS
from csbox.tui.lab_workflow import (
    ExperimentNameError,
    LabStartRequest,
    ShellOption,
)

_PATH_REDACTOR = Redactor.with_configured_values(())


class LabNameInput(Input):
    """A fixed-width Input whose value changes only require a local repaint."""

    value = reactive("", layout=False, init=False)


class LabShellSelect(Select[str]):
    """Let form navigation own arrows until the Select menu is open."""

    BINDINGS = [
        Binding("enter,space", "show_overlay", "选择 Shell", show=False),
    ]


class LabStartDialog(ModalScreen[LabStartRequest | None]):
    """Collect the small, current-project-only Lab start request."""

    BINDINGS = [
        *HELP_BINDINGS,
        Binding(
            "up",
            "focus_previous_control",
            "上一个控件",
            show=False,
            priority=True,
        ),
        Binding(
            "down",
            "focus_next_control",
            "下一个控件",
            show=False,
            priority=True,
        ),
        Binding("escape", "cancel", "取消"),
    ]

    def __init__(
        self,
        *,
        locale: Translator,
        project_dir: Path,
        shell_options: tuple[ShellOption, ...],
        shell_error: str | None = None,
    ) -> None:
        super().__init__()
        self.locale = locale
        self.project_dir = project_dir
        self.shell_options = shell_options
        self.shell_error = shell_error
        self.selected_shell = shell_options[0].value if shell_options else ""
        self._labels = {option.value: option.label for option in shell_options}

    def compose(self) -> ComposeResult:
        select_options = [(option.label, option.value) for option in self.shell_options]
        if not select_options:
            select_options = [(self.locale("lab.start.shell.none"), "")]
        shell_label = (
            self.shell_options[0].label
            if self.shell_options
            else self.locale("lab.start.shell.none")
        )
        yield Container(
            Vertical(
                Static(self.locale("lab.start.title"), id="lab-start-heading", markup=False),
                VerticalScroll(
                    Static(self.locale("lab.start.name"), classes="field-label", markup=False),
                    LabNameInput(
                        placeholder=self.locale("lab.start.name.placeholder"),
                        id="lab-start-name",
                        classes="lab-start-control",
                    ),
                    Static(self.locale("lab.start.shell"), classes="field-label", markup=False),
                    Static(shell_label, id="lab-start-shell-display", markup=False),
                    Button(
                        self.locale("lab.start.advanced"),
                        id="lab-start-advanced",
                        classes="lab-start-control",
                        disabled=not self.shell_options,
                    ),
                    LabShellSelect(
                        select_options,
                        allow_blank=False,
                        value=self.selected_shell,
                        id="lab-start-shell-select",
                        classes="lab-start-control",
                        disabled=not self.shell_options,
                    ),
                    Static(
                        self.locale("lab.start.project"),
                        classes="field-label",
                        markup=False,
                    ),
                    Static(self._project_text(58), id="lab-start-project", markup=False),
                    Static(self.locale("lab.start.hint"), id="lab-start-hint", markup=False),
                    Static(
                        self.shell_error or "",
                        id="lab-start-validation",
                        markup=False,
                    ),
                    id="lab-start-fields",
                ),
                Horizontal(
                    Button(
                        self.locale("lab.start.confirm"),
                        id="lab-start-confirm",
                        classes="lab-start-control",
                        variant="primary",
                        disabled=not self.shell_options,
                    ),
                    Button(
                        self.locale("lab.start.cancel"),
                        id="lab-start-cancel",
                        classes="lab-start-control",
                    ),
                    id="lab-start-actions",
                ),
                id="lab-start-card",
            ),
            id="lab-start-dialog",
        )

    def on_mount(self) -> None:
        self.query_one("#lab-start-shell-select", Select).display = False
        self.call_after_refresh(self._refresh_project_path)
        self.query_one("#lab-start-name", Input).focus()

    def on_resize(self, event: Resize) -> None:
        del event
        self.call_after_refresh(self._refresh_project_path)

    def on_select_changed(self, event: Select.Changed) -> None:
        value = str(event.value)
        if value not in self._labels:
            return
        self.selected_shell = value
        self.query_one("#lab-start-shell-display", Static).update(self._labels[value])

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "lab-start-name":
            self._submit()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "lab-start-confirm":
            self._submit()
        elif event.button.id == "lab-start-cancel":
            self.dismiss(None)
        elif event.button.id == "lab-start-advanced":
            shell_select = self.query_one("#lab-start-shell-select", Select)
            shell_select.display = not shell_select.display

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_focus_next_control(self) -> None:
        self.focus_next(".lab-start-control")

    def action_focus_previous_control(self) -> None:
        self.focus_previous(".lab-start-control")

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if action in {"focus_next_control", "focus_previous_control"}:
            shell_select = self.query_one("#lab-start-shell-select", LabShellSelect)
            if shell_select.expanded:
                return False
        return super().check_action(action, parameters)

    def _submit(self) -> None:
        name_input = self.query_one("#lab-start-name", Input)
        if not self.shell_options:
            self.query_one("#lab-start-validation", Static).update(
                self.shell_error or self.locale("lab.start.shell.none")
            )
            name_input.focus()
            return
        try:
            request = LabStartRequest.from_input(name_input.value, self.selected_shell)
        except ExperimentNameError as error:
            self.query_one("#lab-start-validation", Static).update(str(error))
            name_input.focus()
            return
        self.dismiss(request)

    def _project_text(self, width: int) -> str:
        return truncate_cells(
            _PATH_REDACTOR.text(str(self.project_dir)),
            max(1, width),
            ellipsis="…",
        )

    def _refresh_project_path(self) -> None:
        project = self.query_one("#lab-start-project", Static)
        project.update(self._project_text(project.content_region.width or 1))


__all__ = ["LabStartDialog"]
