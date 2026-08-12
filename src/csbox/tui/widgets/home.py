from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.events import Resize
from textual.widgets import Button, Static

from csbox.api.redaction import Redactor
from csbox.core.display_width import truncate_cells
from csbox.core.models import HomeSnapshot, RecentApiRun, RecentExperiment
from csbox.locales import Translator

ACTION_DEFINITIONS = (
    ("start", "home.entry.start"),
    ("replay", "home.entry.replay"),
    ("api", "home.entry.api"),
    ("check", "home.entry.check"),
    ("pack", "home.entry.pack"),
)
_HOME_REDACTOR = Redactor.with_configured_values(())


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
        project_dir = self.snapshot.project_dir
        environment = self.snapshot.environment
        values = (
            (
                "environment.os",
                _HOME_REDACTOR.text(f"{environment.os_name} {environment.os_version}"),
            ),
            ("environment.python", _HOME_REDACTOR.text(environment.python_version)),
            (
                "environment.shell",
                _HOME_REDACTOR.text(environment.shell or self.locale("common.unknown")),
            ),
            (
                "environment.wsl",
                self.locale("common.yes" if environment.is_wsl else "common.no"),
            ),
            (
                "environment.terminal",
                f"{environment.terminal_columns}×{environment.terminal_rows}",
            ),
            (
                "environment.project",
                truncate_cells(
                    _HOME_REDACTOR.text(
                        str(project_dir) if project_dir else self.locale("common.unknown")
                    ),
                    max(24, self.size.width - 18) if self.size.width else 60,
                    ellipsis="…",
                ),
            ),
        )
        return "\n".join(
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
        yield Static(self._note(), id="demo-note", markup=False)
        yield Static(self._count(), id="demo-count", markup=False)
        yield Static(self._content(), id="recent-content", markup=False)
        yield Static(self._api_content(), id="recent-api-content", markup=False)
        yield Static(self._check_content(), id="home-check-status", markup=False)

    def _content(self) -> str:
        if not self.snapshot.recent_experiments:
            return self.locale("home.recent.empty")
        return "\n".join(
            self._experiment_line(experiment) for experiment in self.snapshot.recent_experiments
        )

    def _uses_demo(self) -> bool:
        return any(item.demo for item in self.snapshot.recent_experiments)

    def _note(self) -> str:
        return self.locale("home.demo.note" if self._uses_demo() else "home.recent.real.note")

    def _count(self) -> str:
        return self.locale(
            "home.demo.count" if self._uses_demo() else "home.recent.count",
            count=len(self.snapshot.recent_experiments),
        )

    def _experiment_line(self, experiment: RecentExperiment) -> str:
        status_key = f"home.status.{experiment.status}"
        if status_key not in self.locale.messages:
            status_key = "home.status.unknown"
        available_width = self.size.width or 70
        context: list[str] = []
        if experiment.platform:
            context.append(_HOME_REDACTOR.text(experiment.platform))
        if experiment.cwd:
            context.append(
                truncate_cells(
                    _HOME_REDACTOR.text(str(experiment.cwd)),
                    max(1, min(40, available_width // 3)),
                    ellipsis="…",
                )
            )
        line = self.locale(
            "home.recent.item",
            demo=self.locale("home.demo.tag") if experiment.demo else "",
            name=truncate_cells(
                _HOME_REDACTOR.text(experiment.name),
                max(1, min(32, available_width // 3)),
                ellipsis="…",
            ),
            status=self.locale(status_key),
            duration=self.locale(
                "home.duration", duration=_HOME_REDACTOR.text(experiment.duration)
            ),
            captures=experiment.capture_count,
            context=(" | " + " | ".join(context)) if context else "",
        )
        return truncate_cells(line, max(1, available_width - 2), ellipsis="…")

    def _api_content(self) -> str:
        if not self.snapshot.recent_api_runs:
            return self.locale("home.api.empty")
        lines = [self.locale("home.api.title")]
        for run in self.snapshot.recent_api_runs:
            lines.append(self._api_run_line(run))
        return "\n".join(lines)

    def _api_run_line(self, run: RecentApiRun) -> str:
        status_key = f"home.api.status.{run.status}"
        if status_key not in self.locale.messages:
            status_key = "home.api.status.unknown"
        line = self.locale(
            "home.api.item",
            id=truncate_cells(_HOME_REDACTOR.text(run.id), 12, ellipsis="…"),
            name=truncate_cells(_HOME_REDACTOR.text(run.scenario_name), 24, ellipsis="…"),
            status=self.locale(status_key),
            elapsed=f"{run.elapsed_ms:.1f} ms",
        )
        return truncate_cells(line, max(1, (self.size.width or 70) - 2), ellipsis="…")

    def _check_content(self) -> str:
        if self.snapshot.check_status is None:
            return self.locale("home.check.empty")
        status_key = f"home.check.status.{self.snapshot.check_status}"
        if status_key not in self.locale.messages:
            status_key = "home.check.status.unknown"
        return self.locale("home.check.status.item", status=self.locale(status_key))

    def on_resize(self, event: Resize) -> None:
        del event
        self.query_one("#recent-content", Static).update(self._content())
        self.query_one("#recent-api-content", Static).update(self._api_content())
        self.query_one("#home-check-status", Static).update(self._check_content())

    def update_snapshot(self, snapshot: HomeSnapshot) -> None:
        self.snapshot = snapshot
        self.query_one("#demo-note", Static).update(self._note())
        self.query_one("#demo-count", Static).update(self._count())
        self.query_one("#recent-content", Static).update(self._content())
        self.query_one("#recent-api-content", Static).update(self._api_content())
        self.query_one("#home-check-status", Static).update(self._check_content())


class ShortcutBar(Static):
    def __init__(self, locale: Translator, **kwargs: object) -> None:
        super().__init__(locale("home.shortcuts"), markup=False, **kwargs)
