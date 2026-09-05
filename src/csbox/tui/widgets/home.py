from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.events import Resize
from textual.widgets import Button, Static

from csbox.api.redaction import Redactor
from csbox.core.display_width import truncate_cells
from csbox.core.models import HomeSnapshot
from csbox.core.text_layout import wrap_cells
from csbox.locales import Translator
from csbox.tui.lab_workflow import HomeNotice

ACTION_DEFINITIONS = (
    ("start", "home.entry.start"),
    ("records", "home.entry.records"),
    ("evidence", "home.entry.evidence"),
    ("check", "home.entry.check"),
    ("pack", "home.entry.pack"),
    ("api", "home.entry.api"),
)
ACTION_DESCRIPTION_KEYS = {
    action_id: f"{label_key}.description" for action_id, label_key in ACTION_DEFINITIONS
}
_HOME_REDACTOR = Redactor.with_configured_values(())


class BrandBlock(Vertical):
    def __init__(self, locale: Translator, **kwargs: object) -> None:
        super().__init__(**kwargs)
        self.locale = locale

    def compose(self) -> ComposeResult:
        yield Static(self.locale("brand.name"), id="brand-name", markup=False)
        yield Static(self.locale("brand.subtitle"), id="brand-subtitle", markup=False)
        yield Static(self.locale("home.greeting"), id="home-greeting", markup=False)
        yield Static(self.locale("home.description"), id="home-description", markup=False)
        yield Static(self.locale("home.quick_start"), id="home-quick-start", markup=False)


class ActionPanel(Vertical):
    def __init__(self, locale: Translator, **kwargs: object) -> None:
        super().__init__(**kwargs)
        self.locale = locale

    def compose(self) -> ComposeResult:
        yield Static(self.locale("home.entries.title"), classes="panel-title", markup=False)
        for action_id, label_key in ACTION_DEFINITIONS:
            yield Horizontal(
                Button(
                    self.locale(label_key),
                    id=f"entry-{action_id}",
                    classes="entry-button",
                    variant="primary" if action_id == "start" else "default",
                ),
                ActionDescription(
                    self.locale(ACTION_DESCRIPTION_KEYS[action_id]),
                    classes="entry-description",
                ),
                classes="entry-card",
            )


class ActionDescription(Static):
    """Keep each short Home purpose line within its display-cell width."""

    def __init__(self, value: str, **kwargs: object) -> None:
        super().__init__(value, markup=False, **kwargs)
        self._value = value

    def on_mount(self) -> None:
        self._refresh_text()

    def on_resize(self, event: Resize) -> None:
        del event
        self._refresh_text()

    def _refresh_text(self) -> None:
        width = max(2, self.content_region.width or 32)
        self.update("\n".join(wrap_cells(self._value, width)))


class ProjectPanel(Vertical):
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
        yield Static(self.locale("home.project.title"), classes="panel-title", markup=False)
        yield Static(self._content(), id="home-project-path", markup=False)
        yield Static(self.locale("home.project.read_only"), id="home-project-note", markup=False)

    def on_resize(self, event: Resize) -> None:
        del event
        self._refresh()

    def update_snapshot(self, snapshot: HomeSnapshot) -> None:
        self.snapshot = snapshot
        self._refresh()

    def _refresh(self) -> None:
        self.query_one("#home-project-path", Static).update(self._content())

    def _content(self) -> str:
        project_dir = self.snapshot.project_dir
        value = str(project_dir) if project_dir is not None else ""
        width = self.size.width - 4 if self.size.width else 64
        return truncate_cells(
            _HOME_REDACTOR.text(value),
            max(1, width),
            ellipsis="…",
        )


class WorkflowStatusPanel(Vertical):
    def __init__(
        self,
        locale: Translator,
        notice: HomeNotice | None = None,
        **kwargs: object,
    ) -> None:
        super().__init__(**kwargs)
        self.locale = locale
        self.notice = notice

    def compose(self) -> ComposeResult:
        yield Static(self.locale("home.workflow.title"), classes="panel-title", markup=False)
        yield Static(self._content(), id="home-workflow-status", markup=False)

    def update_notice(self, notice: HomeNotice | None) -> None:
        self.notice = notice
        self.query_one("#home-workflow-status", Static).update(self._content())

    def _content(self) -> str:
        return self.notice.message if self.notice is not None else self.locale("home.status.ready")


class ShortcutBar(Static):
    def __init__(self, locale: Translator, **kwargs: object) -> None:
        super().__init__(locale("home.shortcuts"), markup=False, **kwargs)
