import pytest

from csbox.core.models import EnvironmentSnapshot
from csbox.lab.fake_data import FakeHomeDataSource
from csbox.locales import load_locale
from csbox.tui.app import CSBoxApp
from csbox.tui.screens.home import HomeScreen


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


@pytest.mark.asyncio
async def test_home_screen_mounts_at_80_by_24_with_demo_data() -> None:
    app = CSBoxApp(
        data_source=FakeHomeDataSource(),
        environment=_environment(),
        locale=load_locale(),
    )

    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()

        assert isinstance(app.screen, HomeScreen)
        assert str(app.screen.query_one("#brand-name").renderable) == "CSBox"
        assert app.screen.query_one("#brand-subtitle")
        assert len(app.screen.query("Button")) == 5
        assert "[DEMO]" in str(app.screen.query_one("#demo-note").renderable)
        assert app.screen.is_wide is False


@pytest.mark.asyncio
async def test_entry_opens_localized_unavailable_dialog() -> None:
    app = CSBoxApp(
        data_source=FakeHomeDataSource(),
        environment=_environment(),
        locale=load_locale(),
    )

    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        app.screen.query_one("#entry-start").focus()
        await pilot.press("enter")
        await pilot.pause()

        assert str(app.screen.query_one("#dialog-message").renderable) == "该功能将在后续版本实现。"
        assert app.screen.query_one("#dialog-close")


@pytest.mark.asyncio
async def test_home_screen_tracks_wide_layout_at_120_columns() -> None:
    app = CSBoxApp(
        data_source=FakeHomeDataSource(),
        environment=_environment(),
        locale=load_locale(),
    )

    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()

        assert isinstance(app.screen, HomeScreen)
        assert app.screen.is_wide is True
