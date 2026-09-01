from __future__ import annotations

import html
import re
import unicodedata
from pathlib import Path
from types import MethodType

import pytest
from textual.containers import VerticalScroll
from textual.widgets import Input, Select, Static

from csbox.core.models import EnvironmentSnapshot, HomeSnapshot
from csbox.lab.repository import SessionRepository
from csbox.lab.service import LabService
from csbox.locales import load_locale
from csbox.tui.app import CSBoxApp
from csbox.tui.dialogs.lab_start import LabStartDialog
from csbox.tui.lab_workflow import LabStartRequest, ShellOption
from csbox.tui.screens.home import HomeScreen
from csbox.tui.screens.records import RecordsScreen

PLACEHOLDER = "例如：计算机网络实验一"
LAYOUTS = ((80, 24), (100, 30), (120, 35), (160, 45))


def _environment() -> EnvironmentSnapshot:
    return EnvironmentSnapshot(
        os_name="Linux",
        os_version="test",
        python_version="3.12.3",
        shell="Bash",
        shell_executable="bash",
        powershell_51_available=False,
        powershell_7_available=False,
        is_wsl=False,
        terminal_columns=80,
        terminal_rows=24,
    )


def _shells() -> tuple[ShellOption, ...]:
    return (
        ShellOption(value="bash", label="Bash"),
        ShellOption(value="zsh", label="Zsh"),
    )


class CountingHomeDataSource:
    source_id = "counting-keyboard-test"

    def __init__(self, project_dir: Path) -> None:
        self.project_dir = project_dir
        self.calls = 0

    def get_home_snapshot(self, environment: EnvironmentSnapshot) -> HomeSnapshot:
        self.calls += 1
        return HomeSnapshot(environment=environment, project_dir=self.project_dir)


def _app(tmp_path: Path, *, data_source: object | None = None) -> CSBoxApp:
    return CSBoxApp(
        data_source=data_source or CountingHomeDataSource(tmp_path),
        environment=_environment(),
        locale=load_locale(),
        shell_options=_shells(),
    )


def _screenshot_text(app: CSBoxApp) -> str:
    without_tags = re.sub(r"<[^>]+>", "", app.export_screenshot())
    return "".join(html.unescape(without_tags).splitlines())


async def _open_start_dialog(app: CSBoxApp, pilot: object) -> LabStartDialog:
    await pilot.press("enter")
    await pilot.pause()
    assert isinstance(app.screen, LabStartDialog)
    return app.screen


async def _type_text(pilot: object, name_input: Input, value: str) -> None:
    for character in value:
        if unicodedata.combining(character):
            name_input.insert_text_at_cursor(character)
        else:
            await pilot.press(character)


@pytest.mark.asyncio
async def test_home_up_down_moves_focus_and_wraps(tmp_path: Path) -> None:
    app = _app(tmp_path)

    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        assert app.screen.focused is not None
        assert app.screen.focused.id == "entry-start"

        await pilot.press("down")
        assert app.screen.focused is not None
        assert app.screen.focused.id == "entry-records"

        await pilot.press("up")
        assert app.screen.focused is not None
        assert app.screen.focused.id == "entry-start"

        await pilot.press("up")
        assert app.screen.focused is not None
        assert app.screen.focused.id == "entry-api"

        await pilot.press("down")
        assert app.screen.focused is not None
        assert app.screen.focused.id == "entry-start"


@pytest.mark.asyncio
async def test_home_enter_activates_arrow_focused_button_and_tab_still_works(
    tmp_path: Path,
) -> None:
    app = _app(tmp_path)

    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        await pilot.press("down", "enter")
        await pilot.pause()
        assert isinstance(app.screen, RecordsScreen)

        await pilot.press("escape")
        await pilot.pause()
        assert isinstance(app.screen, HomeScreen)
        assert app.screen.focused is not None
        assert app.screen.focused.id == "entry-records"
        await pilot.press("tab")
        assert app.screen.focused is not None
        assert app.screen.focused.id == "entry-evidence"


@pytest.mark.asyncio
@pytest.mark.parametrize("entry_id", ("entry-check", "entry-pack", "entry-api"))
async def test_home_resize_keeps_the_focused_entry_visible(
    tmp_path: Path,
    entry_id: str,
) -> None:
    app = _app(tmp_path)

    async with app.run_test(size=(160, 45)) as pilot:
        await pilot.pause()
        entry = app.screen.query_one(f"#{entry_id}")
        entry.focus()
        entry.scroll_visible(animate=False, immediate=True)
        await pilot.resize_terminal(80, 24)
        await pilot.pause()

        main_scroll = app.screen.query_one("#main-scroll", VerticalScroll)
        assert app.screen.focused is entry
        assert main_scroll.content_region.contains_region(entry.region)


@pytest.mark.asyncio
async def test_start_dialog_up_down_moves_through_visible_controls(tmp_path: Path) -> None:
    app = _app(tmp_path)

    async with app.run_test(size=(80, 24)) as pilot:
        dialog = await _open_start_dialog(app, pilot)
        assert dialog.focused is not None
        assert dialog.focused.id == "lab-start-name"

        await pilot.press("down")
        assert dialog.focused is not None
        assert dialog.focused.id == "lab-start-advanced"

        await pilot.press("up")
        assert dialog.focused is not None
        assert dialog.focused.id == "lab-start-name"

        await pilot.press("down", "down")
        assert dialog.focused is not None
        assert dialog.focused.id == "lab-start-confirm"

        await pilot.press("down")
        assert dialog.focused is not None
        assert dialog.focused.id == "lab-start-cancel"


@pytest.mark.asyncio
async def test_start_dialog_wraps_and_tab_remains_compatible(tmp_path: Path) -> None:
    app = _app(tmp_path)

    async with app.run_test(size=(80, 24)) as pilot:
        dialog = await _open_start_dialog(app, pilot)
        await pilot.press("up")
        assert dialog.focused is not None
        assert dialog.focused.id == "lab-start-cancel"

        await pilot.press("down")
        assert dialog.focused is not None
        assert dialog.focused.id == "lab-start-name"

        await pilot.press("tab")
        assert dialog.focused is not None
        assert dialog.focused.id == "lab-start-advanced"

        await pilot.press("shift+tab")
        assert dialog.focused is not None
        assert dialog.focused.id == "lab-start-name"


@pytest.mark.asyncio
async def test_enter_on_valid_name_submits_start_request(tmp_path: Path) -> None:
    app = _app(tmp_path)

    async with app.run_test(size=(80, 24)) as pilot:
        await _open_start_dialog(app, pilot)
        await pilot.press(*"网络Lab实验ABC123", "enter")
        await pilot.pause()

    assert app.return_value == LabStartRequest("网络Lab实验ABC123", "bash")


@pytest.mark.asyncio
async def test_enter_on_empty_name_stays_and_shows_validation(tmp_path: Path) -> None:
    app = _app(tmp_path)

    async with app.run_test(size=(80, 24)) as pilot:
        dialog = await _open_start_dialog(app, pilot)
        await pilot.press("enter")
        await pilot.pause()

        assert isinstance(app.screen, LabStartDialog)
        assert dialog.focused is not None
        assert dialog.focused.id == "lab-start-name"
        assert "请输入实验名称" in str(dialog.query_one("#lab-start-validation", Static).renderable)


@pytest.mark.asyncio
async def test_enter_on_start_button_submits_and_cancel_button_returns_home(
    tmp_path: Path,
) -> None:
    start_app = _app(tmp_path)
    async with start_app.run_test(size=(80, 24)) as pilot:
        dialog = await _open_start_dialog(start_app, pilot)
        dialog.query_one("#lab-start-name", Input).value = "计算机网络实验"
        await pilot.press("down", "down")
        assert dialog.focused is not None
        assert dialog.focused.id == "lab-start-confirm"
        await pilot.press("enter")
        await pilot.pause()
    assert start_app.return_value == LabStartRequest("计算机网络实验", "bash")

    cancel_app = _app(tmp_path)
    async with cancel_app.run_test(size=(80, 24)) as pilot:
        await _open_start_dialog(cancel_app, pilot)
        await pilot.press("down", "down", "down", "enter")
        await pilot.pause()
        assert isinstance(cancel_app.screen, HomeScreen)


@pytest.mark.asyncio
async def test_escape_returns_from_start_dialog_to_home(tmp_path: Path) -> None:
    app = _app(tmp_path)

    async with app.run_test(size=(80, 24)) as pilot:
        await _open_start_dialog(app, pilot)
        await pilot.press("escape")
        await pilot.pause()
        assert isinstance(app.screen, HomeScreen)


@pytest.mark.asyncio
async def test_select_owns_direction_and_enter_keys_only_while_menu_is_open(
    tmp_path: Path,
) -> None:
    app = _app(tmp_path)

    async with app.run_test(size=(80, 24)) as pilot:
        dialog = await _open_start_dialog(app, pilot)
        await pilot.press("down", "enter")
        shell_select = dialog.query_one("#lab-start-shell-select", Select)
        assert shell_select.display is True

        await pilot.press("down")
        assert dialog.focused is shell_select
        assert shell_select.expanded is False

        await pilot.press("enter")
        await pilot.pause()
        assert shell_select.expanded is True

        await pilot.press("down", "up", "down", "enter")
        await pilot.pause()
        assert shell_select.expanded is False
        assert shell_select.value == "zsh"
        assert dialog.selected_shell == "zsh"
        assert dialog.focused is shell_select

        await pilot.press("down")
        assert dialog.focused is not None
        assert dialog.focused.id == "lab-start-confirm"


@pytest.mark.asyncio
async def test_select_escape_closes_menu_before_dialog(tmp_path: Path) -> None:
    app = _app(tmp_path)

    async with app.run_test(size=(80, 24)) as pilot:
        dialog = await _open_start_dialog(app, pilot)
        await pilot.press("down", "enter", "down", "enter")
        shell_select = dialog.query_one("#lab-start-shell-select", Select)
        assert shell_select.expanded is True

        await pilot.press("escape")
        await pilot.pause()
        assert isinstance(app.screen, LabStartDialog)
        assert shell_select.expanded is False
        assert dialog.focused is shell_select

        await pilot.press("escape")
        await pilot.pause()
        assert isinstance(app.screen, HomeScreen)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "value",
    (
        "tui:",
        "计算机网络实验",
        "网络Lab实验ABC123",
        "e\N{COMBINING ACUTE ACCENT}网络实验",
    ),
)
async def test_placeholder_is_removed_from_render_after_printable_input(
    tmp_path: Path,
    value: str,
) -> None:
    app = _app(tmp_path)

    async with app.run_test(size=(80, 24)) as pilot:
        dialog = await _open_start_dialog(app, pilot)
        name_input = dialog.query_one("#lab-start-name", Input)
        assert name_input.value == ""
        assert PLACEHOLDER in _screenshot_text(app)

        await _type_text(pilot, name_input, value)
        await pilot.pause()

        assert name_input.value == value
        rendered = _screenshot_text(app)
        assert value in rendered
        assert PLACEHOLDER not in rendered


@pytest.mark.asyncio
async def test_backspace_to_empty_restores_placeholder(tmp_path: Path) -> None:
    app = _app(tmp_path)

    async with app.run_test(size=(80, 24)) as pilot:
        dialog = await _open_start_dialog(app, pilot)
        name_input = dialog.query_one("#lab-start-name", Input)
        await pilot.press(*"中文A")
        assert PLACEHOLDER not in _screenshot_text(app)

        await pilot.press("backspace", "backspace", "backspace")
        await pilot.pause()

        assert name_input.value == ""
        assert PLACEHOLDER in _screenshot_text(app)


@pytest.mark.asyncio
async def test_continuous_typing_does_not_relayout_or_call_heavy_boundaries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_source = CountingHomeDataSource(tmp_path)
    app = _app(tmp_path, data_source=data_source)

    def forbidden(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("typing crossed a heavy Lab or repository boundary")

    monkeypatch.setattr(LabService, "resolve_shell", forbidden)
    monkeypatch.setattr(SessionRepository, "list_sessions", forbidden)

    async with app.run_test(size=(80, 24)) as pilot:
        dialog = await _open_start_dialog(app, pilot)
        layout_refreshes = 0
        original_refresh_layout = dialog._refresh_layout

        def count_refresh_layout(self: object, *args: object, **kwargs: object) -> None:
            nonlocal layout_refreshes
            layout_refreshes += 1
            original_refresh_layout(*args, **kwargs)

        dialog._refresh_layout = MethodType(count_refresh_layout, dialog)
        sample = "计算机网络Lab实验ABC123" * 2
        await pilot.press(*sample)
        await pilot.pause()

        assert dialog.query_one("#lab-start-name", Input).value == sample
        assert layout_refreshes == 0
        assert data_source.calls == 1


@pytest.mark.asyncio
async def test_cjk_value_cursor_and_focus_survive_continuous_resize(
    tmp_path: Path,
) -> None:
    app = _app(tmp_path)
    chunks = (
        "计算机网络实验",
        "网络Lab实验ABC123",
        "长中文实验名" * 6,
        "e\N{COMBINING ACUTE ACCENT}",
    )

    async with app.run_test(size=LAYOUTS[0]) as pilot:
        dialog = await _open_start_dialog(app, pilot)
        name_input = dialog.query_one("#lab-start-name", Input)
        expected = ""

        for size, chunk in zip(LAYOUTS, chunks, strict=True):
            await pilot.resize_terminal(*size)
            await _type_text(pilot, name_input, chunk)
            await pilot.pause()
            expected += chunk

            assert name_input.value == expected
            assert name_input.cursor_position == len(expected)
            assert dialog.focused is name_input
            assert PLACEHOLDER not in _screenshot_text(app)
