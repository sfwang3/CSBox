from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.events import Resize
from textual.screen import Screen
from textual.widgets import Static

from csbox.core.display_width import truncate_cells
from csbox.core.text_layout import wrap_cells
from csbox.evidence.models import EvidenceItem, EvidenceSource
from csbox.evidence.resolver import LabCaptureResolver
from csbox.lab.captures import CaptureStore
from csbox.lab.models import CaptureRecord
from csbox.lab.repository import SessionRepository, SessionSummary
from csbox.locales import Translator
from csbox.tui.dialogs.evidence import EvidenceItemDialog

_ADDABLE_STATUSES = {"completed", "interrupted", "failed"}


class EvidenceCaptureBrowserScreen(Screen[EvidenceItem | None]):
    """Choose one existing Lab Capture without entering the Lab lifecycle."""

    BINDINGS = (
        Binding("up", "select_previous", "上一条", show=False, priority=True),
        Binding("down", "select_next", "下一条", show=False, priority=True),
        Binding("enter", "select_current", "选择", show=False, priority=True),
        Binding("escape", "go_back", "返回", show=False, priority=True),
        Binding("q", "go_back", "返回", show=False, priority=True),
    )

    def __init__(
        self,
        *,
        session_repository: SessionRepository,
        existing_sources: tuple[EvidenceSource, ...],
        locale: Translator,
    ) -> None:
        super().__init__(name="evidence-capture-browser")
        self.session_repository = session_repository
        self.existing_source_keys = {source.equality_key for source in existing_sources}
        self.locale = locale
        self.resolver = LabCaptureResolver(session_repository)
        self.stage = "sessions"
        self.sessions: tuple[SessionSummary, ...] = ()
        self.captures: tuple[CaptureRecord, ...] = ()
        self.selected_session_index = 0
        self.selected_capture_index = 0
        self.status = ""

    def compose(self) -> ComposeResult:
        yield Vertical(
            Static(id="evidence-browser-title", markup=False),
            Static(id="evidence-browser-list", markup=False),
            Static(id="evidence-browser-message", markup=False),
            Static(id="evidence-browser-footer", markup=False),
            id="evidence-browser-layout",
        )

    def on_mount(self) -> None:
        self._load_sessions()
        self._refresh()

    def on_resize(self, event: Resize) -> None:
        del event
        self._refresh()

    def _load_sessions(self) -> None:
        try:
            self.sessions = self.resolver.list_sessions()
        except (OSError, UnicodeError, ValueError, RecursionError):
            self.sessions = ()
            self.status = self.locale("evidence.browser.sessions_unavailable")
        if self.sessions:
            self.selected_session_index = min(self.selected_session_index, len(self.sessions) - 1)
        else:
            self.selected_session_index = 0

    @property
    def selected_session(self) -> SessionSummary | None:
        if not self.sessions:
            return None
        return self.sessions[self.selected_session_index]

    @property
    def selected_capture(self) -> CaptureRecord | None:
        if not self.captures:
            return None
        return self.captures[self.selected_capture_index]

    def _refresh(self) -> None:
        title = self.query_one("#evidence-browser-title", Static)
        list_widget = self.query_one("#evidence-browser-list", Static)
        message = self.query_one("#evidence-browser-message", Static)
        footer = self.query_one("#evidence-browser-footer", Static)
        title.update(
            self.locale(
                "evidence.browser.sessions_title"
                if self.stage == "sessions"
                else "evidence.browser.captures_title"
            )
        )
        width = max(2, list_widget.content_region.width or self.size.width - 4)
        if self.stage == "sessions":
            list_widget.update(self._render_sessions(width))
            message.update(self._session_message(width))
            footer.update(
                self._fit(
                    self.locale("evidence.browser.sessions_footer"),
                    max(2, self.size.width - 4),
                )
            )
        else:
            list_widget.update(self._render_captures(width))
            message.update(self._capture_message(width))
            footer.update(
                self._fit(
                    self.locale("evidence.browser.captures_footer"),
                    max(2, self.size.width - 4),
                )
            )

    def _render_sessions(self, width: int) -> str:
        if not self.sessions:
            return self.locale("evidence.browser.empty.sessions")
        rows: list[str] = []
        for index, summary in enumerate(self.sessions):
            marker = ">" if index == self.selected_session_index else " "
            status = summary.metadata.status
            status_text = self.locale(f"evidence.browser.status.{status}")
            row = (
                f"{marker} {summary.metadata.experiment_name}  |  "
                f"{summary.capture_count} 条  |  {status_text}"
            )
            rows.append(truncate_cells(row, width, ellipsis="…"))
        return "\n".join(rows)

    def _render_captures(self, width: int) -> str:
        if not self.captures:
            return self.locale("evidence.browser.empty.captures")
        rows: list[str] = []
        for index, capture in enumerate(self.captures):
            marker = ">" if index == self.selected_capture_index else " "
            row = f"{marker} {capture.title or '（无标题）'}  |  t={capture.timestamp:g}s"
            rows.append(truncate_cells(row, width, ellipsis="…"))
        return "\n".join(rows)

    def _session_message(self, width: int) -> str:
        if self.status:
            return self._fit(self.status, width)
        selected = self.selected_session
        if selected is None:
            return self._fit(self.locale("evidence.browser.empty.sessions_next"), width)
        if selected.metadata.status not in _ADDABLE_STATUSES:
            return self._fit(self.locale("evidence.browser.running_not_addable"), width)
        return ""

    def _capture_message(self, width: int) -> str:
        if self.status:
            return self._fit(self.status, width)
        if not self.captures:
            return self._fit(self.locale("evidence.browser.empty.captures_next"), width)
        return ""

    def _fit(self, text: str, width: int) -> str:
        return "\n".join(wrap_cells(text, max(2, width)))

    def action_select_previous(self) -> None:
        self._move_selection(-1)

    def action_select_next(self) -> None:
        self._move_selection(1)

    def _move_selection(self, offset: int) -> None:
        values = self.sessions if self.stage == "sessions" else self.captures
        if not values:
            return
        if self.stage == "sessions":
            self.selected_session_index = (self.selected_session_index + offset) % len(
                self.sessions
            )
        else:
            self.selected_capture_index = (self.selected_capture_index + offset) % len(
                self.captures
            )
        self.status = ""
        self._refresh()

    def action_select_current(self) -> None:
        if self.stage == "sessions":
            self._open_selected_session()
        else:
            self._choose_selected_capture()

    def _open_selected_session(self) -> None:
        selected = self.selected_session
        if selected is None:
            return
        if selected.metadata.status not in _ADDABLE_STATUSES:
            self.status = self.locale("evidence.browser.running_not_addable")
            self._refresh()
            return
        try:
            result = CaptureStore(selected.paths.captures).load()
        except (OSError, UnicodeError, ValueError, RecursionError):
            self.captures = ()
            self.status = self.locale("evidence.browser.captures_unavailable")
        else:
            self.captures = result.captures
            self.status = (
                self.locale("evidence.browser.captures_unavailable")
                if result.warnings and not result.captures
                else ""
            )
        self.selected_capture_index = 0
        self.stage = "captures"
        self._refresh()

    def _choose_selected_capture(self) -> None:
        selected_session = self.selected_session
        selected_capture = self.selected_capture
        if selected_session is None or selected_capture is None:
            return
        source = EvidenceSource(
            source_type="lab_capture",
            session_id=selected_session.metadata.session_id,
            capture_id=selected_capture.capture_id,
        )
        if source.equality_key in self.existing_source_keys:
            self.status = self.locale("evidence.browser.duplicate")
            self._refresh()
            return
        if selected_capture.title.strip():
            self._return_item(
                EvidenceItem(
                    source=source,
                    title=selected_capture.title,
                )
            )
            return
        self.app.push_screen(
            EvidenceItemDialog(
                locale=self.locale,
                heading=self.locale("evidence.item.add"),
            ),
            lambda values: self._handle_blank_capture_title(source, values),
        )

    def _handle_blank_capture_title(
        self,
        source: EvidenceSource,
        values: tuple[str, str, str] | None,
    ) -> None:
        if values is None:
            return
        title, caption, note = values
        self._return_item(
            EvidenceItem(
                source=source,
                title=title,
                caption=caption,
                note=note,
            )
        )

    def _return_item(self, item: EvidenceItem) -> None:
        self.dismiss(item)

    def action_go_back(self) -> None:
        if self.stage == "captures":
            self.stage = "sessions"
            self.captures = ()
            self.selected_capture_index = 0
            self.status = ""
            self._refresh()
            return
        self.dismiss(None)


__all__ = ["EvidenceCaptureBrowserScreen"]
