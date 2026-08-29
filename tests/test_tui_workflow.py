from dataclasses import fields
from datetime import UTC, datetime
from pathlib import Path

import pytest

from csbox.core.models import EnvironmentSnapshot, HomeSnapshot
from csbox.core.shell import BashProfile, PowerShell7Profile, PowerShell51Profile, ZshProfile
from csbox.lab.models import SessionMetadata, SessionPaths
from csbox.lab.repository import SessionRepositoryError
from csbox.locales import load_locale
from csbox.tui import lab_workflow
from csbox.tui.app import CSBoxApp
from csbox.tui.lab_workflow import (
    ActiveLabSession,
    ExperimentNameError,
    HomeNotice,
    LabStartRequest,
    ShellOption,
)


def test_lab_start_request_trims_and_preserves_a_chinese_experiment_name() -> None:
    request = LabStartRequest.from_input("  计算机网络实验  ", "powershell_7")

    assert request.experiment_name == "计算机网络实验"
    assert request.shell == "powershell_7"
    assert [field.name for field in fields(request)] == ["experiment_name", "shell"]


def test_lab_start_request_constructor_enforces_the_same_normalization() -> None:
    request = LabStartRequest("  数据结构实验  ", " bash ")

    assert request == LabStartRequest(experiment_name="数据结构实验", shell="bash")


@pytest.mark.parametrize(
    "name",
    ["", "   ", "实验\n二", "实验\u202e二", "\u200b", "\u0301", "实验" * 600],
)
def test_lab_start_request_rejects_missing_or_controlled_names(name: str) -> None:
    with pytest.raises(ExperimentNameError):
        LabStartRequest.from_input(name, "bash")


def test_shell_option_uses_the_resolver_profile_display_name() -> None:
    assert ShellOption.from_profile(PowerShell7Profile) == ShellOption(
        value="powershell_7",
        label="PowerShell 7",
    )
    assert ShellOption.from_profile(PowerShell51Profile) == ShellOption(
        value="powershell_51",
        label="Windows PowerShell 5.1",
    )
    assert ShellOption.from_profile(BashProfile).label == "Bash"
    assert ShellOption.from_profile(ZshProfile).label == "Zsh"


def test_no_available_shell_message_is_platform_specific() -> None:
    assert "PowerShell 7" in lab_workflow.no_available_shell_message("Windows")
    assert "Windows PowerShell" in lab_workflow.no_available_shell_message("Windows")
    assert "Bash 或 Zsh" in lab_workflow.no_available_shell_message("Linux")


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


def _metadata(status: str, tmp_path: Path) -> SessionMetadata:
    return SessionMetadata(
        id="target-session",
        name="中文实验",
        status=status,
        startedAt=datetime(2026, 8, 19, 8, 0, tzinfo=UTC),
        endedAt=(
            datetime(2026, 8, 19, 8, 5, tzinfo=UTC)
            if status in {"completed", "interrupted", "failed"}
            else None
        ),
        platform="windows",
        shell="powershell_7",
        shellVersion="7.6",
        initialRows=24,
        initialColumns=80,
        cwd=tmp_path,
        csboxVersion="0.3.0",
    )


class CountingHomeDataSource:
    source_id = "counting"

    def __init__(self, project_dir: Path) -> None:
        self.project_dir = project_dir
        self.calls = 0

    def get_home_snapshot(self, environment: EnvironmentSnapshot) -> HomeSnapshot:
        self.calls += 1
        return HomeSnapshot(environment=environment, project_dir=self.project_dir)


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal_status", ["completed", "interrupted", "failed"])
async def test_active_session_monitor_targets_one_session_and_stops_on_terminal_status(
    tmp_path: Path,
    terminal_status: str,
) -> None:
    paths = SessionPaths(tmp_path / "target-session")
    reads: list[SessionPaths] = []

    def load_metadata(target: SessionPaths) -> SessionMetadata:
        reads.append(target)
        return _metadata(terminal_status, tmp_path)

    data_source = CountingHomeDataSource(tmp_path)
    app = CSBoxApp(
        data_source=data_source,
        environment=_environment(),
        locale=load_locale(),
        active_session=ActiveLabSession(paths=paths, experiment_name="中文实验"),
        metadata_loader=load_metadata,
        monitor_interval=60.0,
        home_notice=HomeNotice("实验正在专用终端中运行。", "running"),
    )

    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        app._poll_active_session()
        await pilot.pause()

        assert reads == [paths]
        assert app.active_session is None
        assert data_source.calls == 2
        assert app.home_notice is not None
        assert app.home_notice.kind == terminal_status

        app._poll_active_session()
        await pilot.pause()
        assert reads == [paths]
        assert data_source.calls == 2


@pytest.mark.asyncio
async def test_temporary_metadata_failure_retries_without_fabricating_failed(
    tmp_path: Path,
) -> None:
    paths = SessionPaths(tmp_path / "target-session")
    outcomes: list[SessionMetadata | Exception] = [
        SessionRepositoryError("temporary read failure"),
        _metadata("running", tmp_path),
        _metadata("completed", tmp_path),
    ]
    reads: list[SessionPaths] = []

    def load_metadata(target: SessionPaths) -> SessionMetadata:
        reads.append(target)
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    data_source = CountingHomeDataSource(tmp_path)
    app = CSBoxApp(
        data_source=data_source,
        environment=_environment(),
        locale=load_locale(),
        active_session=ActiveLabSession(paths=paths, experiment_name="中文实验"),
        metadata_loader=load_metadata,
        monitor_interval=60.0,
    )

    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        app._poll_active_session()
        await pilot.pause()
        assert app.active_session is not None
        assert app.home_notice is not None
        assert app.home_notice.kind == "warning"
        assert data_source.calls == 1

        app._poll_active_session()
        await pilot.pause()
        assert app.active_session is not None
        assert app.home_notice is not None
        assert app.home_notice.kind != "failed"
        assert data_source.calls == 1

        app._poll_active_session()
        await pilot.pause()
        assert app.active_session is None
        assert app.home_notice is not None
        assert app.home_notice.kind == "completed"
        assert data_source.calls == 2
        assert reads == [paths, paths, paths]
