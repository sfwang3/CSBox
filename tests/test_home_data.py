from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from csbox.api.models import ApiRequest, ApiRun, ApiScenario, ApiStep
from csbox.api.repository import ApiRunRepository
from csbox.check.models import CheckReport
from csbox.core.events import TerminalEvent, TerminalEventType, TerminalSize
from csbox.core.models import EnvironmentSnapshot
from csbox.lab.captures import CaptureStore
from csbox.lab.home_data import RealHomeDataSource
from csbox.lab.repository import SessionRepository
from csbox.lab.screen import TerminalEmulator


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


def test_real_home_uses_recent_session_and_capture_count(tmp_path: Path) -> None:
    repository = SessionRepository(tmp_path / ".csbox/sessions")
    paths = repository.create_running(
        "中文实验",
        session_id="session-real",
        platform="linux",
        shell="bash",
        shell_version="5.2",
        size=TerminalSize(80, 24),
        cwd=tmp_path / "课程实验",
        started_at=datetime.now(UTC) - timedelta(minutes=2),
    )
    paths.cast.write_text(
        '{"version":3,"term":{"cols":80,"rows":24,"type":"xterm-256color"}}\n',
        encoding="utf-8",
    )
    emulator = TerminalEmulator(columns=8, rows=2)
    emulator.apply(
        TerminalEvent(
            sequence=1,
            monotonic_time=1.0,
            relative_time=1.0,
            type=TerminalEventType.OUTPUT,
            payload="中文".encode(),
        )
    )
    CaptureStore(paths.captures).create_capture(
        emulator.snapshot(), cwd=tmp_path, title="检查结果", timestamp=1.0
    )
    repository.finish(paths, "completed", exit_code=0)

    snapshot = RealHomeDataSource(repository, tmp_path).get_home_snapshot(environment())

    assert snapshot.project_dir == tmp_path
    assert len(snapshot.recent_experiments) == 1
    experiment = snapshot.recent_experiments[0]
    assert experiment.name == "中文实验"
    assert experiment.demo is False
    assert experiment.capture_count == 1
    assert experiment.status == "completed"
    assert "min" in experiment.duration


def test_real_home_has_localized_empty_state_without_demo_data(tmp_path: Path) -> None:
    snapshot = RealHomeDataSource(
        SessionRepository(tmp_path / ".csbox/sessions"), tmp_path
    ).get_home_snapshot(environment())

    assert snapshot.recent_experiments == []
    assert snapshot.project_dir == tmp_path


def test_real_home_includes_real_api_run_and_check_status(tmp_path: Path) -> None:
    api_repository = ApiRunRepository.from_cwd(tmp_path)
    api_repository.save(
        ApiRun(
            id="20260811T080000-api123456789",
            scenario=ApiScenario(
                name="中文接口场景",
                source="scenarios/中文.toml",
                steps=(
                    ApiStep(
                        name="查询",
                        request=ApiRequest(method="GET", url="https://example.test/profile"),
                    ),
                ),
            ),
            started_at=datetime.now(UTC),
        )
    )

    class CheckStub:
        def run(self, root: Path) -> CheckReport:
            return CheckReport(root=root)

    snapshot = RealHomeDataSource(
        SessionRepository(tmp_path / ".csbox/sessions"),
        tmp_path,
        api_repository=api_repository,
        check_service=CheckStub(),  # type: ignore[arg-type]
    ).get_home_snapshot(environment())

    assert snapshot.recent_api_runs[0].scenario_name == "中文接口场景"
    assert snapshot.recent_api_runs[0].status == "PASS"
    assert snapshot.check_status == "PASS"
