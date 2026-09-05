from __future__ import annotations

from pathlib import Path
from time import monotonic

import pytest
from textual.app import App
from textual.widgets import Button

import csbox.tui.app as tui_app_module
import csbox.tui.screens.home as home_screen_module
from csbox.check.models import CheckFinding, CheckReport, CheckStatus
from csbox.check.service import CheckService, create_check_service
from csbox.config import ConfigurationError
from csbox.core.models import EnvironmentSnapshot, HomeSnapshot
from csbox.lab.home_data import RealHomeDataSource
from csbox.lab.repository import SessionRepository
from csbox.locales import load_locale
from csbox.pack.service import PackService, create_pack_service
from csbox.tui.dialogs.unavailable import UnavailableDialog
from csbox.tui.screens.home import HomeScreen
from csbox.tui.screens.project_check import ProjectCheckScreen


async def _wait_until(pilot: object, predicate, *, timeout: float = 5.0) -> None:
    deadline = monotonic() + timeout
    while monotonic() < deadline:
        if predicate():
            return
        await pilot.pause()  # type: ignore[attr-defined]
    assert predicate()


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


class _HomeHost(App[None]):
    def __init__(self, project_dir: Path) -> None:
        super().__init__()
        self.project_dir = project_dir

    def on_mount(self) -> None:
        self.push_screen(
            HomeScreen(
                snapshot=HomeSnapshot(
                    environment=_environment(),
                    project_dir=self.project_dir,
                ),
                locale=load_locale(),
            )
        )


def _large_project(project_dir: Path) -> None:
    (project_dir / ".csbox").mkdir()
    (project_dir / ".csbox" / "config.toml").write_text(
        "[check]\nlarge_file_threshold_mb = 1\n",
        encoding="utf-8",
    )
    (project_dir / "README.md").write_text("# 课程项目\n", encoding="utf-8")
    (project_dir / "large.bin").write_bytes(b"x" * (1024 * 1024 + 1))


@pytest.mark.asyncio
async def test_home_uses_project_configured_check_service_owned_by_pack(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _large_project(tmp_path)
    expected = CheckReport(
        root=tmp_path,
        findings=(
            CheckFinding(
                rule_id="shared-check",
                status=CheckStatus.WARN,
                message="来自共享 CheckService",
                category="shared",
            ),
        ),
    )

    class SharedCheckService:
        def __init__(self) -> None:
            self.calls: list[Path] = []

        def run(self, root: Path, *, build: bool = False, inventory=None) -> CheckReport:
            del build, inventory
            self.calls.append(root)
            return expected

    shared = SharedCheckService()
    monkeypatch.setattr(
        tui_app_module,
        "create_pack_service",
        lambda root: PackService(check_service=shared),
    )
    monkeypatch.setattr(
        home_screen_module,
        "create_check_service",
        lambda root: pytest.fail(f"Home must not create a second service for {root}"),
        raising=False,
    )

    app = tui_app_module.CSBoxApp(
        data_source=RealHomeDataSource(SessionRepository.from_cwd(tmp_path), tmp_path),
        environment=_environment(),
        locale=load_locale(),
    )

    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        app.screen.query_one("#entry-check", Button).focus()
        await pilot.press("enter")
        await _wait_until(pilot, lambda: isinstance(app.screen, ProjectCheckScreen))

        assert isinstance(app.screen, ProjectCheckScreen)
        assert app.screen.report is expected

    assert shared.calls == [tmp_path.resolve()]


@pytest.mark.asyncio
async def test_home_check_applies_the_project_threshold_from_the_shared_service(
    tmp_path: Path,
) -> None:
    _large_project(tmp_path)
    app = tui_app_module.CSBoxApp(
        data_source=RealHomeDataSource(SessionRepository.from_cwd(tmp_path), tmp_path),
        environment=_environment(),
        locale=load_locale(),
    )

    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        app.screen.query_one("#entry-check", Button).focus()
        await pilot.press("enter")
        await _wait_until(pilot, lambda: isinstance(app.screen, ProjectCheckScreen))

        assert isinstance(app.screen, ProjectCheckScreen)
        assert any(
            finding.category == "large-file" and finding.path == Path("large.bin")
            for finding in app.screen.report.findings
        )


@pytest.mark.asyncio
async def test_home_placeholder_project_check_and_pack_remain_delivery_safe(
    tmp_path: Path,
) -> None:
    (tmp_path / "README.md").write_text("# 课程项目\n", encoding="utf-8")
    (tmp_path / "main.py").write_text("print('课程项目')\n", encoding="utf-8")
    (tmp_path / ".env.example").write_text('TOKEN="replace-me-token"\n', encoding="utf-8")
    destination = tmp_path / "交付.zip"
    app = tui_app_module.CSBoxApp(
        data_source=RealHomeDataSource(SessionRepository.from_cwd(tmp_path), tmp_path),
        environment=_environment(),
        locale=load_locale(),
    )

    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        app.screen.query_one("#entry-check", Button).focus()
        await pilot.press("enter")
        await _wait_until(pilot, lambda: isinstance(app.screen, ProjectCheckScreen))

        assert isinstance(app.screen, ProjectCheckScreen)
        assert not any(
            finding.category == "hard-coded-secret" and finding.status is CheckStatus.FAIL
            for finding in app.screen.report.findings
        )

    plan = create_pack_service(tmp_path).plan(tmp_path, destination=destination)
    assert ".env.example" in plan.included
    assert ".env.example:hard-coded-secret" not in plan.rejected


def test_cli_and_pack_factories_read_the_same_project_check_threshold(tmp_path: Path) -> None:
    _large_project(tmp_path)

    cli_service = create_check_service(tmp_path)
    pack_service = create_pack_service(tmp_path)

    cli_report = cli_service.run(tmp_path)
    pack_report = pack_service.check_service.run(tmp_path)  # type: ignore[union-attr]

    assert cli_service.config.check.large_file_threshold_mb == 1
    assert pack_service.check_service.config.check.large_file_threshold_mb == 1  # type: ignore[union-attr]
    assert [
        (item.category, item.path)
        for item in cli_report.findings
        if item.status is CheckStatus.WARN
    ] == [("large-file", Path("large.bin"))]
    assert [
        (item.category, item.path)
        for item in pack_report.findings
        if item.status is CheckStatus.WARN
    ] == [("large-file", Path("large.bin"))]


@pytest.mark.asyncio
async def test_standalone_home_uses_shared_check_factory_when_config_is_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[Path] = []

    def factory(root: Path) -> CheckService:
        calls.append(root)
        return create_check_service(root)

    monkeypatch.setattr(home_screen_module, "create_check_service", factory, raising=False)
    app = _HomeHost(tmp_path)

    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        app.screen.query_one("#entry-check", Button).focus()
        await pilot.press("enter")
        await pilot.pause()

        assert isinstance(app.screen, ProjectCheckScreen)

    assert calls == [tmp_path]


@pytest.mark.asyncio
async def test_home_invalid_config_is_a_recoverable_check_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / ".csbox" / "config.toml"
    error = ConfigurationError(config_path, "配置无效")
    monkeypatch.setattr(
        home_screen_module,
        "create_check_service",
        lambda root: (_ for _ in ()).throw(error),
        raising=False,
    )
    app = _HomeHost(tmp_path)

    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        app.screen.query_one("#entry-check", Button).focus()
        await pilot.press("enter")
        await pilot.pause()

        assert isinstance(app.screen, UnavailableDialog)
        assert "检查项目暂时无法打开" in str(app.screen.query_one("#dialog-message").renderable)
