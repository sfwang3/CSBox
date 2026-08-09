from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import Button, Static

from csbox.core.models import HomeSnapshot, RecentExperiment
from csbox.locales import Translator

ACTION_DEFINITIONS = (
    ("start", "home.entry.start"),
    ("replay", "home.entry.replay"),
    ("check", "home.entry.check"),
    ("api", "home.entry.api"),
    ("pack", "home.entry.pack"),
)


class BrandBlock(Vertical):
    def __init__(self, locale: Translator, **kwargs: object) -> None:
        super().__init__(**kwargs)
        self.locale = locale

    def compose(self) -> ComposeResult:
        yield Static(self.locale("brand.name"), id="brand-name", markup=False)
        yield Static(self.locale("brand.subtitle"), id="brand-subtitle", markup=False)
        yield Static(self.locale("brand.version"), id="brand-version", markup=False)
        yield Static(self.locale("home.greeting"), id="home-greeting", markup=False)
        yield Static(self.locale("home.description"), id="home-description", markup=False)


class EnvironmentPanel(Vertical):
    def __init__(
        self,
        snapshot: HomeSnapshot,
        locale: Translator,
        **kwargs: object,
    ) -> None:
        super().__init__(**kwargs)
        self.snapshot = snapshot
        self.locale = locale

    def compose(self) -> ComposeResult:
        yield Static(self.locale("home.environment.title"), classes="panel-title", markup=False)
        yield Static(self._content(), id="environment-content", markup=False)

    def _content(self) -> str:
        environment = self.snapshot.environment
        values = (
            ("environment.os", f"{environment.os_name} {environment.os_version}"),
            ("environment.python", environment.python_version),
            (
                "environment.shell",
                environment.shell or self.locale("common.unknown"),
            ),
            (
                "environment.wsl",
                self.locale("common.yes" if environment.is_wsl else "common.no"),
            ),
            (
                "environment.terminal",
                f"{environment.terminal_columns}×{environment.terminal_rows}",
            ),
        )
        return "  ·  ".join(
            self.locale("home.environment.item", label=self.locale(key), value=value)
            for key, value in values
        )

    def update_snapshot(self, snapshot: HomeSnapshot) -> None:
        self.snapshot = snapshot
        self.query_one("#environment-content", Static).update(self._content())


class ActionPanel(Vertical):
    def __init__(self, locale: Translator, **kwargs: object) -> None:
        super().__init__(**kwargs)
        self.locale = locale

    def compose(self) -> ComposeResult:
        yield Static(self.locale("home.entries.title"), classes="panel-title", markup=False)
        for action_id, label_key in ACTION_DEFINITIONS:
            yield Button(
                self.locale(label_key),
                id=f"entry-{action_id}",
                classes="entry-button",
            )


class RecentPanel(Vertical):
    def __init__(
        self,
        snapshot: HomeSnapshot,
        locale: Translator,
        **kwargs: object,
    ) -> None:
        super().__init__(**kwargs)
        self.snapshot = snapshot
        self.locale = locale

    def compose(self) -> ComposeResult:
        yield Static(self.locale("home.recent.title"), classes="panel-title", markup=False)
        yield Static(self.locale("home.demo.note"), id="demo-note", markup=False)
        yield Static(
            self.locale("home.demo.count", count=len(self.snapshot.recent_experiments)),
            id="demo-count",
            markup=False,
        )
        yield Static(self._content(), id="recent-content", markup=False)

    def _content(self) -> str:
        return "\n".join(
            self._experiment_line(experiment) for experiment in self.snapshot.recent_experiments
        )

    def _experiment_line(self, experiment: RecentExperiment) -> str:
        status_key = f"home.status.{experiment.status}"
        if status_key not in self.locale.messages:
            status_key = "home.status.unknown"
        return self.locale(
            "home.recent.item",
            demo=self.locale("home.demo.tag") if experiment.demo else "",
            name=experiment.name,
            status=self.locale(status_key),
            duration=self.locale("home.duration", duration=experiment.duration),
        )

    def update_snapshot(self, snapshot: HomeSnapshot) -> None:
        self.snapshot = snapshot
        self.query_one("#demo-count", Static).update(
            self.locale("home.demo.count", count=len(snapshot.recent_experiments))
        )
        self.query_one("#recent-content", Static).update(self._content())


class ShortcutBar(Static):
    def __init__(self, locale: Translator, **kwargs: object) -> None:
        super().__init__(locale("home.shortcuts"), markup=False, **kwargs)
