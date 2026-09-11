from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.events import Resize
from textual.screen import Screen
from textual.widgets import Button

from csbox.check.service import create_check_service
from csbox.core.models import HomeSnapshot
from csbox.locales import Translator
from csbox.tui.dialogs.lab_start import LabStartDialog
from csbox.tui.dialogs.unavailable import UnavailableDialog
from csbox.tui.lab_workflow import HomeNotice, LabStartRequest, ShellOption
from csbox.tui.screens.pack import PackConfirmationScreen
from csbox.tui.screens.project_check import ProjectCheckScreen
from csbox.tui.widgets.home import (
    ACTION_DEFINITIONS,
    ActionPanel,
    BrandBlock,
    ProjectPanel,
    ShortcutBar,
    WorkflowStatusPanel,
)


class HomeScreen(Screen[None]):
    BINDINGS = [
        Binding("up", "focus_previous_entry", "上一个入口", show=False, priority=True),
        Binding("down", "focus_next_entry", "下一个入口", show=False, priority=True),
    ]

    def __init__(
        self,
        *,
        snapshot: HomeSnapshot,
        locale: Translator,
        api_screen_factory: Callable[[], Screen[None]] | None = None,
        pack_plan_factory: Callable[..., object] | None = None,
        pack_action: Callable[[object], object] | None = None,
        shell_options: tuple[ShellOption, ...] = (),
        shell_error: str | None = None,
        notice: HomeNotice | None = None,
        records_screen_factory: Callable[[], Screen[None]] | None = None,
        evidence_screen_factory: Callable[[], Screen[None]] | None = None,
        submission_screen_factory: Callable[[], Screen[None]] | None = None,
        check_service: object | None = None,
    ) -> None:
        super().__init__(name="home")
        self.snapshot = snapshot
        self.locale = locale
        self.api_screen_factory = api_screen_factory
        self.pack_plan_factory = pack_plan_factory
        self.pack_action = pack_action
        self.shell_options = shell_options
        self.shell_error = shell_error
        self.notice = notice
        self.records_screen_factory = records_screen_factory
        self.evidence_screen_factory = evidence_screen_factory
        self.submission_screen_factory = submission_screen_factory
        self.check_service = check_service
        self.is_wide = False
        self._busy_workflow: str | None = None
        self._workflow_status: HomeNotice | None = None

    def compose(self) -> ComposeResult:
        yield Vertical(
            VerticalScroll(
                BrandBlock(self.locale, id="brand-block"),
                Horizontal(
                    ActionPanel(self.locale, id="action-panel"),
                    Vertical(
                        ProjectPanel(self.snapshot, self.locale, id="project-panel"),
                        WorkflowStatusPanel(
                            self.locale,
                            self.notice,
                            id="workflow-status-panel",
                        ),
                        id="home-context",
                    ),
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

    @property
    def is_working(self) -> bool:
        return self._busy_workflow is not None

    def on_resize(self, event: Resize) -> None:
        self.is_wide = event.size.width >= 120
        self.set_class(self.is_wide, "wide")
        self.call_after_refresh(self._keep_focused_entry_visible)

    def _keep_focused_entry_visible(self) -> None:
        focused = self.focused
        if isinstance(focused, Button) and focused.has_class("entry-button"):
            focused.scroll_visible(animate=False, immediate=True)

    def action_focus_next_entry(self) -> None:
        self.focus_next(".entry-button")

    def action_focus_previous_entry(self) -> None:
        self.focus_previous(".entry-button")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id or ""
        action_id = button_id.removeprefix("entry-")
        action_keys = dict(ACTION_DEFINITIONS)
        label_key = action_keys.get(action_id)
        if label_key is None or self._busy_workflow is not None:
            return
        if action_id == "records":
            self._open_records()
            return
        if action_id == "evidence":
            self._open_evidence()
            return
        if action_id == "submit":
            self._open_submission()
            return
        if action_id == "start":
            self.app.push_screen(
                LabStartDialog(
                    locale=self.locale,
                    project_dir=self.snapshot.project_dir or Path.cwd(),
                    shell_options=self.shell_options,
                    shell_error=self.shell_error,
                ),
                self._handle_start_request,
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

    def _open_records(self) -> None:
        if self.records_screen_factory is None:
            self._show_action_error(
                self.locale("home.entry.records"),
                "实验记录暂时无法打开，请稍后重试。",
            )
            return
        try:
            screen = self.records_screen_factory()
        except Exception:
            self._show_action_error(
                self.locale("home.entry.records"),
                "实验记录暂时无法打开，请稍后重试。",
            )
            return
        self.app.push_screen(screen)

    def _open_evidence(self) -> None:
        if self.evidence_screen_factory is None:
            self._show_action_error(
                self.locale("home.entry.evidence"),
                self.locale("evidence.list.load_error"),
            )
            return
        try:
            screen = self.evidence_screen_factory()
        except Exception:
            self._show_action_error(
                self.locale("home.entry.evidence"),
                self.locale("evidence.list.load_error"),
            )
            return
        self.app.push_screen(screen)

    def _open_submission(self) -> None:
        if self.submission_screen_factory is None:
            self._show_action_error("准备提交", "准备提交暂时无法打开，请稍后重试。")
            return
        try:
            screen = self.submission_screen_factory()
        except Exception:
            self._show_action_error("准备提交", "准备提交暂时无法打开，请稍后重试。")
            return
        self.app.push_screen(screen)

    def _open_pack(self) -> None:
        if self.pack_plan_factory is None:
            self._show_action_error(
                self.locale("home.entry.pack"),
                self.locale("home.pack.unavailable"),
            )
            return
        if not self._begin_workflow("pack", self.locale("home.status.pack_preparing")):
            return
        self.run_worker(
            self._prepare_pack(),
            name="home-pack-plan",
            group="home-pack-plan",
            exclusive=True,
            exit_on_error=False,
        )

    def _show_action_error(self, title: str, message: str) -> None:
        self.app.push_screen(UnavailableDialog(title=title, locale=self.locale, message=message))

    def _handle_start_request(self, request: LabStartRequest | None) -> None:
        if request is not None:
            self.app.exit(request)

    def _open_project_check(self) -> None:
        project_dir = self.snapshot.project_dir or Path.cwd()
        if not self._begin_workflow("check", self.locale("home.status.checking")):
            return
        self.run_worker(
            self._prepare_project_check(project_dir),
            name="home-project-check",
            group="home-project-check",
            exclusive=True,
            exit_on_error=False,
        )

    async def _prepare_pack(self) -> None:
        factory = self.pack_plan_factory
        if factory is None:
            self._finish_workflow()
            self._show_action_error(
                self.locale("home.entry.pack"),
                self.locale("home.pack.unavailable"),
            )
            return
        try:
            plan = await asyncio.to_thread(factory)
            screen = PackConfirmationScreen(
                plan=plan,
                locale=self.locale,
                pack_action=self.pack_action,
                plan_factory=factory,
            )
        except Exception:
            self._finish_workflow()
            self._show_action_error(
                self.locale("home.entry.pack"),
                self.locale("home.pack.error"),
            )
            return
        self._finish_workflow()
        self.app.push_screen(screen)

    async def _prepare_project_check(self, project_dir: Path) -> None:
        try:
            report = await asyncio.to_thread(self._run_project_check, project_dir)
        except Exception:
            self._finish_workflow()
            self.app.push_screen(
                UnavailableDialog(
                    title=self.locale("home.entry.check"),
                    locale=self.locale,
                    message_key="home.check.error",
                )
            )
            return
        self._finish_workflow()
        self.app.push_screen(ProjectCheckScreen(report=report, locale=self.locale))

    def _run_project_check(self, project_dir: Path) -> object:
        checker = (
            self.check_service
            if self.check_service is not None
            else create_check_service(project_dir)
        )
        return checker.run(project_dir)

    def _begin_workflow(self, workflow: str, message: str) -> bool:
        if self._busy_workflow is not None:
            return False
        self._busy_workflow = workflow
        self._workflow_status = HomeNotice(message, "running")
        for button in self.query(Button).filter(".entry-button"):
            button.disabled = True
        self._refresh_workflow_status()
        return True

    def _finish_workflow(self) -> None:
        self._busy_workflow = None
        self._workflow_status = None
        for button in self.query(Button).filter(".entry-button"):
            button.disabled = False
        self._refresh_workflow_status()

    def _refresh_workflow_status(self) -> None:
        self.query_one(WorkflowStatusPanel).update_notice(self._workflow_status or self.notice)

    def update_snapshot(self, snapshot: HomeSnapshot) -> None:
        self.snapshot = snapshot
        self.query_one(ProjectPanel).update_snapshot(snapshot)

    def update_notice(self, notice: HomeNotice | None) -> None:
        self.notice = notice
        self._refresh_workflow_status()
