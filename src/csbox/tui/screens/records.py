"""Lightweight Records workflow backed by Lab session summaries."""

from __future__ import annotations

import asyncio
import unicodedata
from collections.abc import Callable
from datetime import datetime, tzinfo
from pathlib import Path, PurePosixPath, PureWindowsPath

from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.events import Resize
from textual.screen import Screen
from textual.widgets import Button, Static

from csbox.api.redaction import Redactor
from csbox.core.display_width import display_width, truncate_cells
from csbox.lab.exporter import LabExportError, LabExportResult
from csbox.lab.repository import SessionRepository, SessionSummary
from csbox.lab.service import create_lab_service
from csbox.locales import Translator
from csbox.tui.dialogs.export import ExportDialog, ExportRequest
from csbox.tui.dialogs.lab_start import LabStartDialog
from csbox.tui.help import HelpDialog
from csbox.tui.lab_workflow import LabStartRequest, ShellOption
from csbox.tui.screens.export import ExportResultScreen
from csbox.tui.screens.review import ReviewController, ReviewScreen

_RECORDS_REDACTOR = Redactor.with_configured_values(())
_ACTIVE_STATUSES = {"starting", "running"}
_STATUS_LABELS = {
    "starting": "录制中",
    "running": "录制中",
    "completed": "已完成",
    "interrupted": "已中断",
    "failed": "失败",
}
_SHELL_LABELS = {
    "powershell_7": "PowerShell 7",
    "powershell_51": "Windows PowerShell 5.1",
    "bash": "Bash",
    "zsh": "Zsh",
}
ExportAction = Callable[..., LabExportResult]


def status_display_name(status: str) -> str:
    """Map persisted lifecycle states to beginner-facing Chinese labels."""

    return _STATUS_LABELS.get(status, "旧记录状态")


def shell_display_name(shell: str) -> str:
    """Map known profiles and safely reduce legacy executable paths to a basename."""

    normalized = shell.strip().strip("\"'")
    known = _SHELL_LABELS.get(normalized.casefold())
    if known is not None:
        return known
    if not normalized:
        return "旧记录 Shell"
    basename = (
        PureWindowsPath(normalized).name if "\\" in normalized else PurePosixPath(normalized).name
    )
    safe_name = _safe_text(basename)
    return safe_name or "旧记录 Shell"


def format_local_time(value: datetime, *, local_timezone: tzinfo | None = None) -> str:
    """Render a persisted UTC timestamp in local time without changing storage semantics."""

    try:
        local_value = (
            value.astimezone(local_timezone) if local_timezone is not None else value.astimezone()
        )
        return local_value.strftime("%Y-%m-%d %H:%M")
    except (OverflowError, OSError, ValueError):
        return "旧记录时间"


class RecordsScreen(Screen[None]):
    """Choose one Lab session and open the established Review workflow."""

    BINDINGS = (
        Binding("up", "select_previous", "上一条", show=False, priority=True),
        Binding("down", "select_next", "下一条", show=False, priority=True),
        Binding("enter", "activate_selected", "打开", show=False, priority=True),
        Binding("e", "export_selected", "导出", show=False, priority=True),
        Binding("escape", "go_back", "返回", show=False, priority=True),
        Binding("q", "go_back", "返回", show=False, priority=True),
    )

    def __init__(
        self,
        *,
        repository: SessionRepository,
        locale: Translator,
        project_dir: Path | str,
        shell_options: tuple[ShellOption, ...] = (),
        shell_error: str | None = None,
        export_action: ExportAction | None = None,
    ) -> None:
        super().__init__(name="records")
        self.repository = repository
        self.locale = locale
        self.project_dir = Path(project_dir)
        self.shell_options = shell_options
        self.shell_error = shell_error
        self.export_action = export_action
        self.summaries: tuple[SessionSummary, ...] = ()
        self.selected_session_id: str | None = None
        self.active_pane = "sessions"
        self.is_wide = False
        self._load_failed = False
        self._pending_refresh_id: str | None = None
        self._saved_focus_id: str | None = None
        self._saved_scroll = (0.0, 0.0)
        self._exporting = False
        self._focus_export_on_resume = False

    @property
    def selected_summary(self) -> SessionSummary | None:
        selected_id = self.selected_session_id
        return next(
            (summary for summary in self.summaries if summary.metadata.session_id == selected_id),
            None,
        )

    @property
    def is_working(self) -> bool:
        return self._exporting

    def compose(self) -> ComposeResult:
        yield Vertical(
            Static(self.locale("records.title"), id="records-title", markup=False),
            Horizontal(
                Static(id="records-list", markup=False),
                Static(id="records-detail", markup=False),
                id="records-body",
            ),
            Static(id="records-narrow", markup=False),
            Static(id="records-message", markup=False),
            Horizontal(
                Button("查看回放", id="records-review"),
                Button("导出材料", id="records-export"),
                Button("开始实验", id="records-start", variant="primary"),
                id="records-actions",
            ),
            Static(id="records-footer", markup=False),
            id="records-layout",
        )

    def on_mount(self) -> None:
        self._set_layout(self.size.width >= 120)
        try:
            self.summaries = self.repository.list_summaries()
        except (OSError, UnicodeError, ValueError):
            self.summaries = ()
            self._load_failed = True
        self.selected_session_id = self.summaries[0].metadata.session_id if self.summaries else None
        self._refresh()
        self.call_after_refresh(self._focus_primary_action)

    def on_resize(self, event: Resize) -> None:
        self._set_layout(event.size.width >= 120)
        self.call_after_refresh(self._refresh)

    def on_screen_resume(self, event: events.ScreenResume) -> None:
        del event
        if self._focus_export_on_resume:
            self._focus_export_on_resume = False
            self.call_after_refresh(self._focus_export_action)
        session_id = self._pending_refresh_id
        if session_id is None:
            return
        self._pending_refresh_id = None
        self._refresh_one_summary(session_id)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "records-review":
            self.action_open_review()
        elif event.button.id == "records-export":
            self.action_export_selected()
        elif event.button.id == "records-start":
            self.action_start_lab()

    def _set_layout(self, wide: bool) -> None:
        self.is_wide = wide
        self.set_class(wide, "wide")
        self.set_class(not wide, "narrow")

    def _refresh(self) -> None:
        list_panel = self.query_one("#records-list", Static)
        detail_panel = self.query_one("#records-detail", Static)
        narrow_panel = self.query_one("#records-narrow", Static)
        message = self.query_one("#records-message", Static)
        review_button = self.query_one("#records-review", Button)
        export_button = self.query_one("#records-export", Button)
        start_button = self.query_one("#records-start", Button)
        footer = self.query_one("#records-footer", Static)

        if not self.summaries:
            empty = self.locale("records.empty.load" if self._load_failed else "records.empty.none")
            list_panel.update(self._fit_lines(empty, self._content_width(list_panel, 44)))
            detail_panel.update("")
            narrow_panel.update(self._fit_lines(empty, self._content_width(narrow_panel, 72)))
            message.update("")
            review_button.display = False
            export_button.display = False
            start_button.display = True
            footer.update(self._fit_footer(self.locale("records.footer.empty")))
            return

        list_panel.update(self._render_wide_list(self._content_width(list_panel, 42)))
        detail_panel.update(self._render_detail(self._content_width(detail_panel, 42)))
        narrow_panel.update(self._render_narrow_list(self._content_width(narrow_panel, 72)))
        selected = self.selected_summary
        is_active = selected is not None and selected.metadata.status in _ACTIVE_STATUSES
        review_button.display = True
        review_button.disabled = is_active or self._exporting
        export_button.display = True
        export_button.disabled = is_active or self._exporting
        start_button.display = False
        message.update(
            "正在导出…" if self._exporting else ("实验结束后可回看和导出" if is_active else "")
        )
        footer.update(self._fit_footer(self.locale("records.footer")))
        self.call_after_refresh(self._keep_selection_visible)

    def _render_wide_list(self, width: int) -> str:
        lines = ["实验记录"]
        for summary in self.summaries:
            selected = summary.metadata.session_id == self.selected_session_id
            marker = ">" if selected else " "
            name_width = max(1, width - display_width(marker) - 1)
            name = truncate_cells(
                _safe_text(summary.metadata.experiment_name), name_width, ellipsis="…"
            )
            lines.append(f"{marker} {name}")
            context = (
                f"  {status_display_name(summary.metadata.status)} · "
                f"{shell_display_name(summary.metadata.shell)} · 关键画面 {summary.capture_count}"
            )
            lines.append(truncate_cells(context, width, ellipsis="…"))
        return "\n".join(lines)

    def _render_detail(self, width: int) -> str:
        summary = self.selected_summary
        if summary is None:
            return ""
        metadata = summary.metadata
        ended = format_local_time(metadata.ended_at) if metadata.ended_at is not None else "—"
        lines = [
            "实验详情",
            status_display_name(metadata.status),
            shell_display_name(metadata.shell),
            f"关键画面 {summary.capture_count}",
            f"开始 {format_local_time(metadata.started_at)}",
            f"结束 {ended}",
        ]
        if metadata.status in _ACTIVE_STATUSES:
            lines.append("实验结束后可回看和导出")
        return "\n".join(truncate_cells(line, width, ellipsis="…") for line in lines)

    def _render_narrow_list(self, width: int) -> str:
        lines: list[str] = []
        for index, summary in enumerate(self.summaries):
            if index:
                lines.append("")
            selected = summary.metadata.session_id == self.selected_session_id
            marker = ">" if selected else " "
            name_width = max(1, width - display_width(marker) - 1)
            name = truncate_cells(
                _safe_text(summary.metadata.experiment_name), name_width, ellipsis="…"
            )
            lines.append(f"{marker} {name}")
            lines.append(
                truncate_cells(
                    f"  {status_display_name(summary.metadata.status)} · "
                    f"{shell_display_name(summary.metadata.shell)}",
                    width,
                    ellipsis="…",
                )
            )
            lines.append(
                truncate_cells(
                    f"  关键画面 {summary.capture_count} · "
                    f"{format_local_time(summary.metadata.started_at)}",
                    width,
                    ellipsis="…",
                )
            )
        return "\n".join(lines)

    def _content_width(self, widget: Static, fallback: int) -> int:
        return max(1, widget.content_region.width or fallback)

    def _fit_lines(self, value: str, width: int) -> str:
        return "\n".join(truncate_cells(line, width, ellipsis="…") for line in value.splitlines())

    def _fit_footer(self, value: str) -> str:
        footer = self.query_one("#records-footer", Static)
        return truncate_cells(
            value,
            max(1, footer.content_region.width or self.size.width - 6),
            ellipsis="…",
        )

    def _focus_primary_action(self) -> None:
        selector = "#records-review" if self.summaries else "#records-start"
        button = self.query_one(selector, Button)
        if not button.disabled:
            button.focus()

    def _keep_selection_visible(self) -> None:
        if self.selected_session_id is None:
            return
        index = self._selected_index()
        if index is None:
            return
        list_panel = self.query_one("#records-list", Static)
        narrow_panel = self.query_one("#records-narrow", Static)
        list_panel.scroll_to(y=max(0, index * 2), animate=False, force=True, immediate=True)
        narrow_panel.scroll_to(y=max(0, index * 4), animate=False, force=True, immediate=True)

    def _selected_index(self) -> int | None:
        return next(
            (
                index
                for index, summary in enumerate(self.summaries)
                if summary.metadata.session_id == self.selected_session_id
            ),
            None,
        )

    def _move_selection(self, delta: int) -> None:
        if not self.summaries or self._exporting:
            return
        index = self._selected_index()
        current = 0 if index is None else index
        target = min(max(0, current + delta), len(self.summaries) - 1)
        self.selected_session_id = self.summaries[target].metadata.session_id
        self._refresh()

    def action_select_previous(self) -> None:
        self._move_selection(-1)

    def action_select_next(self) -> None:
        self._move_selection(1)

    def action_activate_selected(self) -> None:
        if self.summaries:
            focused_id = self.focused.id if self.focused is not None else None
            if focused_id == "records-export":
                self.action_export_selected()
            else:
                self.action_open_review()
        else:
            self.action_start_lab()

    def action_export_selected(self) -> None:
        if self._exporting:
            return
        summary = self.selected_summary
        if summary is None:
            return
        if summary.metadata.status in _ACTIVE_STATUSES:
            self.query_one("#records-message", Static).update("实验结束后可回看和导出")
            return
        destination = default_export_destination(
            self.project_dir,
            summary.metadata.session_id,
        )
        self.app.push_screen(
            ExportDialog(
                locale=self.locale,
                summary=summary,
                destination=destination,
            ),
            self._handle_export_request,
        )

    def _handle_export_request(self, request: ExportRequest | None) -> None:
        if request is None or self._exporting:
            return
        self._exporting = True
        self._refresh()
        self.run_worker(
            self._run_export(request),
            name="lab-export",
            group="lab-export",
            exclusive=True,
            exit_on_error=False,
        )

    async def _run_export(self, request: ExportRequest) -> None:
        try:
            action = self.export_action
            if action is None:
                action = create_lab_service(cwd=self.project_dir).export
            result = await asyncio.to_thread(
                action,
                request.session_id,
                request.destination,
                theme=request.theme,
                force=request.force,
            )
            if not isinstance(result, LabExportResult):
                raise TypeError("export action returned an invalid result")
        except Exception as error:
            self._exporting = False
            self._refresh()
            self._focus_export_on_resume = True
            self._push_result_after_help(
                ExportResultScreen(
                    failure_message=_safe_export_failure_message(error),
                    on_retry=self.action_export_selected,
                )
            )
            return
        self._exporting = False
        self._refresh()
        self._focus_export_on_resume = True
        self._push_result_after_help(ExportResultScreen(result=result))

    def _push_result_after_help(self, result: ExportResultScreen) -> None:
        if isinstance(self.app.screen, HelpDialog):
            self.app.set_timer(0.05, lambda: self._push_result_after_help(result))
            return
        self.app.push_screen(result)

    def _focus_export_action(self) -> None:
        button = self.query_one("#records-export", Button)
        if button.display and not button.disabled:
            button.focus()

    def action_open_review(self) -> None:
        if self._exporting:
            self.query_one("#records-message", Static).update("正在导出，请稍候…")
            return
        summary = self.selected_summary
        if summary is None:
            return
        if summary.metadata.status in _ACTIVE_STATUSES:
            self.query_one("#records-message", Static).update("实验结束后可回看")
            return
        try:
            controller = ReviewController.from_session(summary.paths)
        except Exception:
            self.query_one("#records-message", Static).update(
                "暂时无法打开回放，请返回后选择其他记录。"
            )
            return
        self._pending_refresh_id = summary.metadata.session_id
        self._save_view_state()
        self.app.push_screen(ReviewScreen(controller=controller, locale=self.locale))

    def action_start_lab(self) -> None:
        if self._exporting:
            return
        self.app.push_screen(
            LabStartDialog(
                locale=self.locale,
                project_dir=self.project_dir,
                shell_options=self.shell_options,
                shell_error=self.shell_error,
            ),
            self._handle_start_request,
        )

    def _handle_start_request(self, request: LabStartRequest | None) -> None:
        if request is not None:
            self.app.exit(request)

    def _save_view_state(self) -> None:
        self._saved_focus_id = self.focused.id if self.focused is not None else None
        self._saved_scroll = (
            float(self.query_one("#records-list", Static).scroll_y),
            float(self.query_one("#records-narrow", Static).scroll_y),
        )

    def _refresh_one_summary(self, session_id: str) -> None:
        original_index = next(
            (
                index
                for index, summary in enumerate(self.summaries)
                if summary.metadata.session_id == session_id
            ),
            0,
        )
        try:
            refreshed = self.repository.read_summary(session_id)
        except (OSError, UnicodeError, ValueError):
            refreshed = None
        summaries = [
            summary for summary in self.summaries if summary.metadata.session_id != session_id
        ]
        if refreshed is not None:
            summaries.append(refreshed)
        summaries.sort(key=lambda item: item.metadata.session_id)
        summaries.sort(key=lambda item: item.metadata.started_at, reverse=True)
        self.summaries = tuple(summaries)
        if refreshed is not None:
            self.selected_session_id = session_id
        elif self.summaries:
            fallback_index = min(original_index, len(self.summaries) - 1)
            self.selected_session_id = self.summaries[fallback_index].metadata.session_id
        else:
            self.selected_session_id = None
        self._refresh()
        self.call_after_refresh(self._restore_view_state)

    def _restore_view_state(self) -> None:
        list_scroll, narrow_scroll = self._saved_scroll
        self.query_one("#records-list", Static).scroll_to(
            y=list_scroll, animate=False, force=True, immediate=True
        )
        self.query_one("#records-narrow", Static).scroll_to(
            y=narrow_scroll, animate=False, force=True, immediate=True
        )
        if self._saved_focus_id:
            try:
                button = self.query_one(f"#{self._saved_focus_id}", Button)
            except Exception:
                button = None
            if button is not None and button.display and not button.disabled:
                button.focus()
                return
        self._focus_primary_action()

    def action_go_back(self) -> None:
        if self._exporting:
            self.query_one("#records-message", Static).update("正在导出，请稍候…")
            return
        self.app.pop_screen()


def _safe_text(value: str) -> str:
    redacted = _RECORDS_REDACTOR.text(value)
    cleaned = "".join(
        " " if unicodedata.category(character).startswith("C") else character
        for character in redacted
    )
    return " ".join(cleaned.split())


def default_export_destination(project_dir: Path | str, session_id: str) -> Path:
    """Mirror LabService.export's established default destination without changing its CLI."""

    return Path(project_dir) / f"{session_id}-evidence"


def _safe_export_failure_message(error: Exception) -> str:
    if isinstance(error, PermissionError):
        return "无法写入导出目录。请检查目录权限后重试。"
    if isinstance(error, (FileNotFoundError, NotADirectoryError)):
        return "导出位置不可用。请检查目标路径后重试。"
    if isinstance(error, LabExportError):
        detail = str(error)
        if "符号链接" in detail:
            return "目标路径不安全。请选择普通本地目录后重试。"
        if "已存在" in detail or "被创建" in detail or "不可安全使用" in detail:
            return "目标目录已发生变化。请刷新已有导出或选择其他目录。"
        if "不是目录" in detail:
            return "导出位置不是目录。请检查目标路径后重试。"
    return "导出过程中出现问题。请检查实验记录、字体和目标目录后重试。"


__all__ = [
    "RecordsScreen",
    "default_export_destination",
    "ExportAction",
    "format_local_time",
    "shell_display_name",
    "status_display_name",
]
