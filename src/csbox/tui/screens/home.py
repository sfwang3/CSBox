from __future__ import annotations

from pathlib import Path

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.events import Resize
from textual.screen import Screen
from textual.widgets import Button

from csbox.core.models import HomeSnapshot
from csbox.lab.repository import SessionRepository, SessionRepositoryError
from csbox.locales import Translator
from csbox.tui.dialogs.unavailable import UnavailableDialog
from csbox.tui.screens.review import ReviewController, ReviewScreen
from csbox.tui.widgets.home import (
    ACTION_DEFINITIONS,
    ActionPanel,
    BrandBlock,
    EnvironmentPanel,
    RecentPanel,
    ShortcutBar,
)


class HomeScreen(Screen[None]):
    def __init__(self, *, snapshot: HomeSnapshot, locale: Translator) -> None:
        super().__init__(name="home")
        self.snapshot = snapshot
        self.locale = locale
        self.is_wide = False

    def compose(self) -> ComposeResult:
        yield Vertical(
            BrandBlock(self.locale, id="brand-block"),
            EnvironmentPanel(self.snapshot, self.locale, id="environment-panel"),
            VerticalScroll(
                Horizontal(
                    ActionPanel(self.locale, id="action-panel"),
                    RecentPanel(self.snapshot, self.locale, id="recent-panel"),
                    id="content-grid",
                ),
                id="main-scroll",
            ),
            ShortcutBar(self.locale, id="shortcut-bar"),
            id="home-layout",
        )

    def on_mount(self) -> None:
        self.is_wide = self.size.width >= 120
        self.set_class(self.is_wide, "wide")

    def on_resize(self, event: Resize) -> None:
        self.is_wide = event.size.width >= 120
        self.set_class(self.is_wide, "wide")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id or ""
        action_id = button_id.removeprefix("entry-")
        action_keys = dict(ACTION_DEFINITIONS)
        label_key = action_keys.get(action_id)
        if label_key is None:
            return
        if action_id == "replay":
            self._open_latest_review()
            return
        self.app.push_screen(UnavailableDialog(title=self.locale(label_key), locale=self.locale))

    def _open_latest_review(self) -> None:
        project_dir = self.snapshot.project_dir or Path.cwd()
        try:
            repository = SessionRepository.from_cwd(project_dir)
            target = repository.latest()
            if target is None:
                raise SessionRepositoryError("暂无可回看的 session，请先运行 csbox lab start。")
            self.app.push_screen(
                ReviewScreen(
                    controller=ReviewController.from_session(target.paths),
                    locale=self.locale,
                )
            )
        except (OSError, UnicodeError, ValueError, SessionRepositoryError):
            self.app.push_screen(
                UnavailableDialog(
                    title=self.locale("home.entry.replay"),
                    locale=self.locale,
                    message_key="home.replay.empty",
                )
            )

    def update_snapshot(self, snapshot: HomeSnapshot) -> None:
        self.snapshot = snapshot
        self.query_one(EnvironmentPanel).update_snapshot(snapshot)
        self.query_one(RecentPanel).update_snapshot(snapshot)
