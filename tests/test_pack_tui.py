from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from textual.widgets import Button, Static

from csbox.core.display_width import display_width
from csbox.core.models import EnvironmentSnapshot
from csbox.lab.fake_data import FakeHomeDataSource
from csbox.lab.home_data import RealHomeDataSource
from csbox.lab.repository import SessionRepository
from csbox.locales import load_locale
from csbox.tui.app import CSBoxApp
from csbox.tui.screens.pack import PackConfirmationScreen


def _screen_text(screen: Any) -> str:
    widgets = screen.query(Static)
    buttons = screen.query(Button)
    return "\n".join(
        [
            *(str(widget.renderable) for widget in widgets),
            *(str(button.label) for button in buttons),
        ]
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


@dataclass(frozen=True)
class PackPlan:
    source_root: Path
    included: tuple[str, ...] = ("README.md", "src/main.py")
    excluded: tuple[str, ...] = (".git:directory", ".env:pattern")
    rejected: tuple[str, ...] = ()
    source_bytes: int = 128
    project_type: str = "python"


@pytest.mark.asyncio
async def test_home_pack_entry_opens_confirmation_and_escape_has_no_side_effect(
    tmp_path: Path,
) -> None:
    calls: list[str] = []
    app = CSBoxApp(
        data_source=FakeHomeDataSource(),
        environment=_environment(),
        locale=load_locale(),
        pack_plan_factory=lambda: PackPlan(tmp_path),
        pack_action=lambda _plan: calls.append("packed"),
    )

    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        app.screen.query_one("#entry-pack").focus()
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, PackConfirmationScreen)
        assert "included" in _screen_text(app.screen)
        assert "README.md" in _screen_text(app.screen)
        await pilot.press("escape")
        await pilot.pause()
        assert calls == []


@pytest.mark.asyncio
async def test_pack_confirmation_requires_enter_before_pack_action(tmp_path: Path) -> None:
    calls: list[str] = []
    app = CSBoxApp(
        data_source=FakeHomeDataSource(),
        environment=_environment(),
        locale=load_locale(),
        pack_plan_factory=lambda: PackPlan(tmp_path),
        pack_action=lambda _plan: calls.append("packed"),
    )

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        app.screen.query_one("#entry-pack").focus()
        await pilot.press("enter")
        await pilot.pause()
        app.screen.query_one("#pack-confirm").focus()
        await pilot.press("enter")
        await pilot.pause()
        assert calls == ["packed"]
        assert "打包完成" in _screen_text(app.screen)


@pytest.mark.asyncio
async def test_pack_rejected_plan_has_actionable_error_and_never_leaks_values(
    tmp_path: Path,
) -> None:
    secret = "CSBOX_SECRET_SENTINEL_pack_tui"
    plan = PackPlan(tmp_path, rejected=(f"secrets/{secret}:private-key",))
    app = CSBoxApp(
        data_source=FakeHomeDataSource(),
        environment=_environment(),
        locale=load_locale(),
        pack_plan_factory=lambda: plan,
    )

    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        app.screen.query_one("#entry-pack").focus()
        await pilot.press("enter")
        await pilot.pause()
        text = _screen_text(app.screen)
        assert "拒绝" in text
        assert "Enter" in text
        assert secret not in text


@pytest.mark.asyncio
async def test_pack_confirmation_wraps_cjk_items_at_narrow_width(tmp_path: Path) -> None:
    plan = PackPlan(
        tmp_path,
        included=("课程实验/" + "中文路径" * 40,),
        excluded=("构建产物/" + "输出" * 40,),
    )
    app = CSBoxApp(
        data_source=FakeHomeDataSource(),
        environment=_environment(),
        locale=load_locale(),
        pack_plan_factory=lambda: plan,
    )

    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        app.screen.query_one("#entry-pack").focus()
        await pilot.press("enter")
        await pilot.pause()
        panel = app.screen.query_one("#pack-items")
        assert all(
            display_width(line) <= panel.size.width - 2
            for line in str(panel.renderable).splitlines()
        )


@pytest.mark.asyncio
async def test_real_pack_adapter_uses_plan_and_publishes_only_after_enter(tmp_path: Path) -> None:
    source = tmp_path / "中文项目"
    source.mkdir()
    (source / "README.md").write_text("# demo\n", encoding="utf-8")
    repository = SessionRepository(source / ".csbox" / "sessions")
    app = CSBoxApp(
        data_source=RealHomeDataSource(repository, source),
        environment=_environment(),
        locale=load_locale(),
    )

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        app.screen.query_one("#entry-pack").focus()
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, PackConfirmationScreen)
        assert "README.md" in _screen_text(app.screen)
        assert not tuple(source.glob("*.zip"))
        app.screen.query_one("#pack-confirm").focus()
        await pilot.press("enter")
        await pilot.pause()
        assert "打包完成" in _screen_text(app.screen)

    archives = tuple(source.glob("*.zip"))
    assert len(archives) == 1
