from __future__ import annotations

from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.events import Resize
from textual.screen import Screen
from textual.widgets import Static

from csbox.core.display_width import display_width, truncate_cells
from csbox.lab.captures import CaptureStore
from csbox.lab.models import CaptureRecord, SessionMetadata, SessionPaths
from csbox.lab.replay import ReplayService
from csbox.lab.screen import TerminalCell, TerminalSnapshot
from csbox.locales import Translator
from csbox.tui.dialogs.capture_title import CaptureTitleDialog
from csbox.tui.dialogs.confirm import ConfirmDialog
from csbox.tui.keymap import REVIEW_BINDINGS
from csbox.tui.widgets.review import (
    ReviewCaptureList,
    ReviewFooter,
    ReviewTerminal,
    ReviewTimeline,
)


@dataclass(frozen=True, slots=True)
class ReviewView:
    snapshot: TerminalSnapshot
    captures: tuple[CaptureRecord, ...]
    current_time: float
    duration: float
    playing: bool
    selected_capture: int


class ReviewController:
    """Own review use cases; Textual only renders its immutable view."""

    def __init__(
        self,
        session: SessionPaths,
        replay: ReplayService,
        captures: CaptureStore,
        *,
        cwd: Path,
    ) -> None:
        self.session = session
        self.replay = replay
        self.capture_store = captures
        self.cwd = cwd
        self.current_time = 0.0
        self.playing = False
        self.selected_capture = 0

    @classmethod
    def from_session(cls, session: SessionPaths) -> ReviewController:
        cwd = session.root
        if session.metadata.is_file():
            with suppress(OSError, UnicodeError, ValueError):
                cwd = SessionMetadata.model_validate_json(
                    session.metadata.read_text(encoding="utf-8")
                ).cwd
        return cls(
            session,
            ReplayService(session.cast),
            CaptureStore(session.captures),
            cwd=cwd,
        )

    @property
    def duration(self) -> float:
        return self.replay.duration

    @property
    def captures(self) -> tuple[CaptureRecord, ...]:
        return self.capture_store.load().captures

    @property
    def snapshot(self) -> TerminalSnapshot:
        return self.replay.seek(self.current_time)

    def view(self) -> ReviewView:
        captures = self.captures
        selected = min(self.selected_capture, max(0, len(captures) - 1))
        self.selected_capture = selected
        return ReviewView(
            snapshot=self.snapshot,
            captures=captures,
            current_time=self.current_time,
            duration=self.duration,
            playing=self.playing,
            selected_capture=selected,
        )

    def play(self) -> None:
        self.playing = self.current_time < self.duration

    def pause(self) -> None:
        self.playing = False

    def toggle_play(self) -> None:
        if self.playing:
            self.pause()
        else:
            self.play()

    def advance(self, seconds: float) -> None:
        if not self.playing:
            return
        self.seek(self.current_time + max(0.0, seconds))
        if self.current_time >= self.duration:
            self.playing = False

    def seek(self, relative_time: float) -> float:
        self.current_time = min(max(0.0, float(relative_time)), self.duration)
        return self.current_time

    def seek_by(self, seconds: float) -> float:
        return self.seek(self.current_time + seconds)

    def select_capture(self, delta: int) -> int:
        captures = self.captures
        if not captures:
            self.selected_capture = 0
            return self.selected_capture
        self.selected_capture = min(
            max(0, self.selected_capture + delta),
            len(captures) - 1,
        )
        return self.selected_capture

    def jump_to_capture(self, index: int) -> float:
        captures = self.captures
        if not 0 <= index < len(captures):
            raise IndexError(index)
        self.selected_capture = index
        return self.seek(captures[index].timestamp)

    def create_capture(self, title: str = "") -> CaptureRecord:
        capture = self.capture_store.create_capture(
            self.snapshot,
            timestamp=self.current_time,
            cwd=self.cwd,
            title=title,
        )
        self.selected_capture = max(0, len(self.captures) - 1)
        return capture

    def edit_capture_title(self, capture_id: str, title: str) -> CaptureRecord:
        return self.capture_store.edit_title(capture_id, title)

    def delete_capture(self, capture_id: str) -> bool:
        deleted = self.capture_store.delete(capture_id)
        self.selected_capture = min(self.selected_capture, max(0, len(self.captures) - 1))
        return deleted


def format_progress(
    current: float,
    duration: float,
    *,
    width: int = 28,
    label: str = "",
) -> str:
    safe_width = max(8, width)
    ratio = 0.0 if duration <= 0 else min(max(current / duration, 0.0), 1.0)
    label_width = display_width(label)
    bar_width = max(8, safe_width - label_width - 1) if label else safe_width
    inner_width = bar_width - 2
    filled = round(inner_width * ratio)
    bar = "[" + "=" * filled + "-" * (inner_width - filled) + "]"
    suffix = (
        f" {truncate_cells(label, max(0, safe_width - display_width(bar) - 1))}" if label else ""
    )
    return bar + suffix


def snapshot_to_text(snapshot: TerminalSnapshot) -> Text:
    text = Text()
    for row_index, row in enumerate(snapshot.cells):
        for cell in row:
            if cell.width == 0:
                continue
            character = cell.character or " "
            text.append(character, style=_cell_style(cell))
        if row_index < snapshot.rows - 1:
            text.append("\n")
    return text


def _cell_style(cell: TerminalCell) -> str:
    foreground = "white" if cell.foreground == "default" else cell.foreground
    background = None if cell.background == "default" else cell.background
    if cell.reverse:
        foreground, background = background or "black", foreground
    styles = [foreground]
    if background:
        styles.append(f"on {background}")
    if cell.bold:
        styles.append("bold")
    if cell.underline:
        styles.append("underline")
    if cell.italic:
        styles.append("italic")
    return " ".join(styles)


class ReviewScreen(Screen[None]):
    BINDINGS = REVIEW_BINDINGS

    def __init__(self, *, controller: ReviewController, locale: Translator) -> None:
        super().__init__(name="review")
        self.controller = controller
        self.locale = locale
        self.is_wide = False
        self.active_pane = "terminal"

    def compose(self) -> ComposeResult:
        yield Vertical(
            Static(self._title(), id="review-title", markup=False),
            Horizontal(
                ReviewTerminal(id="review-terminal"),
                Vertical(
                    ReviewTimeline(id="review-timeline"),
                    ReviewCaptureList(id="review-captures"),
                    id="review-sidebar",
                ),
                id="review-body",
            ),
            Static(id="review-narrow"),
            ReviewFooter(id="review-footer", markup=False),
            id="review-layout",
        )

    def on_mount(self) -> None:
        self.set_interval(0.1, self._tick)
        self._set_layout(self.size.width >= 120)
        self._refresh()

    def on_resize(self, event: Resize) -> None:
        self._set_layout(event.size.width >= 120)
        self._refresh()

    def _set_layout(self, wide: bool) -> None:
        self.is_wide = wide
        self.set_class(wide, "wide")
        self.set_class(not wide, "narrow")

    def _tick(self) -> None:
        before = self.controller.current_time
        self.controller.advance(0.1)
        if before != self.controller.current_time:
            self._refresh()

    def _refresh(self) -> None:
        view = self.controller.view()
        self.query_one("#review-terminal", ReviewTerminal).update(snapshot_to_text(view.snapshot))
        self.query_one("#review-timeline", ReviewTimeline).update(self._timeline(view))
        self.query_one("#review-captures", ReviewCaptureList).update(self._captures(view))
        self.query_one("#review-footer", ReviewFooter).update(self._footer(view))
        narrow = self.query_one("#review-narrow", Static)
        if self.active_pane == "terminal":
            narrow.update(snapshot_to_text(view.snapshot))
        elif self.active_pane == "timeline":
            narrow.update(self._timeline(view))
        else:
            narrow.update(self._captures(view))

    def _title(self) -> str:
        available_width = self.size.width - 16 if self.size.width else 64
        title = truncate_cells(self.controller.session.root.name, max(1, available_width))
        return f"REVIEW  //  {title}"

    def _timeline(self, view: ReviewView) -> str:
        state = "播放中" if view.playing else "已暂停"
        return (
            "TIMELINE\n"
            f"{format_progress(view.current_time, view.duration, width=28)}\n"
            f"{view.current_time:05.1f}s / {view.duration:05.1f}s  {state}"
        )

    def _captures(self, view: ReviewView) -> str:
        lines = ["CAPTURES"]
        if not view.captures:
            lines.append("暂无 Capture；按 C 创建。")
        for index, capture in enumerate(view.captures):
            marker = ">" if index == view.selected_capture else " "
            title = truncate_cells(
                capture.title or f"实验记录 {index + 1}",
                max(8, self.size.width - (50 if self.is_wide else 8)),
                ellipsis="…",
            )
            lines.append(f"{marker} {index + 1:02d}  {title}  {capture.timestamp:.1f}s")
        return "\n".join(lines)

    def _footer(self, view: ReviewView) -> str:
        footer = (
            f"{format_progress(view.current_time, view.duration, width=24)}  "
            "Space 播放/暂停  ←→ seek  ↑↓ Capture  C 创建  E 标题  Delete 删除  Tab 切换  Q 返回"
        )
        return truncate_cells(footer, max(1, self.size.width - 2), ellipsis="…")

    def action_cycle_pane(self) -> None:
        panes = ("terminal", "timeline", "captures")
        self.active_pane = panes[(panes.index(self.active_pane) + 1) % len(panes)]
        self._refresh()

    def action_toggle_play(self) -> None:
        self.controller.toggle_play()
        self._refresh()

    def action_seek_back(self) -> None:
        self.controller.seek_by(-1.0)
        self._refresh()

    def action_seek_forward(self) -> None:
        self.controller.seek_by(1.0)
        self._refresh()

    def action_seek_back_large(self) -> None:
        self.controller.seek_by(-10.0)
        self._refresh()

    def action_seek_forward_large(self) -> None:
        self.controller.seek_by(10.0)
        self._refresh()

    def action_select_previous_capture(self) -> None:
        self.controller.select_capture(-1)
        self._refresh()

    def action_select_next_capture(self) -> None:
        self.controller.select_capture(1)
        self._refresh()

    def action_create_capture(self) -> None:
        self.app.push_screen(
            CaptureTitleDialog(locale=self.locale, heading="创建 Capture"),
            self._create_capture_from_dialog,
        )

    def _create_capture_from_dialog(self, title: str | None) -> None:
        if title is not None:
            self.controller.create_capture(title)
            self._refresh()

    def action_jump_capture(self) -> None:
        if self.controller.captures:
            self.controller.jump_to_capture(self.controller.selected_capture)
            self._refresh()

    def action_edit_capture(self) -> None:
        captures = self.controller.captures
        if captures:
            capture = captures[self.controller.selected_capture]
            self.app.push_screen(
                CaptureTitleDialog(
                    locale=self.locale,
                    title=capture.title,
                    heading="编辑 Capture 标题",
                ),
                lambda title: self._edit_capture_from_dialog(capture.capture_id, title),
            )

    def _edit_capture_from_dialog(self, capture_id: str, title: str | None) -> None:
        if title is not None:
            self.controller.edit_capture_title(capture_id, title)
            self._refresh()

    def action_delete_capture(self) -> None:
        captures = self.controller.captures
        if captures:
            capture = captures[self.controller.selected_capture]
            self.app.push_screen(
                ConfirmDialog(
                    locale=self.locale,
                    title="删除 Capture",
                    message=f"确定删除“{capture.title or '实验记录'}”吗？此操作不可撤销。",
                ),
                lambda confirmed: self._delete_capture_from_dialog(capture.capture_id, confirmed),
            )

    def _delete_capture_from_dialog(self, capture_id: str, confirmed: bool) -> None:
        if confirmed:
            self.controller.delete_capture(capture_id)
            self._refresh()

    def action_go_back(self) -> None:
        if getattr(self.app, "owns_review_screen", False):
            self.app.exit()
        else:
            self.app.pop_screen()
