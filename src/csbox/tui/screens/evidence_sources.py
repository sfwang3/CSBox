from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.events import Resize
from textual.screen import Screen
from textual.widgets import Static

from csbox.api.errors import ApiPersistenceError
from csbox.api.repository import ApiRunRepository, ApiRunSummary
from csbox.core.display_width import truncate_cells
from csbox.core.text_layout import wrap_cells
from csbox.evidence.models import EvidenceItem, EvidenceSource
from csbox.evidence.resolver import ApiStepResolver, ApiStepSummary, LabCaptureResolver
from csbox.lab.captures import CaptureStore
from csbox.lab.models import CaptureRecord
from csbox.lab.repository import SessionRepository, SessionSummary
from csbox.locales import Translator
from csbox.tui.dialogs.evidence import EvidenceItemDialog

_ADDABLE_STATUSES = {"completed", "interrupted", "failed"}
_SOURCE_OPTIONS = ("lab", "api")


class EvidenceCaptureBrowserScreen(Screen[EvidenceItem | None]):
    """Choose one existing Lab Capture or persisted API step."""

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
        api_repository: ApiRunRepository | None = None,
    ) -> None:
        super().__init__(name="evidence-capture-browser")
        self.session_repository = session_repository
        self.api_repository = api_repository or ApiRunRepository(
            session_repository.root.parent / "api" / "runs"
        )
        self.existing_source_keys = {source.equality_key for source in existing_sources}
        self.locale = locale
        self.lab_resolver = LabCaptureResolver(session_repository)
        self.api_resolver = ApiStepResolver(self.api_repository)
        self.stage = "sources"
        self.source_index = 0
        self.sessions: tuple[SessionSummary, ...] = ()
        self.captures: tuple[CaptureRecord, ...] = ()
        self.api_runs: tuple[ApiRunSummary, ...] = ()
        self.api_steps: tuple[ApiStepSummary, ...] = ()
        self.selected_session_index = 0
        self.selected_capture_index = 0
        self.selected_api_run_index = 0
        self.selected_api_step_index = 0
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
        self._refresh()

    def on_resize(self, event: Resize) -> None:
        del event
        self._refresh()

    def _load_sessions(self) -> None:
        try:
            self.sessions = self.lab_resolver.list_sessions()
        except (OSError, UnicodeError, ValueError, RecursionError):
            self.sessions = ()
            self.status = self.locale("evidence.browser.sessions_unavailable")
        if self.sessions:
            self.selected_session_index = min(self.selected_session_index, len(self.sessions) - 1)
        else:
            self.selected_session_index = 0

    def _load_api_runs(self) -> None:
        try:
            self.api_runs = self.api_resolver.list_runs()
        except (ApiPersistenceError, OSError, UnicodeError, ValueError, RecursionError):
            self.api_runs = ()
            self.status = self.locale("evidence.browser.api.runs_unavailable")
        if self.api_runs:
            self.selected_api_run_index = min(
                self.selected_api_run_index,
                len(self.api_runs) - 1,
            )
        else:
            self.selected_api_run_index = 0

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

    @property
    def selected_api_run(self) -> ApiRunSummary | None:
        if not self.api_runs:
            return None
        return self.api_runs[self.selected_api_run_index]

    @property
    def selected_api_step(self) -> ApiStepSummary | None:
        if not self.api_steps:
            return None
        return self.api_steps[self.selected_api_step_index]

    def _refresh(self) -> None:
        title = self.query_one("#evidence-browser-title", Static)
        list_widget = self.query_one("#evidence-browser-list", Static)
        message = self.query_one("#evidence-browser-message", Static)
        footer = self.query_one("#evidence-browser-footer", Static)
        width = max(2, list_widget.content_region.width or self.size.width - 4)

        if self.stage == "sources":
            title.update(self.locale("evidence.browser.sources_title"))
            list_widget.update(self._render_sources(width))
            message.update(self._fit(self.status, width) if self.status else "")
            footer.update(self._fit_footer("evidence.browser.sources_footer"))
        elif self.stage == "sessions":
            title.update(self.locale("evidence.browser.sessions_title"))
            list_widget.update(self._render_sessions(width))
            message.update(self._session_message(width))
            footer.update(self._fit_footer("evidence.browser.sessions_footer"))
        elif self.stage == "captures":
            title.update(self.locale("evidence.browser.captures_title"))
            list_widget.update(self._render_captures(width))
            message.update(self._capture_message(width))
            footer.update(self._fit_footer("evidence.browser.captures_footer"))
        elif self.stage == "api_runs":
            title.update(self.locale("evidence.browser.api.runs_title"))
            list_widget.update(self._render_api_runs(width))
            message.update(self._api_runs_message(width))
            footer.update(self._fit_footer("evidence.browser.api.runs_footer"))
        else:
            title.update(self.locale("evidence.browser.api.steps_title"))
            list_widget.update(self._render_api_steps(width))
            message.update(self._api_steps_message(width))
            footer.update(self._fit_footer("evidence.browser.api.steps_footer"))

    def _fit_footer(self, key: str) -> str:
        return self._fit(self.locale(key), max(2, self.size.width - 4))

    def _render_sources(self, width: int) -> str:
        labels = (
            self.locale("evidence.browser.sources.lab"),
            self.locale("evidence.browser.sources.api"),
        )
        return "\n".join(
            truncate_cells(
                f"{'> ' if index == self.source_index else '  '}{label}",
                width,
                ellipsis="…",
            )
            for index, label in enumerate(labels)
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

    def _render_api_runs(self, width: int) -> str:
        if not self.api_runs:
            return self.locale("evidence.browser.empty.api_runs")
        rows: list[str] = []
        for index, summary in enumerate(self.api_runs):
            marker = ">" if index == self.selected_api_run_index else " "
            status = self._api_status(summary.status)
            started = _format_run_time(summary.started_at)
            row = f"{marker} {summary.scenario_name}  |  {status}  |  {started}"
            rows.append(truncate_cells(row, width, ellipsis="…"))
        return "\n".join(rows)

    def _render_api_steps(self, width: int) -> str:
        if not self.api_steps:
            return self.locale("evidence.browser.empty.api_steps")
        rows: list[str] = []
        for index, summary in enumerate(self.api_steps):
            marker = ">" if index == self.selected_api_step_index else " "
            status = self._api_status(summary.step_status or summary.run_status)
            row = f"{marker} {summary.step_name}  |  {status}"
            rows.append(truncate_cells(row, width, ellipsis="…"))
        return "\n".join(rows)

    def _api_status(self, status: str | None) -> str:
        key = (status or "unknown").lower()
        return self.locale(f"evidence.browser.api.status.{key}")

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

    def _api_runs_message(self, width: int) -> str:
        if self.status:
            return self._fit(self.status, width)
        if not self.api_runs:
            return self._fit(self.locale("evidence.browser.empty.api_runs_next"), width)
        return ""

    def _api_steps_message(self, width: int) -> str:
        if self.status:
            return self._fit(self.status, width)
        if not self.api_steps:
            return self._fit(self.locale("evidence.browser.empty.api_steps_next"), width)
        return ""

    def _fit(self, text: str, width: int) -> str:
        return "\n".join(wrap_cells(text, max(2, width)))

    def action_select_previous(self) -> None:
        self._move_selection(-1)

    def action_select_next(self) -> None:
        self._move_selection(1)

    def _move_selection(self, offset: int) -> None:
        if self.stage == "sources":
            self.source_index = (self.source_index + offset) % len(_SOURCE_OPTIONS)
            self.status = ""
            self._refresh()
            return
        if self.stage == "sessions":
            if not self.sessions:
                return
            self.selected_session_index = (self.selected_session_index + offset) % len(
                self.sessions
            )
        elif self.stage == "captures":
            if not self.captures:
                return
            self.selected_capture_index = (self.selected_capture_index + offset) % len(
                self.captures
            )
        elif self.stage == "api_runs":
            if not self.api_runs:
                return
            self.selected_api_run_index = (self.selected_api_run_index + offset) % len(
                self.api_runs
            )
        else:
            if not self.api_steps:
                return
            self.selected_api_step_index = (self.selected_api_step_index + offset) % len(
                self.api_steps
            )
        self.status = ""
        self._refresh()

    def action_select_current(self) -> None:
        if self.stage == "sources":
            self._open_selected_source()
        elif self.stage == "sessions":
            self._open_selected_session()
        elif self.stage == "captures":
            self._choose_selected_capture()
        elif self.stage == "api_runs":
            self._open_selected_api_run()
        else:
            self._choose_selected_api_step()

    def _open_selected_source(self) -> None:
        self.status = ""
        if _SOURCE_OPTIONS[self.source_index] == "lab":
            self._load_sessions()
            # Keep the established one-session keyboard path compact while
            # retaining the explicit source choice for mixed-source sets.
            if len(self.sessions) == 1 and self.sessions[0].metadata.status in _ADDABLE_STATUSES:
                self._open_selected_session()
                return
            self.stage = "sessions"
        else:
            self._load_api_runs()
            self.stage = "api_runs"
        self._refresh()

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

    def _open_selected_api_run(self) -> None:
        selected = self.selected_api_run
        if selected is None:
            return
        self.api_steps = self.api_resolver.list_steps(selected.id)
        self.selected_api_step_index = 0
        self.status = ""
        self.stage = "api_steps"
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

    def _choose_selected_api_step(self) -> None:
        selected = self.selected_api_step
        if selected is None:
            return
        source = selected.source
        if source.equality_key in self.existing_source_keys:
            self.status = self.locale("evidence.browser.duplicate")
            self._refresh()
            return
        title = selected.step_name.strip() or self.locale(
            "evidence.browser.api.step_default_title",
            index=source.step_index,
        )
        self._return_item(EvidenceItem(source=source, title=title))

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
        if self.stage == "sessions" or self.stage == "api_runs":
            self.dismiss(None)
            return
        if self.stage == "api_steps":
            self.stage = "api_runs"
            self.api_steps = ()
            self.selected_api_step_index = 0
            self.status = ""
            self._refresh()
            return
        self.dismiss(None)


def _format_run_time(value: object) -> str:
    try:
        return value.astimezone().strftime("%Y-%m-%d %H:%M")
    except (AttributeError, OverflowError, OSError, ValueError):
        return "—"


__all__ = ["EvidenceCaptureBrowserScreen"]
