"""Destination dialogs for Evidence Set report handoff."""

from __future__ import annotations

from pathlib import Path

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical, VerticalScroll
from textual.reactive import reactive
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, Input, Static, TextArea

from csbox.core.text_layout import wrap_cells
from csbox.evidence.exporter import ReportExportRequest
from csbox.locales import Translator
from csbox.report.models import MAX_REPORT_SECTIONS, ReportProfile, ReportSection


class ReportExportOverwriteDialog(ModalScreen[bool]):
    """Require an explicit confirmation before refreshing an existing directory."""

    BINDINGS = [
        Binding("escape", "cancel", "取消"),
        Binding("q", "cancel", "取消", show=False),
    ]

    def __init__(self, *, locale: Translator, destination: Path) -> None:
        super().__init__(name="report-export-overwrite")
        self.locale = locale
        self.destination = destination

    def compose(self) -> ComposeResult:
        yield Container(
            Vertical(
                Static(
                    self.locale("evidence.export.overwrite.title"),
                    id="report-overwrite-heading",
                    markup=False,
                ),
                Static(
                    self.locale(
                        "evidence.export.overwrite.message",
                        path=str(self.destination),
                    ),
                    id="report-overwrite-message",
                    markup=False,
                ),
                Horizontal(
                    Button(
                        self.locale("evidence.export.overwrite.confirm"),
                        id="report-overwrite-confirm",
                        variant="primary",
                    ),
                    Button(
                        self.locale("evidence.export.overwrite.cancel"),
                        id="report-overwrite-cancel",
                    ),
                    id="report-overwrite-actions",
                ),
                id="report-overwrite-card",
            ),
            id="report-overwrite-dialog",
        )

    def on_mount(self) -> None:
        self._refresh_message()
        self.query_one("#report-overwrite-confirm", Button).focus()

    def on_resize(self) -> None:
        self.call_after_refresh(self._refresh_message)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "report-overwrite-confirm":
            self.dismiss(True)
        elif event.button.id == "report-overwrite-cancel":
            self.dismiss(False)

    def action_cancel(self) -> None:
        self.dismiss(False)

    def _refresh_message(self) -> None:
        message = self.query_one("#report-overwrite-message", Static)
        width = max(2, message.content_region.width or self.size.width - 8)
        message.update(
            "\n".join(
                wrap_cells(
                    self.locale(
                        "evidence.export.overwrite.message",
                        path=str(self.destination),
                    ),
                    width,
                )
            )
        )


class ReportDestinationInput(Input):
    """Keep report destination editing local to the destination field."""

    value = reactive("", layout=False, init=False)


class ReportExportDialog(ModalScreen[ReportExportRequest | None]):
    """Collect a report destination and explicit overwrite choice."""

    BINDINGS = [
        Binding("up", "focus_previous_control", "上一个控件", show=False, priority=True),
        Binding("down", "focus_next_control", "下一个控件", show=False, priority=True),
        Binding("escape", "cancel", "取消"),
        Binding("q", "cancel", "取消", show=False),
    ]

    def __init__(
        self,
        *,
        locale: Translator,
        destination: Path,
    ) -> None:
        super().__init__(name="report-export")
        self.locale = locale
        self.default_destination = Path(destination)
        self.destination = self.default_destination

    def compose(self) -> ComposeResult:
        yield Container(
            Vertical(
                VerticalScroll(
                    Static(
                        self.locale("evidence.export.title"),
                        id="report-export-heading",
                        markup=False,
                    ),
                    Static(
                        self.locale("evidence.export.hint"),
                        id="report-export-hint",
                        markup=False,
                    ),
                    Static(
                        self.locale("evidence.export.destination"),
                        classes="field-label",
                        markup=False,
                    ),
                    ReportDestinationInput(
                        value=str(self.default_destination),
                        id="report-destination-input",
                        classes="report-export-control",
                    ),
                    Static(id="report-destination-validation", markup=False),
                    Button(
                        self.locale("evidence.export.restore_default"),
                        id="report-restore-default",
                        classes="report-export-control",
                    ),
                    id="report-export-fields",
                ),
                Horizontal(
                    Button(
                        self.locale("evidence.export.submit"),
                        id="report-export-confirm",
                        classes="report-export-control",
                        variant="primary",
                    ),
                    Button(
                        self.locale("evidence.export.cancel"),
                        id="report-export-cancel",
                        classes="report-export-control",
                    ),
                    id="report-export-actions",
                ),
                id="report-export-card",
            ),
            id="report-export-dialog",
        )

    def on_mount(self) -> None:
        self._refresh_destination_validation()
        self._refresh_hint()
        self.query_one("#report-destination-input", Input).focus()

    def on_resize(self) -> None:
        self.call_after_refresh(self._refresh_destination_validation)
        self.call_after_refresh(self._refresh_hint)

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id != "report-destination-input":
            return
        self.destination = Path(event.value)
        self._refresh_destination_validation()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "report-destination-input":
            self._submit()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "report-export-confirm":
            self._submit()
        elif event.button.id == "report-export-cancel":
            self.action_cancel()
        elif event.button.id == "report-restore-default":
            self.destination = self.default_destination
            self.query_one("#report-destination-input", Input).value = str(self.destination)

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_focus_next_control(self) -> None:
        self.focus_next(
            "#report-destination-input, #report-restore-default, "
            "#report-export-confirm, #report-export-cancel"
        )

    def action_focus_previous_control(self) -> None:
        self.focus_previous(
            "#report-destination-input, #report-restore-default, "
            "#report-export-confirm, #report-export-cancel"
        )

    def _submit(self) -> None:
        destination_input = self.query_one("#report-destination-input", Input)
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
                ReportExportOverwriteDialog(
                    locale=self.locale,
                    destination=self.destination,
                ),
                self._handle_overwrite_confirmation,
            )
            return
        self.dismiss(ReportExportRequest(destination=self.destination, force=False))

    def _handle_overwrite_confirmation(self, confirmed: bool) -> None:
        if confirmed:
            self.dismiss(ReportExportRequest(destination=self.destination, force=True))
        else:
            self.dismiss(None)

    def _refresh_destination_validation(self) -> None:
        destination_input = self.query_one("#report-destination-input", Input)
        empty = not destination_input.value.strip()
        self.query_one("#report-export-confirm", Button).disabled = empty
        self.query_one("#report-destination-validation", Static).update(
            self.locale("evidence.export.validation.destination") if empty else ""
        )

    def _refresh_hint(self) -> None:
        hint = self.query_one("#report-export-hint", Static)
        width = max(2, hint.content_region.width or self.size.width - 8)
        hint.update("\n".join(wrap_cells(self.locale("evidence.export.hint"), width)))


class ReportProfileDialog(ModalScreen[ReportProfile | None]):
    """Edit a bounded report profile with one explicit save action."""

    BINDINGS = [
        Binding("escape", "cancel", "取消"),
        Binding("ctrl+s", "save", "保存", show=False, priority=True),
    ]

    _METADATA_FIELDS = (
        "report_title",
        "course_name",
        "course_code",
        "student_name",
        "student_id",
        "instructor",
        "semester",
        "report_date",
    )

    def __init__(
        self,
        *,
        locale: Translator,
        profile: ReportProfile,
        error: str = "",
    ) -> None:
        super().__init__(name="report-profile")
        self.locale = locale
        self.profile = profile
        self.error = error

    def compose(self) -> ComposeResult:
        fields: list[object] = []
        for field_name in self._METADATA_FIELDS:
            fields.extend(
                (
                    Static(
                        self.locale(f"evidence.report_profile.{field_name}.label"),
                        classes="report-profile-field-label",
                        markup=False,
                    ),
                    Input(
                        value=getattr(self.profile, field_name),
                        id=f"report-profile-{field_name.replace('_', '-')}-input",
                    ),
                )
            )

        sections = self.profile.sections
        for index in range(MAX_REPORT_SECTIONS):
            section = sections[index] if index < len(sections) else None
            fields.append(
                Vertical(
                    Static(
                        self.locale(
                            "evidence.report_profile.section.title",
                            index=index + 1,
                        ),
                        classes="report-profile-section-title",
                        markup=False,
                    ),
                    Static(
                        self.locale("evidence.report_profile.section.heading.label"),
                        classes="report-profile-field-label",
                        markup=False,
                    ),
                    Input(
                        value=section.heading if section is not None else "",
                        id=f"report-profile-section-{index + 1}-heading-input",
                    ),
                    Static(
                        self.locale("evidence.report_profile.section.body.label"),
                        classes="report-profile-field-label",
                        markup=False,
                    ),
                    TextArea(
                        section.body if section is not None else "",
                        id=f"report-profile-section-{index + 1}-body-input",
                        soft_wrap=True,
                    ),
                    Checkbox(
                        self.locale("evidence.report_profile.section.evidence"),
                        value=section.include_evidence if section is not None else False,
                        id=f"report-profile-section-{index + 1}-evidence-input",
                    ),
                    classes="report-profile-section",
                )
            )

        yield Container(
            Vertical(
                Static(
                    self.locale("evidence.report_profile.title"),
                    id="report-profile-heading",
                    markup=False,
                ),
                Static(
                    self.locale("evidence.report_profile.hint"),
                    id="report-profile-hint",
                    markup=False,
                ),
                VerticalScroll(
                    *fields,
                    id="report-profile-fields",
                ),
                Static(id="report-profile-error", markup=False),
                Horizontal(
                    Button(
                        self.locale("evidence.report_profile.save"),
                        id="report-profile-save",
                        variant="primary",
                    ),
                    Button(
                        self.locale("evidence.report_profile.cancel"),
                        id="report-profile-cancel",
                    ),
                    id="report-profile-actions",
                ),
                id="report-profile-card",
            ),
            id="report-profile-dialog",
        )

    def on_mount(self) -> None:
        self._show_error(self.error)
        self._refresh_hint()
        self.query_one("#report-profile-report-title-input", Input).focus()

    def on_resize(self) -> None:
        self.call_after_refresh(self._refresh_hint)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "report-profile-save":
            self.action_save()
        elif event.button.id == "report-profile-cancel":
            self.action_cancel()

    def action_save(self) -> None:
        sections: list[ReportSection] = []
        for index in range(MAX_REPORT_SECTIONS):
            heading_input = self.query_one(
                f"#report-profile-section-{index + 1}-heading-input",
                Input,
            )
            body_input = self.query_one(
                f"#report-profile-section-{index + 1}-body-input",
                TextArea,
            )
            evidence_input = self.query_one(
                f"#report-profile-section-{index + 1}-evidence-input",
                Checkbox,
            )
            heading = heading_input.value
            body = body_input.text
            include_evidence = evidence_input.value
            if not heading.strip():
                if body.strip() or include_evidence:
                    self._show_error(
                        self.locale(
                            "evidence.report_profile.validation.section_heading",
                            index=index + 1,
                        )
                    )
                    heading_input.focus()
                    return
                continue
            sections.append(
                ReportSection(
                    heading=heading,
                    body=body,
                    include_evidence=include_evidence,
                )
            )

        values = {
            field_name: self.query_one(
                f"#report-profile-{field_name.replace('_', '-')}-input",
                Input,
            ).value
            for field_name in self._METADATA_FIELDS
        }
        try:
            profile = self.profile.model_copy(update={**values, "sections": tuple(sections)})
        except ValueError:
            self._show_error(self.locale("evidence.report_profile.validation.invalid"))
            return
        self.dismiss(profile)

    def action_cancel(self) -> None:
        self.dismiss(None)

    def _show_error(self, value: str) -> None:
        self.query_one("#report-profile-error", Static).update(value)

    def _refresh_hint(self) -> None:
        hint = self.query_one("#report-profile-hint", Static)
        width = max(2, hint.content_region.width or self.size.width - 8)
        hint.update("\n".join(wrap_cells(self.locale("evidence.report_profile.hint"), width)))


__all__ = [
    "ReportDestinationInput",
    "ReportExportDialog",
    "ReportExportOverwriteDialog",
    "ReportProfileDialog",
]
