from pathlib import Path

from csbox.core.models import EnvironmentSnapshot
from csbox.lab.home_data import RealHomeDataSource


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


class HeavyReadSentinel:
    def list_sessions(self) -> object:
        raise AssertionError("Home must not enumerate sessions or open session.cast")


class ApiReadSentinel:
    def list(self) -> object:
        raise AssertionError("Home must not read API history")


class CheckReadSentinel:
    def run(self, root: Path) -> object:
        del root
        raise AssertionError("Home must not run project checks")


def test_real_home_is_a_lightweight_current_project_snapshot(tmp_path: Path) -> None:
    snapshot = RealHomeDataSource(
        HeavyReadSentinel(),  # type: ignore[arg-type]
        tmp_path,
        api_repository=ApiReadSentinel(),  # type: ignore[arg-type]
        check_service=CheckReadSentinel(),  # type: ignore[arg-type]
    ).get_home_snapshot(environment())

    assert snapshot.environment == environment()
    assert snapshot.project_dir == tmp_path
    assert snapshot.recent_experiments == []
    assert snapshot.recent_api_runs == []
    assert snapshot.check_status is None
