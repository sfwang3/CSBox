from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from csbox.core.events import TerminalSize
from csbox.core.shell import ShellUnavailableError
from csbox.lab.models import SessionPaths
from csbox.lab.windows_host import (
    LabLaunchError,
    LaunchClaimError,
    LaunchIntentStore,
    WindowsTerminalLabLauncher,
    run_dedicated_lab_host,
)


class FakeProcess:
    def __init__(self, returncode: int | None = None) -> None:
        self.returncode = returncode
        self.poll_calls = 0

    def poll(self) -> int | None:
        self.poll_calls += 1
        return self.returncode


class FakeClock:
    def __init__(self) -> None:
        self.value = 0.0

    def monotonic(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.value += seconds


def reserve_intent(tmp_path: Path):
    return LaunchIntentStore(tmp_path / "launches").reserve(
        name="中文实验",
        shell="powershell_7",
        command=("pwsh.exe", "-NoLogo"),
        cwd=tmp_path / "课程目录",
        size=TerminalSize(100, 30),
    )


def test_launch_intent_is_private_and_duplicate_host_claim_is_rejected(tmp_path: Path) -> None:
    store = LaunchIntentStore(tmp_path / "launches")
    intent = reserve_intent(tmp_path)

    assert intent.request_path.is_file()
    assert intent.request_path.stat().st_mode & 0o077 == 0
    payload = json.loads(intent.request_path.read_text(encoding="utf-8"))
    assert payload["name"] == "中文实验"
    assert payload["size"] == {"columns": 100, "rows": 30}

    store.claim(intent)
    with pytest.raises(LaunchClaimError):
        store.claim(LaunchIntentStore.load(intent.request_path))


def test_launcher_reports_success_only_after_host_ready_and_uses_dedicated_window(
    tmp_path: Path,
) -> None:
    store = LaunchIntentStore(tmp_path / "launches")
    launched: list[tuple[tuple[str, ...], dict[str, object]]] = []
    process = FakeProcess()

    def spawn(argv: tuple[str, ...], **kwargs: object) -> FakeProcess:
        launched.append((argv, kwargs))
        intent_path = Path(argv[argv.index("--intent") + 1])
        intent = LaunchIntentStore.load(intent_path)
        store.claim(intent)
        store.mark_ready(intent, session_id="session-42")
        return process

    result = WindowsTerminalLabLauncher(
        launch_root=tmp_path / "launches",
        wt_executable="wt.exe",
        process_factory=spawn,
        timeout_seconds=1.0,
    ).start(
        name="中文实验",
        shell="powershell_7",
        command=("pwsh.exe", "-NoLogo"),
        cwd=tmp_path,
        size=TerminalSize(100, 30),
    )

    assert result.session_id == "session-42"
    assert result.status == "running"
    argv = launched[0][0]
    assert argv[:4] == ("wt.exe", "--window", "new", "new-tab")
    assert "--startingDirectory" in argv
    assert str(tmp_path) in argv
    assert "-m" in argv and "csbox" in argv
    assert "_host" in argv
    assert not any("1049" in item for item in argv)


def test_launcher_keeps_timeout_as_a_distinct_bounded_failure(tmp_path: Path) -> None:
    clock = FakeClock()
    process = FakeProcess()

    def spawn(argv: tuple[str, ...], **kwargs: object) -> FakeProcess:
        del argv, kwargs
        return process

    launcher = WindowsTerminalLabLauncher(
        launch_root=tmp_path / "launches",
        wt_executable="wt.exe",
        process_factory=spawn,
        timeout_seconds=0.2,
        poll_interval_seconds=0.1,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )

    with pytest.raises(LabLaunchError, match="READY") as raised:
        launcher.start(
            name="超时实验",
            shell="powershell_7",
            command=("pwsh.exe",),
            cwd=tmp_path,
            size=TerminalSize(80, 24),
        )

    assert raised.value.kind == "launcher_timeout"
    assert process.poll_calls > 0
    status = launcher.last_intent_status
    assert status is not None
    assert status.state == "cancelled"


def test_missing_wt_and_spawn_failure_are_not_host_ready_failures(tmp_path: Path) -> None:
    missing = WindowsTerminalLabLauncher(
        launch_root=tmp_path / "missing",
        wt_locator=lambda: None,
    )
    with pytest.raises(LabLaunchError) as missing_error:
        missing.start(
            name="实验",
            shell="powershell_7",
            command=("pwsh.exe",),
            cwd=tmp_path,
            size=TerminalSize(80, 24),
        )
    assert missing_error.value.kind == "wt_launch_failure"

    def broken_spawn(argv: tuple[str, ...], **kwargs: object) -> FakeProcess:
        del argv, kwargs
        raise OSError("WT client unavailable")

    broken = WindowsTerminalLabLauncher(
        launch_root=tmp_path / "broken",
        wt_executable="wt.exe",
        process_factory=broken_spawn,
    )
    with pytest.raises(LabLaunchError) as spawn_error:
        broken.start(
            name="实验",
            shell="powershell_7",
            command=("pwsh.exe",),
            cwd=tmp_path,
            size=TerminalSize(80, 24),
        )
    assert spawn_error.value.kind == "wt_launch_failure"


def test_host_classifies_shell_startup_failure_and_preserves_cause(tmp_path: Path) -> None:
    intent = reserve_intent(tmp_path)

    def service_factory(*args: object, **kwargs: object) -> object:
        del args, kwargs

        class FailingService:
            def start(self, *args: object, **kwargs: object) -> object:
                del args, kwargs
                cause = FileNotFoundError("pwsh.exe")
                raise ShellUnavailableError("未找到 pwsh.exe。", cause) from cause

        return FailingService()

    return_code = run_dedicated_lab_host(
        intent.request_path,
        intent.token,
        service_factory=service_factory,
    )

    assert return_code == 1
    status = LaunchIntentStore.load_status(intent)
    assert status.state == "failed"
    assert status.error_kind == "shell_startup_failure"
    assert "FileNotFoundError" in status.exception_chain


def test_host_finalizes_ready_session_with_actual_nonzero_child_status(tmp_path: Path) -> None:
    intent = reserve_intent(tmp_path)
    session = SessionPaths(tmp_path / "session-17")

    def service_factory(*args: object, **kwargs: object) -> object:
        del args, kwargs

        class CompletedService:
            def start(self, *args: object, **kwargs: object) -> object:
                ready_callback = kwargs["ready_callback"]
                ready_callback(session)
                return SimpleNamespace(session=session, status="completed", exit_code=17)

        return CompletedService()

    assert (
        run_dedicated_lab_host(
            intent.request_path,
            intent.token,
            service_factory=service_factory,
        )
        == 0
    )
    status = LaunchIntentStore.load_status(intent)
    assert status.state == "finished"
    assert status.session_id == "session-17"
    assert status.session_status == "completed"
    assert status.exit_code == 17


def test_late_host_cannot_claim_a_launcher_timeout_intent(tmp_path: Path) -> None:
    store = LaunchIntentStore(tmp_path / "launches")
    intent = reserve_intent(tmp_path)
    assert store.cancel(intent).state == "cancelled"

    assert (
        run_dedicated_lab_host(
            intent.request_path,
            intent.token,
            service_factory=lambda **_: None,
        )
        == 1
    )
    assert LaunchIntentStore.load_status(intent).state == "cancelled"


def test_host_does_not_publish_finished_without_a_ready_callback(tmp_path: Path) -> None:
    intent = reserve_intent(tmp_path)

    def service_factory(*args: object, **kwargs: object) -> object:
        del args, kwargs

        class ServiceWithoutReady:
            def start(self, *args: object, **kwargs: object) -> object:
                del args, kwargs
                return SimpleNamespace(
                    session=SessionPaths(tmp_path / "session-without-ready"),
                    status="completed",
                    exit_code=0,
                )

        return ServiceWithoutReady()

    assert (
        run_dedicated_lab_host(intent.request_path, intent.token, service_factory=service_factory)
        == 1
    )
    status = LaunchIntentStore.load_status(intent)
    assert status.state == "failed"
    assert status.error_kind == "host_startup_failure"


def test_backend_failure_after_ready_is_recorded_as_runtime_failure(tmp_path: Path) -> None:
    intent = reserve_intent(tmp_path)
    session = SessionPaths(tmp_path / "session-runtime")

    def service_factory(*args: object, **kwargs: object) -> object:
        del args, kwargs

        class BrokenService:
            def start(self, *args: object, **kwargs: object) -> object:
                ready_callback = kwargs["ready_callback"]
                ready_callback(session)
                raise RuntimeError("backend closed unexpectedly")

        return BrokenService()

    assert (
        run_dedicated_lab_host(
            intent.request_path,
            intent.token,
            service_factory=service_factory,
        )
        == 1
    )
    status = LaunchIntentStore.load_status(intent)
    assert status.state == "finished"
    assert status.session_id == "session-runtime"
    assert status.session_status == "failed"
    assert status.error_kind == "runtime_backend_failure"
