from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from threading import Event

import pytest
from textual.widgets import Button, Static

from csbox.check.models import CheckReport
from csbox.core.models import EnvironmentSnapshot
from csbox.lab.home_data import RealHomeDataSource
from csbox.lab.repository import SessionRepository
from csbox.locales import load_locale
from csbox.tui.app import CSBoxApp
from csbox.tui.dialogs.unavailable import UnavailableDialog
from csbox.tui.screens.pack import PackConfirmationScreen, PackOverwriteDialog
from csbox.tui.screens.project_check import ProjectCheckScreen
from tui_harness import (
    focus_and_press,
    wait_for_busy,
    wait_for_disabled,
    wait_for_focus,
    wait_for_rendered,
    wait_for_screen,
    wait_for_widget,
    wait_for_worker_start,
)
from tui_harness import (
    wait_until as _wait_until,
)


def _environment() -> EnvironmentSnapshot:
    return EnvironmentSnapshot(
        os_name="Linux",
        os_version="test",
        python_version="3.14",
        shell="Bash",
        shell_executable="bash",
        powershell_51_available=False,
        powershell_7_available=False,
        is_wsl=False,
        terminal_columns=100,
        terminal_rows=30,
    )


def _app(tmp_path: Path, **kwargs: object) -> CSBoxApp:
    return CSBoxApp(
        data_source=RealHomeDataSource(
            SessionRepository(tmp_path / ".csbox" / "sessions"),
            tmp_path,
        ),
        environment=_environment(),
        locale=load_locale(),
        **kwargs,
    )


@dataclass
class _PackPlan:
    source_root: Path
    destination: Path
    output_filename: str
    included: tuple[str, ...] = ("README.md",)
    excluded: tuple[str, ...] = ()
    rejected: tuple[str, ...] = ()
    source_bytes: int = 1
    project_type: str = "python"
    output_exists: bool = False
    force: bool = False
    verification_requested: bool = True


def _plan(source: Path, destination: Path | None = None) -> _PackPlan:
    target = destination or source / "student-project-course.zip"
    return _PackPlan(
        source_root=source,
        destination=target,
        output_filename=target.name,
        output_exists=target.exists(),
    )


class _BlockingCheckService:
    def __init__(self, root: Path, *, error: Exception | None = None) -> None:
        self.root = root
        self.error = error
        self.started = Event()
        self.release = Event()
        self.calls = 0

    def run(self, root: Path, *, build: bool = False) -> CheckReport:
        assert build is False
        self.calls += 1
        self.started.set()
        if not self.release.wait(timeout=5):
            raise AssertionError("check worker was not released")
        if self.error is not None:
            raise self.error
        return CheckReport(root=root)


class _BlockingCallable:
    def __init__(self, result_factory) -> None:
        self.result_factory = result_factory
        self.started = Event()
        self.release = Event()
        self.calls = 0

    def __call__(self, *args, **kwargs):
        self.calls += 1
        self.started.set()
        if not self.release.wait(timeout=5):
            raise AssertionError("worker was not released")
        return self.result_factory(*args, **kwargs)


class _MalformedPlan:
    @property
    def destination(self) -> Path:
        raise RuntimeError("malformed destination")


@pytest.mark.asyncio
async def test_home_check_shows_working_state_and_ignores_duplicate_submission(
    tmp_path: Path,
) -> None:
    check = _BlockingCheckService(tmp_path)
    app = _app(tmp_path)
    app.pack_service.check_service = check

    async with app.run_test(size=(100, 30)) as pilot:
        await _wait_until(pilot, lambda: app.screen.name == "home")
        check_button = app.screen.query_one("#entry-check", Button)
        check_button.focus()
        await pilot.press("enter")
        await _wait_until(
            pilot,
            lambda: (
                check.started.is_set()
                and "正在检查"
                in str(app.screen.query_one("#home-workflow-status", Static).renderable)
            ),
        )
        assert check_button.disabled is True
        await pilot.press("enter")
        assert check.calls == 1

        check.release.set()
        await _wait_until(pilot, lambda: isinstance(app.screen, ProjectCheckScreen))


@pytest.mark.asyncio
async def test_home_check_background_workflow_gates_help_and_quit(tmp_path: Path) -> None:
    check = _BlockingCheckService(tmp_path)
    app = _app(tmp_path)
    app.pack_service.check_service = check

    async with app.run_test(size=(100, 30)) as pilot:
        await _wait_until(pilot, lambda: app.screen.name == "home")
        app.screen.query_one("#entry-check", Button).focus()
        await pilot.press("enter")
        await _wait_until(pilot, lambda: check.started.is_set())

        await pilot.press("f1")
        assert app.screen.name == "home"
        await pilot.press("q")
        assert app.is_running is True
        await pilot.press("ctrl+c")
        assert app.is_running is True

        check.release.set()
        await _wait_until(pilot, lambda: isinstance(app.screen, ProjectCheckScreen))


@pytest.mark.asyncio
async def test_home_check_worker_failure_restores_entry_actions(tmp_path: Path) -> None:
    check = _BlockingCheckService(tmp_path, error=RuntimeError("unexpected check failure"))
    app = _app(tmp_path)
    app.pack_service.check_service = check

    async with app.run_test(size=(100, 30)) as pilot:
        await _wait_until(pilot, lambda: app.screen.name == "home")
        check_button = app.screen.query_one("#entry-check", Button)
        check_button.focus()
        await pilot.press("enter")
        await _wait_until(pilot, lambda: check.started.is_set())

        check.release.set()
        await _wait_until(pilot, lambda: isinstance(app.screen, UnavailableDialog))
        assert check_button.disabled is False
        await pilot.press("escape")
        await _wait_until(pilot, lambda: app.screen.name == "home")
        assert check_button.disabled is False


@pytest.mark.asyncio
async def test_home_pack_shows_working_state_and_ignores_duplicate_submission(
    tmp_path: Path,
) -> None:
    factory = _BlockingCallable(lambda: _plan(tmp_path))
    app = _app(tmp_path, pack_plan_factory=factory)

    async with app.run_test(size=(100, 30)) as pilot:
        await wait_for_screen(pilot, app, "home")
        await focus_and_press(pilot, app, "#entry-pack")
        await wait_for_worker_start(pilot, factory.started.is_set)
        await _wait_until(
            pilot,
            lambda: (
                "正在准备打包"
                in str(app.screen.query_one("#home-workflow-status", Static).renderable)
            ),
        )
        await wait_for_disabled(pilot, app.screen, "#entry-pack", True)
        await pilot.press("enter")
        assert factory.calls == 1

        factory.release.set()
        await wait_for_screen(pilot, app, PackConfirmationScreen)


@pytest.mark.asyncio
async def test_home_pack_malformed_initial_plan_is_recoverable(tmp_path: Path) -> None:
    factory = _BlockingCallable(lambda: _MalformedPlan())
    app = _app(tmp_path, pack_plan_factory=factory)

    async with app.run_test(size=(100, 30)) as pilot:
        home = await wait_for_screen(pilot, app, "home")
        pack_button = await wait_for_widget(pilot, home, "#entry-pack")
        await focus_and_press(pilot, app, "#entry-pack")
        await wait_for_worker_start(pilot, factory.started.is_set)

        factory.release.set()
        await wait_for_screen(pilot, app, UnavailableDialog)
        await wait_for_disabled(pilot, home, "#entry-pack", False)
        assert pack_button.disabled is False


@pytest.mark.asyncio
async def test_pack_destination_plan_rebuild_is_background_and_duplicate_safe(
    tmp_path: Path,
) -> None:
    destination = tmp_path / "课程提交" / "最终.zip"
    planner = _BlockingCallable(
        lambda requested=None: _plan(tmp_path, requested or tmp_path / "student-project-course.zip")
    )

    def plan_factory(destination: Path | None = None) -> _PackPlan:
        if destination is None:
            return _plan(tmp_path)
        return planner(destination)

    app = _app(tmp_path, pack_plan_factory=plan_factory)

    async with app.run_test(size=(100, 30)) as pilot:
        await wait_for_screen(pilot, app, "home")
        await focus_and_press(pilot, app, "#entry-pack")
        await wait_for_screen(pilot, app, PackConfirmationScreen)

        destination_input = await wait_for_widget(pilot, app.screen, "#pack-destination-input")
        await wait_for_focus(pilot, app, destination_input)
        destination_input.value = str(destination)
        await pilot.press("enter")
        await _wait_until(
            pilot,
            lambda: (
                planner.started.is_set()
                and "正在更新打包预览"
                in str(app.screen.query_one("#pack-status", Static).renderable)
            ),
        )
        await wait_for_disabled(pilot, app.screen, "#pack-confirm", True)
        await pilot.press("enter")
        assert planner.calls == 1

        planner.release.set()
        await _wait_until(
            pilot,
            lambda: (
                isinstance(app.screen, PackConfirmationScreen)
                and app.screen.destination == destination.absolute()
                and app.screen.focused is not None
                and app.screen.focused.id == "pack-confirm"
            ),
        )


@pytest.mark.asyncio
async def test_pack_malformed_plan_result_restores_retry_state(tmp_path: Path) -> None:
    destination = tmp_path / "课程提交" / "最终.zip"

    def plan_factory(requested: Path | None = None) -> _PackPlan | _MalformedPlan:
        if requested is None:
            return _plan(tmp_path)
        return _MalformedPlan()

    app = _app(tmp_path, pack_plan_factory=plan_factory)

    async with app.run_test(size=(100, 30)) as pilot:
        await wait_for_screen(pilot, app, "home")
        await focus_and_press(pilot, app, "#entry-pack")
        await wait_for_screen(pilot, app, PackConfirmationScreen)

        destination_input = await wait_for_widget(pilot, app.screen, "#pack-destination-input")
        await wait_for_focus(pilot, app, destination_input)
        destination_input.value = str(destination)
        await pilot.press("enter")
        await _wait_until(
            pilot,
            lambda: (
                isinstance(app.screen, PackConfirmationScreen)
                and not app.screen.is_working
                and "无法更新打包预览"
                in str(app.screen.query_one("#pack-status", Static).renderable)
            ),
        )
        assert app.screen.query_one("#pack-confirm", Button).disabled is False


@pytest.mark.asyncio
async def test_pack_overwrite_confirmation_is_single_use(tmp_path: Path) -> None:
    destination = tmp_path / "课程提交" / "最终.zip"
    destination.parent.mkdir()
    destination.write_bytes(b"existing")
    pack = _BlockingCallable(lambda _plan: None)
    app = _app(
        tmp_path,
        pack_plan_factory=lambda: _plan(tmp_path, destination),
        pack_action=pack,
    )

    async with app.run_test(size=(100, 30)) as pilot:
        await wait_for_screen(pilot, app, "home")
        await focus_and_press(pilot, app, "#entry-pack")
        confirmation = await wait_for_screen(pilot, app, PackConfirmationScreen)
        await wait_for_disabled(pilot, confirmation, "#pack-confirm", False)

        confirmation.action_confirm()
        confirmation.action_confirm()
        await wait_for_screen(pilot, app, PackOverwriteDialog)
        assert sum(isinstance(screen, PackOverwriteDialog) for screen in app.screen_stack) == 1
        await wait_for_rendered(pilot, app.screen, "#pack-overwrite-message")
        await focus_and_press(pilot, app, "#pack-overwrite-confirm")
        await wait_for_busy(pilot, confirmation, True)
        await wait_for_worker_start(pilot, pack.started.is_set)
        assert pack.calls == 1
        assert "已取消覆盖" not in str(confirmation.query_one("#pack-status", Static).renderable)

        pack.release.set()
        await _wait_until(
            pilot,
            lambda: (
                not confirmation.is_working
                and "未返回交付报告"
                in str(confirmation.query_one("#pack-status", Static).renderable)
            ),
        )


@pytest.mark.asyncio
async def test_pack_publish_is_background_and_duplicate_safe(tmp_path: Path) -> None:
    pack = _BlockingCallable(lambda _plan: None)
    app = _app(
        tmp_path,
        pack_plan_factory=lambda: _plan(tmp_path),
        pack_action=pack,
    )

    async with app.run_test(size=(100, 30)) as pilot:
        await wait_for_screen(pilot, app, "home")
        await focus_and_press(pilot, app, "#entry-pack")
        confirmation = await wait_for_screen(pilot, app, PackConfirmationScreen)
        await wait_for_disabled(pilot, confirmation, "#pack-confirm", False)
        await focus_and_press(pilot, app, "#pack-confirm")
        await _wait_until(
            pilot,
            lambda: (
                pack.started.is_set()
                and "正在打包" in str(app.screen.query_one("#pack-status", Static).renderable)
            ),
        )
        await wait_for_disabled(pilot, confirmation, "#pack-confirm", True)
        await pilot.press("enter")
        assert pack.calls == 1

        pack.release.set()
        await _wait_until(
            pilot,
            lambda: (
                isinstance(app.screen, PackConfirmationScreen)
                and not app.screen.is_working
                and "正在打包" not in str(app.screen.query_one("#pack-status", Static).renderable)
            ),
        )


@pytest.mark.asyncio
async def test_pack_publish_gates_help_and_quit_until_completion(tmp_path: Path) -> None:
    pack = _BlockingCallable(lambda _plan: None)
    app = _app(
        tmp_path,
        pack_plan_factory=lambda: _plan(tmp_path),
        pack_action=pack,
    )

    async with app.run_test(size=(100, 30)) as pilot:
        await wait_for_screen(pilot, app, "home")
        await focus_and_press(pilot, app, "#entry-pack")
        confirmation = await wait_for_screen(pilot, app, PackConfirmationScreen)
        await wait_for_disabled(pilot, confirmation, "#pack-confirm", False)
        await focus_and_press(pilot, app, "#pack-confirm")
        await wait_for_worker_start(pilot, pack.started.is_set)

        await pilot.press("f1")
        assert isinstance(app.screen, PackConfirmationScreen)
        await pilot.press("escape")
        assert isinstance(app.screen, PackConfirmationScreen)
        await pilot.press("q")
        assert isinstance(app.screen, PackConfirmationScreen)
        await pilot.press("ctrl+c")
        assert app.is_running is True

        pack.release.set()
        await _wait_until(
            pilot,
            lambda: (
                isinstance(app.screen, PackConfirmationScreen)
                and not app.screen.is_working
                and "未返回交付报告" in str(app.screen.query_one("#pack-status", Static).renderable)
            ),
        )
