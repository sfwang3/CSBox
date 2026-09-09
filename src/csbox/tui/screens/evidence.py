from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.events import Resize
from textual.screen import Screen
from textual.widgets import Button, Static

from csbox.api.repository import ApiRunRepository
from csbox.config.loader import ConfigurationError
from csbox.core.display_width import truncate_cells
from csbox.core.text_layout import wrap_cells
from csbox.evidence.exporter import (
    ReportExportError,
    ReportExportPhase,
    ReportExportRequest,
    ReportExportResult,
)
from csbox.evidence.models import (
    ApiStepSource,
    EvidenceItem,
    EvidenceSet,
    EvidenceSetSummary,
    LabCaptureSource,
)
from csbox.evidence.repository import EvidencePersistenceError, EvidenceSetRepository
from csbox.evidence.resolver import ApiStepResolver, EvidenceSourceResolver, LabCaptureResolver
from csbox.evidence.service import create_report_handoff_service
from csbox.lab.repository import SessionRepository
from csbox.locales import Translator
from csbox.report.models import ReportProfile
from csbox.report.repository import ReportProfilePersistenceError, ReportProfileRepository
from csbox.report.service import default_report_profile
from csbox.tui.dialogs.confirm import ConfirmDialog
from csbox.tui.dialogs.evidence import EvidenceItemDialog, EvidenceSetTitleDialog
from csbox.tui.dialogs.report import ReportExportDialog, ReportProfileDialog
from csbox.tui.dialogs.unavailable import UnavailableDialog
from csbox.tui.help import HelpDialog
from csbox.tui.screens.evidence_sources import EvidenceCaptureBrowserScreen
from csbox.tui.screens.report import ReportExportResultScreen

ReportExportAction = Callable[..., ReportExportResult]


class EvidenceSetsScreen(Screen[None]):
    """List independently persisted Evidence Sets using metadata only."""

    BINDINGS = (
        Binding("up", "select_previous", "上一条", show=False, priority=True),
        Binding("down", "select_next", "下一条", show=False, priority=True),
        Binding("enter", "open_selected", "打开", show=False),
        Binding("n", "create_set", "新建", show=False, priority=True),
        Binding("escape", "go_back", "返回", show=False, priority=True),
        Binding("q", "go_back", "返回", show=False, priority=True),
    )

    def __init__(
        self,
        *,
        repository: EvidenceSetRepository,
        session_repository: SessionRepository,
        locale: Translator,
        project_dir: Path | str | None = None,
        report_export_action: ReportExportAction | None = None,
        api_repository: ApiRunRepository | None = None,
    ) -> None:
        super().__init__(name="evidence-sets")
        self.repository = repository
        self.session_repository = session_repository
        self.locale = locale
        self.project_dir = Path(project_dir) if project_dir is not None else Path.cwd()
        self.report_export_action = report_export_action
        self.api_repository = api_repository or ApiRunRepository.from_cwd(self.project_dir)
        self.summaries: tuple[EvidenceSetSummary, ...] = ()
        self.selected_evidence_set_id: str | None = None
        self._load_failed = False

    def compose(self) -> ComposeResult:
        yield Vertical(
            Static(self.locale("evidence.list.title"), id="evidence-list-title", markup=False),
            VerticalScroll(
                Static(id="evidence-list", markup=False),
                id="evidence-list-scroll",
            ),
            Static(id="evidence-list-message", markup=False),
            Horizontal(
                Button(
                    self.locale("evidence.action.new_set"),
                    id="evidence-new",
                    variant="primary",
                ),
                Button(self.locale("evidence.action.open"), id="evidence-open"),
                Button(self.locale("evidence.action.back"), id="evidence-back"),
                id="evidence-list-actions",
            ),
            Static(id="evidence-list-footer", markup=False),
            id="evidence-list-layout",
        )

    def on_mount(self) -> None:
        self._reload()
        self.call_after_refresh(self._focus_primary)

    def on_screen_resume(self, event: events.ScreenResume) -> None:
        del event
        self._reload()

    def on_resize(self, event: Resize) -> None:
        del event
        self._refresh()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "evidence-new":
            self.action_create_set()
        elif event.button.id == "evidence-open":
            self.action_open_selected()
        elif event.button.id == "evidence-back":
            self.action_go_back()

    def _reload(self) -> None:
        previous = self.selected_evidence_set_id
        try:
            summaries = self.repository.list_summaries()
        except (EvidencePersistenceError, OSError, UnicodeError, ValueError):
            summaries = ()
            self._load_failed = True
        else:
            self._load_failed = False
        self.summaries = summaries
        readable = tuple(summary for summary in summaries if summary.readable)
        ids = {summary.evidence_set_id for summary in readable}
        if previous in ids:
            self.selected_evidence_set_id = previous
        else:
            self.selected_evidence_set_id = readable[0].evidence_set_id if readable else None
        self._refresh()

    @property
    def selected_summary(self) -> EvidenceSetSummary | None:
        selected = self.selected_evidence_set_id
        return next(
            (summary for summary in self.summaries if summary.evidence_set_id == selected),
            None,
        )

    def _focus_primary(self) -> None:
        target = (
            "#evidence-open"
            if any(summary.readable for summary in self.summaries)
            else "#evidence-new"
        )
        self.query_one(target, Button).focus()

    def _refresh(self) -> None:
        list_widget = self.query_one("#evidence-list", Static)
        message_widget = self.query_one("#evidence-list-message", Static)
        footer = self.query_one("#evidence-list-footer", Static)
        open_button = self.query_one("#evidence-open", Button)

        width = max(1, list_widget.content_region.width or self.size.width - 4)
        if not self.summaries:
            if self._load_failed:
                message = self.locale("evidence.list.load_error")
            else:
                message = self.locale("evidence.list.empty")
            list_widget.update(self._fit(message, width))
            message_widget.update("")
            open_button.disabled = True
        else:
            list_widget.update(self._render_rows(width))
            selected = self.selected_summary
            if not any(summary.readable for summary in self.summaries):
                message_widget.update(self.locale("evidence.list.all_unreadable"))
            else:
                message_widget.update(
                    ""
                    if selected is None or selected.readable
                    else self.locale("evidence.list.unreadable")
                )
            open_button.disabled = selected is None or not selected.readable
        footer.update(self._fit(self.locale("evidence.list.footer"), max(1, self.size.width - 4)))

    def _render_rows(self, width: int) -> str:
        lines = [self.locale("evidence.list.columns")]
        for summary in self.summaries:
            selected = summary.evidence_set_id == self.selected_evidence_set_id
            marker = ">" if selected else " "
            if summary.readable:
                updated = _format_local_time(summary.updated_at)
                row = f"{marker} {summary.title}  |  {summary.item_count} 条  |  {updated}"
            else:
                row = f"{marker} 不可读取 · {summary.evidence_set_id}  |  —  |  —"
            lines.append(truncate_cells(row, width, ellipsis="…"))
        return "\n".join(lines)

    def _fit(self, text: str, width: int) -> str:
        return "\n".join(wrap_cells(text, max(2, width)))

    def action_select_previous(self) -> None:
        self._select_by_offset(-1)

    def action_select_next(self) -> None:
        self._select_by_offset(1)

    def _select_by_offset(self, offset: int) -> None:
        readable = [summary for summary in self.summaries if summary.readable]
        if not readable:
            return
        current = next(
            (
                index
                for index, item in enumerate(readable)
                if item.evidence_set_id == self.selected_evidence_set_id
            ),
            0,
        )
        self.selected_evidence_set_id = readable[(current + offset) % len(readable)].evidence_set_id
        self._refresh()

    def action_create_set(self) -> None:
        self.app.push_screen(EvidenceSetTitleDialog(locale=self.locale), self._handle_create_title)

    def _handle_create_title(self, title: str | None) -> None:
        if title is None:
            return
        try:
            evidence_set = self.repository.create(title)
        except (EvidencePersistenceError, OSError, UnicodeError, ValueError):
            self.app.push_screen(
                EvidenceSetTitleDialog(
                    locale=self.locale,
                    initial_title=title,
                    error=self.locale("evidence.create.save_failed"),
                ),
                self._handle_create_title,
            )
            return
        self.selected_evidence_set_id = evidence_set.evidence_set_id
        self.app.push_screen(
            EvidenceSetEditorScreen(
                repository=self.repository,
                session_repository=self.session_repository,
                evidence_set=evidence_set,
                locale=self.locale,
                project_dir=self.project_dir,
                report_export_action=self.report_export_action,
                api_repository=self.api_repository,
            )
        )

    def action_open_selected(self) -> None:
        summary = self.selected_summary
        if summary is None:
            return
        if not summary.readable:
            self._show_error(self.locale("evidence.list.unreadable"))
            return
        try:
            evidence_set = self.repository.load(summary.evidence_set_id)
        except (EvidencePersistenceError, OSError, UnicodeError, ValueError) as exc:
            self._show_error(str(exc))
            return
        self.app.push_screen(
            EvidenceSetEditorScreen(
                repository=self.repository,
                session_repository=self.session_repository,
                evidence_set=evidence_set,
                locale=self.locale,
                project_dir=self.project_dir,
                report_export_action=self.report_export_action,
                api_repository=self.api_repository,
            )
        )

    def _show_error(self, message: str) -> None:
        self.app.push_screen(
            UnavailableDialog(
                title=self.locale("evidence.list.title"),
                locale=self.locale,
                message=message,
            )
        )

    def action_go_back(self) -> None:
        self.app.pop_screen()


class EvidenceSetEditorScreen(Screen[None]):
    """Edit an Evidence Set while keeping source resolution outside persistence."""

    BINDINGS = (
        Binding("up", "select_previous", "上一条", show=False, priority=True),
        Binding("down", "select_next", "下一条", show=False, priority=True),
        Binding("enter", "edit_item", "编辑", show=False),
        Binding("e", "edit_item", "编辑", show=False, priority=True),
        Binding("a", "add_item", "添加", show=False, priority=True),
        Binding("delete", "remove_item", "移除", show=False, priority=True),
        Binding("ctrl+up", "move_up", "上移", show=False, priority=True),
        Binding("ctrl+down", "move_down", "下移", show=False, priority=True),
        Binding("r", "retry_save", "重试保存", show=False, priority=True),
        Binding("p", "export_report", "导出报告材料", show=False, priority=True),
        Binding("c", "configure_report", "配置报告", show=False, priority=True),
        Binding("escape", "go_back", "返回", show=False, priority=True),
        Binding("q", "go_back", "返回", show=False, priority=True),
    )

    def __init__(
        self,
        *,
        repository: EvidenceSetRepository,
        session_repository: SessionRepository,
        evidence_set: EvidenceSet,
        locale: Translator,
        project_dir: Path | str | None = None,
        report_export_action: ReportExportAction | None = None,
        api_repository: ApiRunRepository | None = None,
    ) -> None:
        super().__init__(name="evidence-editor")
        self.repository = repository
        self.session_repository = session_repository
        self.locale = locale
        self.project_dir = Path(project_dir) if project_dir is not None else Path.cwd()
        self.report_export_action = report_export_action
        self.api_repository = api_repository or ApiRunRepository.from_cwd(self.project_dir)
        self.report_profile_repository = ReportProfileRepository.from_cwd(self.project_dir)
        self.working_set = evidence_set
        self.api_resolver = ApiStepResolver(self.api_repository)
        self.resolver = EvidenceSourceResolver(
            LabCaptureResolver(session_repository),
            self.api_resolver,
        )
        self.selected_index = 0
        self.save_failed = False
        self.status = self.locale("evidence.status.saved")
        self._report_exporting = False
        self._report_request: ReportExportRequest | None = None
        self._report_export_set: EvidenceSet | None = None
        self._report_profile: ReportProfile | None = None
        self._focus_export_on_resume = False

    @property
    def is_working(self) -> bool:
        return self._report_exporting

    def compose(self) -> ComposeResult:
        yield Vertical(
            Static(id="evidence-editor-title", markup=False),
            Horizontal(
                Static(id="evidence-items", markup=False),
                Static(id="evidence-item-detail", markup=False),
                id="evidence-editor-body",
            ),
            Static(id="evidence-editor-message", markup=False),
            Horizontal(
                Button(self.locale("evidence.action.add_capture"), id="evidence-add"),
                Button(self.locale("evidence.action.edit"), id="evidence-edit"),
                Button(self.locale("evidence.action.move_up"), id="evidence-up"),
                Button(self.locale("evidence.action.move_down"), id="evidence-down"),
                Button(self.locale("evidence.action.remove"), id="evidence-remove"),
                Button(
                    self.locale("evidence.action.export_report"),
                    id="evidence-export",
                    variant="primary",
                ),
                Button(self.locale("evidence.action.back"), id="evidence-editor-back"),
                id="evidence-editor-actions",
            ),
            Static(id="evidence-editor-footer", markup=False),
            id="evidence-editor-layout",
        )

    def on_mount(self) -> None:
        self._set_layout_class(self.size.width)
        self._refresh()
        self.call_after_refresh(self._refresh)
        self.call_after_refresh(self._focus_primary)

    def on_screen_resume(self, event: events.ScreenResume) -> None:
        del event
        if self._focus_export_on_resume:
            self._focus_export_on_resume = False
            self.call_after_refresh(self._focus_export_action)

    def on_resize(self, event: Resize) -> None:
        self._set_layout_class(event.size.width)
        self._refresh()
        self.call_after_refresh(self._refresh)

    def _set_layout_class(self, width: int) -> None:
        self.set_class(width < 120, "narrow")
        self.set_class(width >= 120, "wide")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if self._report_exporting:
            return
        actions: dict[str, Callable[[], None]] = {
            "evidence-add": self.action_add_item,
            "evidence-edit": self.action_edit_item,
            "evidence-up": self.action_move_up,
            "evidence-down": self.action_move_down,
            "evidence-remove": self.action_remove_item,
            "evidence-export": self.action_export_report,
            "evidence-editor-back": self.action_go_back,
        }
        action = actions.get(event.button.id or "")
        if action is not None:
            action()

    def _focus_primary(self) -> None:
        target = "#evidence-add" if not self.working_set.items else "#evidence-edit"
        self.query_one(target, Button).focus()

    def _refresh(self) -> None:
        title = self.query_one("#evidence-editor-title", Static)
        items = self.query_one("#evidence-items", Static)
        detail = self.query_one("#evidence-item-detail", Static)
        message = self.query_one("#evidence-editor-message", Static)
        footer = self.query_one("#evidence-editor-footer", Static)
        edit_button = self.query_one("#evidence-edit", Button)
        up_button = self.query_one("#evidence-up", Button)
        down_button = self.query_one("#evidence-down", Button)
        remove_button = self.query_one("#evidence-remove", Button)
        export_button = self.query_one("#evidence-export", Button)
        back_button = self.query_one("#evidence-editor-back", Button)

        title.update(self.working_set.title)
        width = max(1, items.content_region.width or self.size.width // 2 - 2)
        if not self.working_set.items:
            items.update(self.locale("evidence.editor.empty"))
            detail.update("")
            edit_button.disabled = True
            up_button.disabled = True
            down_button.disabled = True
            remove_button.disabled = True
        else:
            self.selected_index = min(self.selected_index, len(self.working_set.items) - 1)
            rows = [self.locale("evidence.editor.items")]
            for index, item in enumerate(self.working_set.items):
                marker = ">" if index == self.selected_index else " "
                rows.append(
                    truncate_cells(f"{marker} {index + 1:02d}  {item.title}", width, ellipsis="…")
                )
            items.update("\n".join(rows))
            detail_width = max(2, detail.content_region.width or self.size.width // 2 - 4)
            detail.update(
                self._render_detail(self.working_set.items[self.selected_index], detail_width)
            )
            edit_button.disabled = False
            up_button.disabled = self.selected_index == 0
            down_button.disabled = self.selected_index == len(self.working_set.items) - 1
            remove_button.disabled = False
        export_button.disabled = self._report_exporting
        back_button.disabled = self._report_exporting
        if self._report_exporting:
            for button in (edit_button, up_button, down_button, remove_button):
                button.disabled = True
        message.update(self.status)
        footer.update(
            self._fit(
                self.locale("evidence.editor.footer"),
                max(1, self.size.width - 4),
            )
        )

    def _render_detail(self, item: EvidenceItem, width: int) -> str:
        resolved = self.resolver.resolve(item.source)
        source_status = (
            self.locale("evidence.item.detail.source_available")
            if resolved.available
            else self.locale("evidence.item.detail.source_unavailable")
        )
        lines = [
            self.locale("evidence.item.detail.title", value=item.title),
            self.locale("evidence.item.detail.source", value=_source_text(item.source)),
            source_status,
        ]
        if isinstance(item.source, LabCaptureSource):
            lines.append(
                self.locale(
                    "evidence.item.detail.session",
                    value=resolved.session_name or "（不可用）",
                )
            )
        elif isinstance(item.source, ApiStepSource):
            lines.extend(
                (
                    self.locale(
                        "evidence.item.detail.scenario",
                        value=resolved.scenario_name or "（不可用）",
                    ),
                    self.locale(
                        "evidence.item.detail.step",
                        value=resolved.step_name or "（不可用）",
                    ),
                    self.locale(
                        "evidence.item.detail.run_status",
                        value=resolved.run_status or "（不可用）",
                    ),
                )
            )
        lines.extend(
            (
                self.locale("evidence.item.detail.caption", value=item.caption or "（空）"),
                self.locale("evidence.item.detail.note", value=item.note or "（空）"),
            )
        )
        return "\n".join(line for raw in lines for line in wrap_cells(raw, width))

    def _fit(self, text: str, width: int) -> str:
        return "\n".join(wrap_cells(text, max(2, width)))

    def action_select_previous(self) -> None:
        if self.working_set.items and not self._report_exporting:
            self.selected_index = max(0, self.selected_index - 1)
            self._refresh()

    def action_select_next(self) -> None:
        if self.working_set.items and not self._report_exporting:
            self.selected_index = min(len(self.working_set.items) - 1, self.selected_index + 1)
            self._refresh()

    def action_edit_item(self) -> None:
        if self._report_exporting:
            return
        if not self.working_set.items:
            self.status = self.locale("evidence.status.no_item")
            self._refresh()
            return
        index = self.selected_index
        item = self.working_set.items[index]
        self.app.push_screen(
            EvidenceItemDialog(
                locale=self.locale,
                title=item.title,
                caption=item.caption,
                note=item.note,
            ),
            lambda values: self._handle_item_edit(index, values),
        )

    def action_add_item(self) -> None:
        if self._report_exporting:
            return
        self.app.push_screen(
            EvidenceCaptureBrowserScreen(
                session_repository=self.session_repository,
                existing_sources=tuple(item.source for item in self.working_set.items),
                locale=self.locale,
                api_repository=self.api_repository,
            ),
            self._handle_item_add,
        )

    def action_remove_item(self) -> None:
        if self._report_exporting:
            return
        if not self.working_set.items:
            self.status = self.locale("evidence.status.no_item")
            self._refresh()
            return
        item = self.working_set.items[self.selected_index]
        self.app.push_screen(
            ConfirmDialog(
                locale=self.locale,
                title=self.locale("evidence.remove.title"),
                message=self.locale("evidence.remove.message", value=item.title),
            ),
            lambda confirmed: self._handle_remove(self.selected_index, confirmed),
        )

    def action_move_up(self) -> None:
        if self._report_exporting:
            return
        if not self.working_set.items:
            self.status = self.locale("evidence.status.no_item")
            self._refresh()
            return
        if self.selected_index == 0:
            return
        items = list(self.working_set.items)
        items[self.selected_index - 1], items[self.selected_index] = (
            items[self.selected_index],
            items[self.selected_index - 1],
        )
        self.selected_index -= 1
        self._persist_candidate(self.working_set.model_copy(update={"items": tuple(items)}))

    def action_move_down(self) -> None:
        if self._report_exporting:
            return
        if not self.working_set.items:
            self.status = self.locale("evidence.status.no_item")
            self._refresh()
            return
        if self.selected_index >= len(self.working_set.items) - 1:
            return
        items = list(self.working_set.items)
        items[self.selected_index], items[self.selected_index + 1] = (
            items[self.selected_index + 1],
            items[self.selected_index],
        )
        self.selected_index += 1
        self._persist_candidate(self.working_set.model_copy(update={"items": tuple(items)}))

    def action_retry_save(self) -> None:
        if self._report_exporting:
            return
        if not self.save_failed:
            self.status = self.locale("evidence.status.retry_unavailable")
            self._refresh()
            return
        self._persist_candidate(self.working_set)

    def action_export_report(self) -> None:
        if self._report_exporting:
            self.status = self.locale("evidence.export.status.running")
            self._refresh()
            return
        self._open_report_dialog(
            default_report_destination(
                self.project_dir,
                self.working_set.evidence_set_id,
            )
        )

    def action_configure_report(self) -> None:
        if self._report_exporting:
            return
        try:
            profile = self._load_report_profile()
        except (
            ConfigurationError,
            OSError,
            ReportProfilePersistenceError,
            UnicodeError,
            ValueError,
        ):
            self.app.push_screen(
                UnavailableDialog(
                    title=self.locale("evidence.report_profile.title"),
                    locale=self.locale,
                    message=self.locale("evidence.report_profile.load_failed"),
                )
            )
            return
        self.app.push_screen(
            ReportProfileDialog(locale=self.locale, profile=profile),
            self._handle_report_profile,
        )

    def _load_report_profile(self) -> ReportProfile:
        return self.report_profile_repository.load_or_default(
            self.working_set.evidence_set_id,
            default=default_report_profile(self.project_dir),
        )

    def _handle_report_profile(self, profile: ReportProfile | None) -> None:
        if profile is None or self._report_exporting:
            return
        try:
            self.report_profile_repository.save(
                self.working_set.evidence_set_id,
                profile,
            )
        except (
            OSError,
            ReportProfilePersistenceError,
            UnicodeError,
            ValueError,
        ):
            self.status = self.locale("evidence.report_profile.save_failed")
            self._refresh()
            self.app.push_screen(
                ReportProfileDialog(
                    locale=self.locale,
                    profile=profile,
                    error=self.locale("evidence.report_profile.save_failed"),
                ),
                self._handle_report_profile,
            )
            return
        self.status = self.locale("evidence.report_profile.saved")
        self._refresh()

    def _open_report_dialog(self, destination: Path) -> None:
        self.app.push_screen(
            ReportExportDialog(
                locale=self.locale,
                destination=destination,
            ),
            self._handle_report_request,
        )

    def _handle_report_request(self, request: ReportExportRequest | None) -> None:
        if request is None or self._report_exporting:
            return
        self._report_request = request
        self._start_report_export(request)

    def _start_report_export(self, request: ReportExportRequest) -> None:
        self._report_request = request
        if self.report_export_action is None:
            try:
                self._report_profile = self._load_report_profile()
            except (
                ConfigurationError,
                OSError,
                ReportProfilePersistenceError,
                UnicodeError,
                ValueError,
            ):
                self._report_exporting = False
                self.status = self.locale("evidence.export.status.failed")
                self._refresh()
                self._push_report_result_after_help(
                    ReportExportResultScreen(
                        locale=self.locale,
                        error=ReportExportError(
                            self.locale("evidence.report_profile.load_failed"),
                            destination=request.destination,
                            kind="profile",
                        ),
                        on_retry=lambda: self._start_report_export(request),
                        on_change_destination=lambda: self._open_report_dialog(request.destination),
                    )
                )
                return
        else:
            self._report_profile = None
        self._report_export_set = self.working_set
        self._report_exporting = True
        self._focus_export_on_resume = True
        self.status = self.locale("evidence.export.status.generating")
        self._refresh()
        self.run_worker(
            self._run_report_export(self._report_export_set, request),
            name="evidence-report-export",
            group="evidence-report-export",
            exclusive=True,
            exit_on_error=False,
        )

    async def _run_report_export(
        self,
        evidence_set: EvidenceSet | None,
        request: ReportExportRequest,
    ) -> None:
        if evidence_set is None:
            evidence_set = self.working_set
        try:
            action = self.report_export_action

            def run_export() -> ReportExportResult:
                selected = action
                if selected is None:
                    selected = create_report_handoff_service(cwd=self.project_dir).export
                    result = selected(
                        evidence_set,
                        request.destination,
                        force=request.force,
                        phase_callback=self._report_phase_from_worker,
                        report_profile=self._report_profile,
                    )
                else:
                    result = selected(
                        evidence_set,
                        request.destination,
                        force=request.force,
                        phase_callback=self._report_phase_from_worker,
                    )
                if not isinstance(result, ReportExportResult):
                    raise TypeError("report export action returned an invalid result")
                return result

            result = await asyncio.to_thread(run_export)
        except ReportExportError as error:
            self._report_exporting = False
            self.status = self.locale("evidence.export.status.failed")
            self._refresh()
            self._push_report_result_after_help(
                ReportExportResultScreen(
                    locale=self.locale,
                    error=error,
                    on_retry=lambda: self._start_report_export(request),
                    on_change_destination=lambda: self._open_report_dialog(request.destination),
                )
            )
            return
        except Exception:
            self._report_exporting = False
            self.status = self.locale("evidence.export.status.failed")
            self._refresh()
            error = ReportExportError(
                "报告材料导出失败，未生成成功结果；请检查目标目录后重试。",
                destination=request.destination,
            )
            self._push_report_result_after_help(
                ReportExportResultScreen(
                    locale=self.locale,
                    error=error,
                    on_retry=lambda: self._start_report_export(request),
                    on_change_destination=lambda: self._open_report_dialog(request.destination),
                )
            )
            return
        self._report_exporting = False
        self.status = self.locale("evidence.export.status.complete")
        self._refresh()
        self._push_report_result_after_help(
            ReportExportResultScreen(
                locale=self.locale,
                result=result,
            )
        )

    def _push_report_result_after_help(self, result: ReportExportResultScreen) -> None:
        if isinstance(self.app.screen, HelpDialog):
            self.app.set_timer(0.05, lambda: self._push_report_result_after_help(result))
            return
        self.app.push_screen(result)

    def _report_phase_from_worker(self, phase: ReportExportPhase) -> None:
        self.app.call_from_thread(self._handle_report_phase, phase)

    def _handle_report_phase(self, phase: ReportExportPhase) -> None:
        if phase is ReportExportPhase.GENERATING:
            self.status = self.locale("evidence.export.status.generating")
        elif phase is ReportExportPhase.PUBLISHING:
            self.status = self.locale("evidence.export.status.publishing")
        elif phase is ReportExportPhase.COMPLETE:
            self.status = self.locale("evidence.export.status.complete")
        elif phase is ReportExportPhase.FAILED:
            self.status = self.locale("evidence.export.status.failed")
        self._refresh()

    def _focus_export_action(self) -> None:
        button = self.query_one("#evidence-export", Button)
        if button.display and not button.disabled:
            button.focus()

    def action_go_back(self) -> None:
        if self.save_failed:
            self.status = self.locale("evidence.status.save_failed")
            self._refresh()
            return
        if self._report_exporting:
            self.status = self.locale("evidence.export.status.running")
            self._refresh()
            return
        self.app.pop_screen()

    def _handle_item_add(self, item: EvidenceItem | None) -> None:
        if item is None or self._report_exporting:
            return
        if any(
            existing.source.equality_key == item.source.equality_key
            for existing in self.working_set.items
        ):
            self.status = self.locale("evidence.browser.duplicate")
            self._refresh()
            return
        candidate = self.working_set.model_copy(update={"items": (*self.working_set.items, item)})
        self._persist_candidate(candidate)
        if self.working_set.items:
            self.selected_index = len(self.working_set.items) - 1
            self._refresh()

    def _handle_item_edit(
        self,
        index: int,
        values: tuple[str, str, str] | None,
    ) -> None:
        if values is None or self._report_exporting or index >= len(self.working_set.items):
            return
        title, caption, note = values
        item = self.working_set.items[index].model_copy(
            update={"title": title, "caption": caption, "note": note}
        )
        items = list(self.working_set.items)
        items[index] = item
        self._persist_candidate(self.working_set.model_copy(update={"items": tuple(items)}))

    def _handle_remove(self, index: int, confirmed: bool) -> None:
        if not confirmed or self._report_exporting or index >= len(self.working_set.items):
            return
        items = tuple(
            item for item_index, item in enumerate(self.working_set.items) if item_index != index
        )
        self._persist_candidate(self.working_set.model_copy(update={"items": items}))
        if self.working_set.items:
            self.selected_index = min(index, len(self.working_set.items) - 1)
        else:
            self.selected_index = 0
        self._refresh()

    def _persist_candidate(self, candidate: EvidenceSet) -> None:
        if self._report_exporting:
            return
        try:
            saved = self.repository.save(candidate)
        except (EvidencePersistenceError, OSError, UnicodeError, ValueError):
            self.working_set = candidate
            self.save_failed = True
            self.status = self.locale("evidence.status.save_failed")
        else:
            self.working_set = saved
            self.save_failed = False
            self.status = self.locale("evidence.status.saved")
        self._refresh()


def _format_local_time(value: datetime | None) -> str:
    if value is None:
        return "—"
    try:
        return value.astimezone().strftime("%Y-%m-%d %H:%M")
    except (OverflowError, OSError, ValueError):
        return "—"


def _source_text(source: object) -> str:
    if isinstance(source, LabCaptureSource):
        return f"实验记录 {source.session_id} / 关键画面 {source.capture_id}"
    if isinstance(source, ApiStepSource):
        return f"API 运行 {source.run_id} / 第 {source.step_index} 步"
    return "未知来源"


def default_report_destination(project_dir: Path | str, evidence_set_id: str) -> Path:
    """Use the same project-local, ID-based destination style as Lab export."""

    return Path(project_dir) / f"{evidence_set_id}-report"


__all__ = [
    "EvidenceSetEditorScreen",
    "EvidenceSetsScreen",
    "ReportExportAction",
    "default_report_destination",
]
