from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from time import monotonic
from typing import Any

import pytest
from textual.widget import Widget
from textual.widgets import Button, Input, Select, Static

from csbox.api.repository import ApiRunRepository
from csbox.api.scenario import ScenarioLoader
from csbox.check.models import CheckFinding, CheckReport, CheckStatus, DetectedProject
from csbox.core.display_width import display_width
from csbox.core.models import EnvironmentSnapshot, HomeSnapshot, RecentApiRun, RecentExperiment
from csbox.lab.fake_data import FakeHomeDataSource
from csbox.lab.home_data import RealHomeDataSource
from csbox.lab.models import SessionPaths
from csbox.lab.repository import SessionRepository
from csbox.locales import load_locale
from csbox.tui.app import ApiApp, CheckApp, CSBoxApp, ReviewApp
from csbox.tui.dialogs.capture_title import CaptureTitleDialog
from csbox.tui.dialogs.confirm import ConfirmDialog
from csbox.tui.dialogs.lab_start import LabStartDialog
from csbox.tui.dialogs.unavailable import UnavailableDialog
from csbox.tui.lab_workflow import ShellOption
from csbox.tui.screens.api import ApiScreen
from csbox.tui.screens.home import HomeScreen
from csbox.tui.screens.pack import PackConfirmationScreen
from csbox.tui.screens.project_check import ProjectCheckScreen
from csbox.tui.screens.records import RecordsScreen
from csbox.tui.screens.review import ReviewController, ReviewScreen

SECRET = "CSBOX_SECRET_SENTINEL_task15"
LAYOUTS = ((80, 24), (100, 30), (120, 35), (160, 45))


@dataclass(frozen=True)
class PackPlan:
    source_root: Path
    included: tuple[str, ...] = ("课程实验/中文路径",)
    excluded: tuple[str, ...] = ("构建产物/输出",)
    rejected: tuple[str, ...] = ()
    source_bytes: int = 128
    project_type: str = "python"


class SentinelHomeDataSource:
    source_id = "sentinel"

    def __init__(self, project_dir: Path) -> None:
        self.project_dir = project_dir

    def get_home_snapshot(self, environment: EnvironmentSnapshot) -> HomeSnapshot:
        return HomeSnapshot(
            environment=environment,
            project_dir=self.project_dir / f"token={SECRET}",
            recent_experiments=[
                RecentExperiment(
                    name=f"中文实验 token={SECRET}",
                    status="completed",
                    duration="1 s",
                    demo=False,
                    cwd=self.project_dir / f"token={SECRET}",
                )
            ],
            recent_api_runs=[
                RecentApiRun(
                    id=f"token={SECRET}",
                    scenario_name=f"secret={SECRET}",
                    status="PASS",
                    elapsed_ms=1.0,
                )
            ],
        )


def environment() -> EnvironmentSnapshot:
    return EnvironmentSnapshot(
        os_name="Linux",
        os_version="test",
        python_version="3.12.3",
        shell="Bash",
        shell_executable="bash",
        powershell_51_available=False,
        powershell_7_available=False,
        is_wsl=True,
        terminal_columns=80,
        terminal_rows=24,
    )


def screen_text(screen: Widget) -> str:
    return "\n".join(
        [
            *(str(widget.renderable) for widget in screen.query(Static)),
            *(str(button.label) for button in screen.query(Button)),
            *(input_widget.value for input_widget in screen.query(Input)),
        ]
    )


async def _wait_until(pilot: Any, predicate: Callable[[], bool], *, timeout: float = 3.0) -> None:
    deadline = monotonic() + timeout
    while monotonic() < deadline:
        if predicate():
            return
        await pilot.pause()
    assert predicate()


def assert_visible_geometry(screen: Widget) -> None:
    for widget in screen.query("*"):
        if not _is_rendered(widget) or widget is screen:
            continue
        assert widget.outer_size.width > 0, f"{widget!r} has no width"
        assert widget.outer_size.height > 0, f"{widget!r} has no height"
        if not _has_scrollable_ancestor(widget):
            assert widget.region.x >= 0
            assert widget.region.y >= 0
            assert widget.region.right <= screen.size.width
            assert widget.region.bottom <= screen.size.height


def _is_rendered(widget: Widget) -> bool:
    current: Widget | None = widget
    while current is not None:
        if not current.display:
            return False
        current = current.parent
    return widget.visible


def _has_scrollable_ancestor(widget: Widget) -> bool:
    current = widget.parent
    while current is not None:
        if current.is_scrollable:
            return True
        current = current.parent
    return False


def assert_static_lines_fit(screen: Widget) -> None:
    for widget in screen.query(Static):
        if not _is_rendered(widget) or widget.size.width <= 0:
            continue
        for line in str(widget.renderable).splitlines():
            assert display_width(line) <= widget.content_region.width, (
                f"{widget!r} rendered {display_width(line)} cells "
                f"in {widget.content_region.width} cells"
            )


def make_review_session(tmp_path: Path) -> SessionPaths:
    paths = SessionPaths(tmp_path / "session-real")
    paths.root.mkdir(parents=True, exist_ok=True)
    paths.cast.write_text(
        "\n".join(
            [
                json.dumps({"version": 3, "term": {"cols": 12, "rows": 3}}),
                json.dumps([1.0, "o", "hello\n项目检查\n中文路径"]),
                json.dumps([1.0, "x", "0"]),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    paths.metadata.write_text(
        json.dumps({"id": "session-real", "cwd": str(tmp_path)}), encoding="utf-8"
    )
    return paths


@pytest.mark.asyncio
@pytest.mark.parametrize("size", LAYOUTS)
async def test_home_geometry_and_tab_navigation_are_usable(
    size: tuple[int, int], tmp_path: Path
) -> None:
    app = CSBoxApp(
        data_source=FakeHomeDataSource(),
        environment=environment(),
        locale=load_locale(),
        pack_plan_factory=lambda: PackPlan(tmp_path),
    )

    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        assert isinstance(app.screen, HomeScreen)
        assert app.screen.focused is not None
        assert app.screen.focused.id == "entry-start"
        assert_visible_geometry(app.screen)
        assert_static_lines_fit(app.screen)
        for expected_id in ("entry-records", "entry-check", "entry-pack", "entry-api"):
            await pilot.press("tab")
            await pilot.pause()
            assert app.screen.focused is not None
            assert app.screen.focused.id == expected_id
        for button in app.screen.query(Button):
            button.focus()
            button.scroll_visible(animate=False, immediate=True)
            await pilot.pause()
            assert button.region.y >= 0
            assert button.region.bottom <= size[1]
            assert display_width(str(button.label)) <= button.content_region.width
        app.screen.query_one("#entry-pack").focus()
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, PackConfirmationScreen)
        await pilot.press("escape")
        await pilot.pause()
        assert isinstance(app.screen, HomeScreen)


@pytest.mark.asyncio
async def test_home_resize_wide_narrow_wide_preserves_focus_and_state(tmp_path: Path) -> None:
    app = CSBoxApp(
        data_source=FakeHomeDataSource(),
        environment=environment(),
        locale=load_locale(),
        pack_plan_factory=lambda: PackPlan(tmp_path),
    )

    async with app.run_test(size=(160, 45)) as pilot:
        await pilot.pause()
        app.screen.query_one("#entry-check").focus()
        for size in ((80, 24), (160, 45)):
            await pilot.resize_terminal(*size)
            await pilot.pause()
            assert isinstance(app.screen, HomeScreen)
            assert app.screen.focused is not None
            assert_visible_geometry(app.screen)
        assert app.screen.focused.id == "entry-check"


@pytest.mark.asyncio
@pytest.mark.parametrize("size", LAYOUTS)
async def test_home_help_dialog_focuses_close_and_escape_returns_home(
    size: tuple[int, int],
) -> None:
    app = CSBoxApp(
        data_source=FakeHomeDataSource(), environment=environment(), locale=load_locale()
    )

    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        await pilot.press("f1")
        await pilot.pause()
        assert isinstance(app.screen, UnavailableDialog)
        assert_visible_geometry(app.screen)
        assert app.screen.focused is not None
        assert app.screen.focused.id == "dialog-close"
        assert SECRET not in screen_text(app.screen)
        await pilot.press("escape")
        await pilot.pause()
        assert isinstance(app.screen, HomeScreen)


@pytest.mark.asyncio
@pytest.mark.parametrize("size", LAYOUTS)
async def test_home_error_dialog_is_returnable_at_all_layouts(
    size: tuple[int, int],
) -> None:
    app = CSBoxApp(
        data_source=FakeHomeDataSource(),
        environment=environment(),
        locale=load_locale(),
    )

    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, LabStartDialog)
        assert_visible_geometry(app.screen)
        assert_static_lines_fit(app.screen)
        assert app.screen.focused is not None
        assert app.screen.focused.id == "lab-start-name"
        text = screen_text(app.screen)
        assert "当前项目" in text
        assert "csbox lab start" not in text
        assert "F5" not in text
        assert SECRET not in text
        await pilot.press("escape")
        await pilot.pause()
        assert isinstance(app.screen, HomeScreen)


@pytest.mark.asyncio
async def test_start_dialog_continuous_resize_preserves_cjk_form_state(
    tmp_path: Path,
) -> None:
    project_dir = tmp_path.joinpath(*(["中文课程项目"] * 18))
    app = CSBoxApp(
        data_source=RealHomeDataSource(SessionRepository.from_cwd(project_dir), project_dir),
        environment=environment(),
        locale=load_locale(),
        shell_options=(
            ShellOption("bash", "Bash"),
            ShellOption("zsh", "Zsh"),
        ),
    )

    async with app.run_test(size=(160, 45)) as pilot:
        await pilot.press("enter")
        await pilot.pause()
        name_input = app.screen.query_one("#lab-start-name", Input)
        name_input.value = "中文实验名称"
        await pilot.click("#lab-start-advanced")
        shell_select = app.screen.query_one("#lab-start-shell-select", Select)
        shell_select.value = "zsh"
        await pilot.pause()

        for size in ((120, 35), (80, 24), (160, 45), (100, 30)):
            await pilot.resize_terminal(*size)
            await pilot.pause()
            assert isinstance(app.screen, LabStartDialog)
            assert name_input.value == "中文实验名称"
            assert shell_select.display is True
            assert app.screen.selected_shell == "zsh"
            assert str(app.screen.query_one("#lab-start-shell-display", Static).renderable) == "Zsh"
            assert_visible_geometry(app.screen)
            assert_static_lines_fit(app.screen)


@pytest.mark.asyncio
@pytest.mark.parametrize("size", LAYOUTS)
async def test_review_keyboard_resize_and_cjk_layout_are_usable(
    tmp_path: Path, size: tuple[int, int]
) -> None:
    controller = ReviewController.from_session(make_review_session(tmp_path))
    controller.create_capture("中文 Capture " * 30)
    app = ReviewApp(
        controller=controller,
        locale=load_locale(),
    )

    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        assert isinstance(app.screen, ReviewScreen)
        assert app.screen.query_one("#review-title").content_region.height >= 1
        assert_visible_geometry(app.screen)
        assert_static_lines_fit(app.screen)
        if size[0] < 120:
            assert app.screen.query_one("#review-narrow").content_region.height > 0
        await pilot.press("c")
        await pilot.pause()
        assert isinstance(app.screen, CaptureTitleDialog)
        assert_visible_geometry(app.screen)
        assert app.screen.focused is not None
        assert app.screen.focused.id == "capture-title-input"
        assert SECRET not in screen_text(app.screen)
        await pilot.press("escape")
        await pilot.pause()
        assert isinstance(app.screen, ReviewScreen)
        await pilot.press("tab", "tab")
        assert app.screen.active_pane == "captures"
        await pilot.resize_terminal(160, 45)
        await pilot.pause()
        assert app.screen.is_wide is True
        assert_visible_geometry(app.screen)
        assert app.screen.active_pane == "captures"
        assert_static_lines_fit(app.screen)
        await pilot.resize_terminal(80, 24)
        await pilot.pause()
        assert app.screen.is_wide is False
        assert app.screen.active_pane == "captures"
        assert_static_lines_fit(app.screen)
        await pilot.press("space")
        await pilot.pause()
        assert app.screen.controller.playing is True
        await pilot.press("right")
        await pilot.pause()
        assert app.screen.controller.current_time > 0
        await pilot.press("space")
        await pilot.pause()
        assert app.screen.controller.playing is False
        await pilot.press("escape")
        await pilot.pause()
        assert app.is_running is False


@pytest.mark.asyncio
@pytest.mark.parametrize("size", LAYOUTS)
async def test_review_confirm_dialog_is_returnable_at_all_layouts(
    tmp_path: Path, size: tuple[int, int]
) -> None:
    controller = ReviewController.from_session(make_review_session(tmp_path))
    controller.create_capture("中文 Capture")
    app = ReviewApp(controller=controller, locale=load_locale())

    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        await pilot.press("delete")
        await pilot.pause()
        assert isinstance(app.screen, ConfirmDialog)
        assert_visible_geometry(app.screen)
        assert app.screen.focused is not None
        assert app.screen.focused.id == "confirm-no"
        assert SECRET not in screen_text(app.screen)
        await pilot.press("escape")
        await pilot.pause()
        assert isinstance(app.screen, ReviewScreen)
        assert len(controller.captures) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("size", LAYOUTS)
async def test_review_capture_title_dialogs_redact_secret_titles(
    tmp_path: Path, size: tuple[int, int]
) -> None:
    controller = ReviewController.from_session(make_review_session(tmp_path))
    controller.create_capture(f"token={SECRET}")
    app = ReviewApp(controller=controller, locale=load_locale())

    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        assert SECRET not in screen_text(app.screen)
        await pilot.press("e")
        await pilot.pause()
        assert isinstance(app.screen, CaptureTitleDialog)
        assert SECRET not in screen_text(app.screen)
        assert app.screen.query_one("#capture-title-input", Input).value != f"token={SECRET}"
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, ReviewScreen)
        assert controller.captures[0].title == f"token={SECRET}"
        assert SECRET not in screen_text(app.screen)
        await pilot.press("delete")
        await pilot.pause()
        assert isinstance(app.screen, ConfirmDialog)
        assert SECRET not in screen_text(app.screen)
        await pilot.press("escape")
        await pilot.pause()
        assert isinstance(app.screen, ReviewScreen)
        assert len(controller.captures) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("size", LAYOUTS)
async def test_check_wraps_long_cjk_findings_and_returns_at_all_layouts(
    tmp_path: Path, size: tuple[int, int]
) -> None:
    report = CheckReport(
        root=tmp_path,
        projects=(DetectedProject(kind="python", root=tmp_path, marker="pyproject.toml"),),
        findings=(
            CheckFinding(
                rule_id="finding",
                status=CheckStatus.FAIL,
                message="中文发现" * 50,
                path=Path("说明文件.md"),
            ),
        ),
    )
    app = CheckApp(report=report, locale=load_locale())

    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        assert isinstance(app.screen, ProjectCheckScreen)
        assert_visible_geometry(app.screen)
        assert_static_lines_fit(app.screen)
        for target in ((160, 45), (80, 24)):
            await pilot.resize_terminal(*target)
            await pilot.pause()
            assert_visible_geometry(app.screen)
            assert_static_lines_fit(app.screen)
        await pilot.press("escape")
        await pilot.pause()
        assert app.is_running is False


@pytest.mark.asyncio
async def test_check_sensitive_finding_never_renders_secret_sentinel(tmp_path: Path) -> None:
    report = CheckReport(
        root=tmp_path,
        deep_scan=CheckFinding(
            rule_id="deep-secret-scan",
            status=CheckStatus.FAIL,
            message=f"发现 {SECRET}，请轮换。",
            category="deep-secret-scan",
        ),
    )
    app = CheckApp(report=report, locale=load_locale())

    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        assert SECRET not in screen_text(app.screen)


@pytest.mark.asyncio
@pytest.mark.parametrize("size", LAYOUTS)
async def test_api_normal_resize_preserves_selected_row_and_pane(
    tmp_path: Path, size: tuple[int, int]
) -> None:
    scenario_dir = tmp_path / ".csbox" / "api" / "scenarios"
    scenario_dir.mkdir(parents=True)
    for name in ("a", "b"):
        (scenario_dir / f"{name}.toml").write_text(
            f'name = "场景 {name.upper()}"\n\n'
            '[[steps]]\nname = "中文步骤"\nmethod = "GET"\n'
            'url = "https://example.test/中文"\n',
            encoding="utf-8",
        )
    app = ApiApp(ApiRunRepository.from_cwd(tmp_path), ScenarioLoader(), lambda: None, load_locale())

    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        assert_visible_geometry(app.screen)
        assert_static_lines_fit(app.screen)
        await pilot.press("down")
        await pilot.pause()
        assert app.screen.focused is not None
        assert app.screen.focused.id == "scenario-1"
        assert app.screen.selected_scenario_index == 1
        assert "场景 B" in str(app.screen.query_one("#api-detail").renderable)
        await pilot.resize_terminal(160, 45)
        await pilot.pause()
        assert app.screen.is_wide is True
        assert app.screen.focused is not None
        assert app.screen.focused.id == "scenario-1"
        assert app.screen.selected_scenario_index == 1
        assert_static_lines_fit(app.screen)
        await pilot.press("right")
        await pilot.pause()
        assert app.screen.active_pane == "runs"
        await pilot.press("right")
        await pilot.pause()
        assert app.screen.active_pane == "detail"
        assert app.screen.focused is not None
        assert app.screen.focused.id == "api-detail"
        assert_static_lines_fit(app.screen)
        await pilot.resize_terminal(80, 24)
        await pilot.pause()
        assert app.screen.is_wide is False
        assert app.screen.active_pane == "detail"
        assert app.screen.focused is not None
        assert app.screen.focused.id == "api-detail"
        assert "场景 B" in str(app.screen.query_one("#api-detail").renderable)
        assert_static_lines_fit(app.screen)
        await pilot.resize_terminal(*size)
        await pilot.pause()
        assert app.screen.active_pane == "detail"
        assert app.screen.focused is not None
        assert app.screen.focused.id == "api-detail"
        await pilot.press("escape")
        await pilot.pause()
        assert app.is_running is False


@pytest.mark.asyncio
async def test_api_empty_state_keeps_focus_safe_and_resize_preserves_pane(
    tmp_path: Path,
) -> None:
    app = ApiApp(ApiRunRepository.from_cwd(tmp_path), ScenarioLoader(), lambda: None, load_locale())

    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        assert isinstance(app.screen, ApiScreen)
        assert app.screen.query_one("#api-title").content_region.height >= 1
        assert app.screen.focused is not None
        assert app.screen.focused.id == "api-quick-create"
        assert_static_lines_fit(app.screen)
        for key, pane in (("tab", "runs"), ("tab", "detail"), ("tab", "scenarios")):
            await pilot.press(key)
            await pilot.pause()
            assert app.screen.active_pane == pane
        await pilot.resize_terminal(160, 45)
        await pilot.pause()
        assert app.screen.is_wide is True
        assert app.screen.active_pane == "scenarios"
        await pilot.resize_terminal(80, 24)
        await pilot.pause()
        assert app.screen.is_wide is False
        assert app.screen.active_pane == "scenarios"
        await pilot.press("escape")
        await pilot.pause()
        assert app.is_running is False


@pytest.mark.asyncio
@pytest.mark.parametrize("size", LAYOUTS)
@pytest.mark.parametrize("corrupt", (False, True))
async def test_home_records_screen_ignores_session_corruption(
    tmp_path: Path, size: tuple[int, int], corrupt: bool
) -> None:
    sessions_root = tmp_path / ".csbox" / "sessions"
    if corrupt:
        corrupt_root = sessions_root / "corrupt-session"
        corrupt_root.mkdir(parents=True)
        (corrupt_root / "metadata.json").write_text(f'{{"id":"{SECRET}"', encoding="utf-8")
    app = CSBoxApp(
        data_source=RealHomeDataSource(SessionRepository(sessions_root), tmp_path),
        environment=environment(),
        locale=load_locale(),
    )

    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        app.screen.query_one("#entry-records").focus()
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, RecordsScreen)
        assert_visible_geometry(app.screen)
        text = screen_text(app.screen)
        assert "下一阶段" not in text
        assert "暂无实验记录" in text
        assert "lab list" not in text
        assert "F5" not in text
        assert SECRET not in text
        await _wait_until(
            pilot,
            lambda: (
                isinstance(app.screen, RecordsScreen)
                and app.screen.focused is not None
                and app.screen.focused.id == "records-start"
            ),
        )
        await pilot.press("escape")
        await pilot.pause()
        assert isinstance(app.screen, HomeScreen)


@pytest.mark.asyncio
@pytest.mark.parametrize("size", LAYOUTS)
async def test_home_summary_does_not_render_sentinel_values(
    tmp_path: Path, size: tuple[int, int]
) -> None:
    app = CSBoxApp(
        data_source=SentinelHomeDataSource(tmp_path),
        environment=environment(),
        locale=load_locale(),
    )

    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        assert SECRET not in screen_text(app.screen)


@pytest.mark.asyncio
@pytest.mark.parametrize("size", LAYOUTS)
async def test_pack_normal_confirmation_is_keyboard_actionable_at_all_layouts(
    tmp_path: Path, size: tuple[int, int]
) -> None:
    calls: list[str] = []
    app = CSBoxApp(
        data_source=FakeHomeDataSource(),
        environment=environment(),
        locale=load_locale(),
        pack_plan_factory=lambda: PackPlan(tmp_path),
        pack_action=lambda _plan: calls.append("packed"),
    )

    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        app.screen.query_one("#entry-pack").focus()
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, PackConfirmationScreen)
        assert_visible_geometry(app.screen)
        app.screen.query_one("#pack-confirm").focus()
        await pilot.press("enter")
        await pilot.pause()
        assert calls == ["packed"]
        assert "打包完成" in screen_text(app.screen)
        assert SECRET not in screen_text(app.screen)
        assert_static_lines_fit(app.screen)
        await pilot.press("escape")
        await pilot.pause()
        assert isinstance(app.screen, HomeScreen)


@pytest.mark.asyncio
@pytest.mark.parametrize("size", LAYOUTS)
async def test_pack_error_status_is_bounded_and_secret_safe(
    tmp_path: Path, size: tuple[int, int]
) -> None:
    def fail(_plan: object) -> None:
        raise RuntimeError(SECRET)

    app = CSBoxApp(
        data_source=FakeHomeDataSource(),
        environment=environment(),
        locale=load_locale(),
        pack_plan_factory=lambda: PackPlan(tmp_path),
        pack_action=fail,
    )

    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        app.screen.query_one("#entry-pack").focus()
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, PackConfirmationScreen)
        assert_visible_geometry(app.screen)
        app.screen.query_one("#pack-confirm").focus()
        await pilot.press("enter")
        await pilot.pause()
        assert "打包失败" in screen_text(app.screen)
        assert SECRET not in screen_text(app.screen)
        assert_static_lines_fit(app.screen)
        for target in ((160, 45), (80, 24)):
            await pilot.resize_terminal(*target)
            await pilot.pause()
            assert_visible_geometry(app.screen)
            assert_static_lines_fit(app.screen)
        await pilot.press("escape")
        await pilot.pause()
        assert isinstance(app.screen, HomeScreen)


@pytest.mark.asyncio
@pytest.mark.parametrize("size", LAYOUTS)
async def test_pack_rejected_plan_has_no_focusable_confirm_action(
    tmp_path: Path, size: tuple[int, int]
) -> None:
    plan = PackPlan(tmp_path, rejected=(f"private/{SECRET}:private-key",))
    calls: list[str] = []
    app = CSBoxApp(
        data_source=FakeHomeDataSource(),
        environment=environment(),
        locale=load_locale(),
        pack_plan_factory=lambda: plan,
        pack_action=lambda _plan: calls.append("packed"),
    )

    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        app.screen.query_one("#entry-pack").focus()
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, PackConfirmationScreen)
        assert_visible_geometry(app.screen)
        assert app.screen.query_one("#pack-title").content_region.height >= 1
        confirm = app.screen.query_one("#pack-confirm", Button)
        assert confirm.disabled is True
        assert app.screen.focused is not confirm
        assert SECRET not in screen_text(app.screen)
        assert_static_lines_fit(app.screen)
        assert calls == []
