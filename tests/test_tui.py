from pathlib import Path

import pytest
from textual.widgets import Button, Input, Select, Static

from csbox.core.display_width import display_width
from csbox.core.models import EnvironmentSnapshot, HomeSnapshot
from csbox.lab.fake_data import FakeHomeDataSource
from csbox.lab.home_data import RealHomeDataSource
from csbox.lab.repository import SessionRepository
from csbox.locales import load_locale
from csbox.tui import lab_workflow
from csbox.tui.app import CSBoxApp
from csbox.tui.dialogs.lab_start import LabStartDialog
from csbox.tui.lab_workflow import LabStartRequest, ShellOption
from csbox.tui.screens.home import HomeScreen
from csbox.tui.screens.records import RecordsScreen


def _environment() -> EnvironmentSnapshot:
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


def _shells() -> tuple[ShellOption, ...]:
    return (
        ShellOption(value="bash", label="Bash"),
        ShellOption(value="zsh", label="Zsh"),
    )


def _screen_text(app: CSBoxApp) -> str:
    return "\n".join(str(widget.renderable) for widget in app.screen.query(Static))


@pytest.mark.asyncio
async def test_home_is_task_first_and_has_the_required_button_order() -> None:
    app = CSBoxApp(
        data_source=FakeHomeDataSource(),
        environment=_environment(),
        locale=load_locale(),
        shell_options=_shells(),
    )

    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()

        assert isinstance(app.screen, HomeScreen)
        assert [button.id for button in app.screen.query(Button)] == [
            "entry-start",
            "entry-records",
            "entry-evidence",
            "entry-submit",
            "entry-check",
            "entry-pack",
            "entry-api",
        ]
        assert app.screen.focused is not None
        assert app.screen.focused.id == "entry-start"
        assert not app.screen.query("#environment-panel")
        assert not app.screen.query("#recent-panel")
        text = _screen_text(app)
        assert "Python" not in text
        assert "WSL" not in text
        assert "终端尺寸" not in text
        assert "F5" not in text
        assert "csbox lab" not in text
        assert "F12" in text
        assert "exit" in text
        assert "实验记录" in text


@pytest.mark.asyncio
async def test_start_dialog_has_chinese_name_friendly_shell_and_read_only_project(
    tmp_path: Path,
) -> None:
    app = CSBoxApp(
        data_source=RealHomeDataSource(SessionRepository.from_cwd(tmp_path), tmp_path),
        environment=_environment(),
        locale=load_locale(),
        shell_options=_shells(),
    )

    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()

        assert isinstance(app.screen, LabStartDialog)
        assert app.screen.focused is not None
        assert app.screen.focused.id == "lab-start-name"
        assert [widget.id for widget in app.screen.query(Input)] == ["lab-start-name"]
        project_widget = app.screen.query_one("#lab-start-project", Static)
        project_text = str(project_widget.renderable)
        assert str(tmp_path).startswith(project_text.removesuffix("…"))
        assert display_width(project_text) <= project_widget.content_region.width
        assert str(app.screen.query_one("#lab-start-shell-display", Static).renderable) == "Bash"
        shell_select = app.screen.query_one("#lab-start-shell-select", Select)
        assert shell_select.display is False

        await pilot.click("#lab-start-advanced")
        await pilot.pause()
        assert shell_select.display is True
        assert "未知" not in _screen_text(app)

        app.screen.query_one("#lab-start-name", Input).value = "  中文实验名称  "
        await pilot.click("#lab-start-confirm")
        await pilot.pause()

    assert app.return_value == LabStartRequest(
        experiment_name="中文实验名称",
        shell="bash",
    )


@pytest.mark.asyncio
async def test_start_dialog_cancel_returns_home() -> None:
    app = CSBoxApp(
        data_source=FakeHomeDataSource(),
        environment=_environment(),
        locale=load_locale(),
        shell_options=_shells(),
    )

    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, LabStartDialog)
        await pilot.press("escape")
        await pilot.pause()
        assert isinstance(app.screen, HomeScreen)


@pytest.mark.asyncio
async def test_start_dialog_keeps_focus_and_shows_a_friendly_validation_error() -> None:
    app = CSBoxApp(
        data_source=FakeHomeDataSource(),
        environment=_environment(),
        locale=load_locale(),
        shell_options=_shells(),
    )

    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.press("enter")
        await pilot.pause()
        app.screen.query_one("#lab-start-name", Input).value = "  "
        await pilot.click("#lab-start-confirm")
        await pilot.pause()

        assert isinstance(app.screen, LabStartDialog)
        assert "请输入实验名称" in str(
            app.screen.query_one("#lab-start-validation", Static).renderable
        )
        assert app.screen.focused is not None
        assert app.screen.focused.id == "lab-start-name"


@pytest.mark.asyncio
async def test_unavailable_shell_blocks_start_without_showing_unknown() -> None:
    app = CSBoxApp(
        data_source=FakeHomeDataSource(),
        environment=_environment(),
        locale=load_locale(),
        shell_options=(),
        shell_error=lab_workflow.no_available_shell_message("Linux"),
    )

    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.press("enter")
        await pilot.pause()

        assert isinstance(app.screen, LabStartDialog)
        assert app.screen.query_one("#lab-start-confirm", Button).disabled is True
        text = _screen_text(app)
        assert "未找到 Bash 或 Zsh" in text
        assert "未知" not in text

        app.screen.query_one("#lab-start-name", Input).value = "中文实验"
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, LabStartDialog)
        assert app.return_value is None
        assert "未找到 Bash 或 Zsh" in str(
            app.screen.query_one("#lab-start-validation", Static).renderable
        )


@pytest.mark.asyncio
async def test_windows_unavailable_shell_shows_power_shell_recovery() -> None:
    environment = _environment().model_copy(
        update={"os_name": "Windows", "shell": "", "shell_executable": None}
    )
    app = CSBoxApp(
        data_source=FakeHomeDataSource(),
        environment=environment,
        locale=load_locale(),
        shell_options=(),
        shell_error=lab_workflow.no_available_shell_message("Windows"),
    )

    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.press("enter")
        await pilot.pause()

        text = _screen_text(app)
        assert app.screen.query_one("#lab-start-confirm", Button).disabled is True
        assert "未找到可用的 PowerShell" in text
        assert "PowerShell 7" in text


@pytest.mark.asyncio
async def test_records_entry_stays_in_tui_without_cli_or_refresh_instructions() -> None:
    app = CSBoxApp(
        data_source=FakeHomeDataSource(),
        environment=_environment(),
        locale=load_locale(),
        shell_options=_shells(),
    )

    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        app.screen.query_one("#entry-records", Button).focus()
        await pilot.press("enter")
        await pilot.pause()

        assert isinstance(app.screen, RecordsScreen)
        text = _screen_text(app)
        assert "下一阶段" not in text
        assert "lab list" not in text
        assert "F5" not in text


@pytest.mark.asyncio
async def test_home_screen_tracks_wide_layout_at_120_columns() -> None:
    app = CSBoxApp(
        data_source=FakeHomeDataSource(),
        environment=_environment(),
        locale=load_locale(),
        shell_options=_shells(),
    )

    async with app.run_test(size=(120, 35)) as pilot:
        await pilot.pause()
        assert isinstance(app.screen, HomeScreen)
        assert app.screen.is_wide is True


def test_home_snapshot_contract_still_accepts_current_project_only(tmp_path: Path) -> None:
    snapshot = HomeSnapshot(environment=_environment(), project_dir=tmp_path)
    assert snapshot.project_dir == tmp_path
