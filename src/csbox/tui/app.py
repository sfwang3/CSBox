from __future__ import annotations

from pathlib import Path

from textual.app import App

from csbox.core.models import EnvironmentSnapshot
from csbox.lab.ports import HomeDataSource
from csbox.locales import Translator
from csbox.tui.dialogs.unavailable import UnavailableDialog
from csbox.tui.screens.home import HomeScreen


class CSBoxApp(App[None]):
    TITLE = "CSBox"
    CSS_PATH = Path(__file__).parent / "themes" / "csbox.tcss"
    BINDINGS = [
        ("f1", "show_help", ""),
        ("f5", "refresh_home", ""),
        ("q", "quit_app", ""),
    ]

    def __init__(
        self,
        *,
        data_source: HomeDataSource,
        environment: EnvironmentSnapshot,
        locale: Translator,
    ) -> None:
        super().__init__()
        self.data_source = data_source
        self.environment = environment
        self.locale = locale
        self.snapshot = data_source.get_home_snapshot(environment)

    def on_mount(self) -> None:
        self.push_screen(HomeScreen(snapshot=self.snapshot, locale=self.locale))

    def action_show_help(self) -> None:
        self.push_screen(
            UnavailableDialog(
                title=self.locale("home.help.title"),
                locale=self.locale,
                message_key="home.help.body",
                close_key="home.help.close",
            )
        )

    def action_refresh_home(self) -> None:
        self.snapshot = self.data_source.get_home_snapshot(self.environment)
        if isinstance(self.screen, HomeScreen):
            self.screen.update_snapshot(self.snapshot)

    def action_quit_app(self) -> None:
        self.exit()
