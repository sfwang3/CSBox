from pathlib import Path

import pytest

from csbox.core.display_width import display_width
from csbox.core.events import TerminalSize
from csbox.core.models import EnvironmentSnapshot, HomeSnapshot, RecentExperiment
from csbox.lab.fake_data import FakeHomeDataSource
from csbox.lab.home_data import RealHomeDataSource
from csbox.lab.repository import SessionRepository
from csbox.locales import load_locale
from csbox.tui.app import CSBoxApp
from csbox.tui.screens.home import HomeScreen
from csbox.tui.screens.review import ReviewScreen


class LongHomeDataSource:
    source_id = "long"

    def get_home_snapshot(self, environment: EnvironmentSnapshot) -> HomeSnapshot:
        return HomeSnapshot(
            environment=environment,
            recent_experiments=[
                RecentExperiment(
                    name="计算机网络实验一" * 30,
                    status="completed",
                    duration="8 min",
                    demo=False,
                    capture_count=2,
                    platform="windows",
                    cwd=Path("C:/Users/测试用户/桌面/实验一") / ("课程实验" * 30),
                )
            ],
        )


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
        for button in app.screen.query("Button"):
            button.focus()
            await pilot.pause()
            assert button.region.y < 24


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

        message = str(app.screen.query_one("#dialog-message").renderable)
        assert "csbox lab start" in message
        assert "F5" in message
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


@pytest.mark.asyncio
async def test_runtime_home_uses_real_empty_state_without_demo_data(tmp_path: Path) -> None:
    app = CSBoxApp(
        data_source=RealHomeDataSource(SessionRepository(tmp_path / "sessions"), tmp_path),
        environment=_environment(),
        locale=load_locale(),
    )

    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()

        assert isinstance(app.screen, HomeScreen)
        assert "[DEMO]" not in str(app.screen.query_one("#demo-note").renderable)
        assert "暂无 session" in str(app.screen.query_one("#recent-content").renderable)
        environment_text = str(app.screen.query_one("#environment-content").renderable)
        assert "项目路径:" in environment_text
        assert str(tmp_path)[:32] in environment_text


@pytest.mark.asyncio
async def test_home_recent_cjk_fields_are_truncated_for_80_columns() -> None:
    app = CSBoxApp(
        data_source=LongHomeDataSource(),
        environment=_environment(),
        locale=load_locale(),
    )

    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()

        content = str(app.screen.query_one("#recent-content").renderable)
        panel = app.screen.query_one("#recent-panel")
        assert "计算机网络实验一" * 30 not in content
        assert "C:/Users/测试用户/桌面/实验一" + ("课程实验" * 30) not in content
        assert "…" in content
        assert all(display_width(line) <= panel.size.width for line in content.splitlines())


@pytest.mark.asyncio
async def test_home_replay_entry_opens_latest_real_session(tmp_path: Path) -> None:
    repository = SessionRepository.from_cwd(tmp_path)
    paths = repository.create_running(
        "回看实验",
        platform="linux",
        shell="bash",
        shell_version="5.2",
        size=TerminalSize(80, 24),
        cwd=tmp_path,
    )
    paths.cast.write_text(
        '{"version":3,"term":{"cols":8,"rows":2}}\n',
        encoding="utf-8",
    )
    repository.finish(paths, "completed", exit_code=0)
    app = CSBoxApp(
        data_source=RealHomeDataSource(repository, tmp_path),
        environment=_environment(),
        locale=load_locale(),
    )

    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        app.screen.query_one("#entry-replay").focus()
        await pilot.press("enter")
        await pilot.pause()

        assert isinstance(app.screen, ReviewScreen)
