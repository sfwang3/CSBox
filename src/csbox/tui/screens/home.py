from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.events import Resize
from textual.screen import Screen
from textual.widgets import Button

from csbox.check.service import CheckService, CheckServiceError
from csbox.core.models import HomeSnapshot
from csbox.lab.repository import SessionRepository, SessionRepositoryError
from csbox.locales import Translator
from csbox.tui.dialogs.unavailable import UnavailableDialog
from csbox.tui.screens.pack import PackConfirmationScreen
from csbox.tui.screens.project_check import ProjectCheckScreen
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
    def __init__(
        self,
        *,
        snapshot: HomeSnapshot,
        locale: Translator,
        api_screen_factory: Callable[[], Screen[None]] | None = None,
        pack_plan_factory: Callable[[], object] | None = None,
        pack_action: Callable[[object], object] | None = None,
    ) -> None:
        super().__init__(name="home")
        self.snapshot = snapshot
        self.locale = locale
        self.api_screen_factory = api_screen_factory
        self.pack_plan_factory = pack_plan_factory
        self.pack_action = pack_action
        self.is_wide = False

    def compose(self) -> ComposeResult:
        yield Vertical(
            VerticalScroll(
                BrandBlock(self.locale, id="brand-block"),
                EnvironmentPanel(self.snapshot, self.locale, id="environment-panel"),
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
        self.query_one("#entry-start", Button).focus()

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
        if action_id == "start":
            self._show_action_error(
                self.locale("home.entry.start"),
                self.locale("home.start.guidance"),
            )
            return
        if action_id == "check":
            self._open_project_check()
            return
        if action_id == "api":
            self._open_api()
            return
        if action_id == "pack":
            self._open_pack()
            return
        self.app.push_screen(UnavailableDialog(title=self.locale(label_key), locale=self.locale))

    def _open_api(self) -> None:
        if self.api_screen_factory is None:
            self._show_action_error(
                self.locale("home.entry.api"),
                self.locale("home.api.error"),
            )
            return
        try:
            screen = self.api_screen_factory()
        except Exception:
            self._show_action_error(
                self.locale("home.entry.api"),
                self.locale("home.api.error"),
            )
            return
        self.app.push_screen(screen)

    def _open_pack(self) -> None:
        if self.pack_plan_factory is None:
            self._show_action_error(
                self.locale("home.entry.pack"),
                self.locale("home.pack.unavailable"),
            )
            return
        try:
            plan = self.pack_plan_factory()
        except Exception:
            self._show_action_error(
                self.locale("home.entry.pack"),
                self.locale("home.pack.error"),
            )
            return
        self.app.push_screen(
            PackConfirmationScreen(
                plan=plan,
                locale=self.locale,
                pack_action=self.pack_action,
            )
        )

    def _show_action_error(self, title: str, message: str) -> None:
        self.app.push_screen(UnavailableDialog(title=title, locale=self.locale, message=message))

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

    def _open_project_check(self) -> None:
        project_dir = self.snapshot.project_dir or Path.cwd()
        try:
            report = CheckService().run(project_dir)
        except (OSError, UnicodeError, ValueError, CheckServiceError):
            self.app.push_screen(
                UnavailableDialog(
                    title=self.locale("home.entry.check"),
                    locale=self.locale,
                    message_key="home.check.error",
                )
            )
            return
        self.app.push_screen(ProjectCheckScreen(report=report, locale=self.locale))

    def update_snapshot(self, snapshot: HomeSnapshot) -> None:
        self.snapshot = snapshot
        self.query_one(EnvironmentPanel).update_snapshot(snapshot)
        self.query_one(RecentPanel).update_snapshot(snapshot)
