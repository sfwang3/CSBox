from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from textual.widgets import Static

import csbox.lab.service as service_module
from csbox.config.models import CSBoxConfig
from csbox.core.events import TerminalEvent, TerminalEventType, TerminalSize
from csbox.core.shell import ShellKind, ShellProfile
from csbox.core.terminal import TerminalProcessExited
from csbox.lab.captures import CaptureStore
from csbox.lab.dispatcher import DispatchError, TerminalEventDispatcher
from csbox.lab.models import SessionMetadata, SessionPaths
from csbox.lab.proxy import TerminalProxy
from csbox.lab.recorder import AsciicastV3Reader, AsciicastV3Recorder, RecorderError
from csbox.lab.repository import SessionRepository, SessionRepositoryError
from csbox.lab.screen import TerminalEmulator
from csbox.lab.service import LabService
from csbox.lab.status import LabSurfacePresenter
from csbox.locales import load_locale
from csbox.tui.app import ReviewApp
from csbox.tui.screens.review import ReviewController

_TEST_SHELL = ShellProfile(ShellKind.BASH, "Test Shell", "test-shell")


@pytest.fixture(autouse=True)
def _use_fake_shell_boundary(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        service_module,
        "select_shell",
        lambda *args, **kwargs: _TEST_SHELL,
    )
    monkeypatch.setattr(service_module, "detect_shell_version", lambda profile: None)


class ScriptedBackend:
    def __init__(self, reads: list[bytes | None], *, exit_code: int | None = 0) -> None:
        self.reads = list(reads)
        self.exit_code = exit_code
        self.spawned = False
        self.closed = False
        self.on_spawn = None
        self.on_read = None
        self.writes: list[bytes] = []

    def spawn(self, command, *, cwd=None, env=None, size=None) -> None:
        del command, cwd, env, size
        self.spawned = True
        if self.on_spawn is not None:
            self.on_spawn()

    def read(self, max_bytes: int = 65536, timeout: float = 0.05) -> bytes | None:
        del max_bytes, timeout
        if self.on_read is not None:
            self.on_read()
            self.on_read = None
        if self.reads:
            return self.reads.pop(0)
        return b""

    def write(self, data: bytes) -> int:
        self.writes.append(data)
        return len(data)

    def resize(self, columns: int, rows: int) -> None:
        del columns, rows

    def is_alive(self) -> bool:
        return not self.closed and bool(self.reads)

    def wait(self, timeout: float = 0.0) -> int | None:
        del timeout
        return self.exit_code

    def close(self) -> None:
        self.closed = True


class MemoryInput:
    def __init__(self, chunks: list[bytes | None]) -> None:
        self.chunks = list(chunks)

    def read(self, max_bytes: int = 4096, timeout: float = 0.05) -> bytes | None:
        del max_bytes, timeout
        if self.chunks:
            return self.chunks.pop(0)
        return b""


class MemoryOutput:
    def __init__(self) -> None:
        self.data = bytearray()

    def write(self, data: bytes) -> int:
        self.data.extend(data)
        return len(data)


class TerminalState:
    def __enter__(self) -> TerminalState:
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        del exc_type, exc, traceback


def make_service(
    tmp_path: Path,
    backend: ScriptedBackend,
    input_chunks: list[bytes | None],
) -> tuple[LabService, SessionRepository]:
    repository = SessionRepository(tmp_path / "sessions")
    service = LabService(
        repository=repository,
        config=CSBoxConfig(),
        cwd=tmp_path,
        backend_factory=lambda: backend,
        input_adapter_factory=lambda: MemoryInput(input_chunks),
        output_adapter_factory=MemoryOutput,
        terminal_state_factory=TerminalState,
    )
    return service, repository


@pytest.mark.parametrize("exit_status", [0, 7])
def test_service_persists_completed_status_when_pending_input_races_child_exit(
    tmp_path: Path,
    exit_status: int,
) -> None:
    class ExitRaceBackend(ScriptedBackend):
        def write(self, data: bytes) -> int:
            self.writes.append(data)
            if len(self.writes) == 1:
                return len(data)
            raise TerminalProcessExited(exit_status)

    backend = ExitRaceBackend([None, b"tail-output", b""], exit_code=exit_status)
    service, repository = make_service(tmp_path, backend, [b"exit\r", b"late-input"])

    result = service.start("exit-race", shell="bash")
    summary = repository.list_sessions()[0]

    assert result.status == "completed"
    assert result.exit_code == exit_status
    assert summary.metadata.status == "completed"
    assert summary.metadata.status_reason is None
    assert summary.metadata.exit_code == exit_status
    assert b"tail-output" in result.session.cast.read_bytes()


def write_cast_header(paths: SessionPaths, *, columns: int = 80, rows: int = 24) -> None:
    paths.cast.write_text(
        json.dumps({"version": 3, "term": {"cols": columns, "rows": rows}}) + "\n",
        encoding="utf-8",
    )


def write_metadata(paths: SessionPaths, *, status: str) -> None:
    metadata = SessionMetadata(
        id=paths.root.name,
        name="测试会话",
        status=status,
        startedAt="2026-08-14T00:00:00Z",
        endedAt=None,
        platform="linux",
        shell="bash",
        shellVersion=None,
        initialRows=24,
        initialColumns=80,
        cwd=paths.root,
        csboxVersion="0.3.0",
    )
    paths.metadata.write_text(
        json.dumps(metadata.model_dump(mode="json", by_alias=True), ensure_ascii=False),
        encoding="utf-8",
    )


def test_session_metadata_represents_starting_and_owner_reason() -> None:
    metadata = SessionMetadata(
        id="session-001",
        name="网络实验",
        status="starting",
        statusReason="等待 PTY",
        ownerPid=1234,
        startedAt="2026-08-14T00:00:00Z",
        endedAt=None,
        platform="linux",
        shell="bash",
        shellVersion=None,
        initialRows=24,
        initialColumns=80,
        cwd=Path("/tmp/中文项目"),
        csboxVersion="0.3.0",
    )

    assert metadata.status == "starting"
    assert metadata.status_reason == "等待 PTY"
    assert metadata.owner_pid == 1234


def test_service_persists_running_before_spawn_and_signals_ready_after_spawn(
    tmp_path: Path,
) -> None:
    backend = ScriptedBackend([b"NEW_MARKER", b""], exit_code=7)
    service, repository = make_service(tmp_path, backend, [None, b""])
    observed: list[tuple[str, str]] = []

    def observe_spawn() -> None:
        observed.append(("spawn", repository.list_sessions()[0].metadata.status))

    def observe_read() -> None:
        observed.append(("read", repository.list_sessions()[0].metadata.status))

    original_mark_running = repository.mark_running

    def observe_ready(paths: SessionPaths) -> SessionMetadata:
        assert backend.spawned is True
        observed.append(("ready", repository.list_sessions()[0].metadata.status))
        return original_mark_running(paths)

    backend.on_spawn = observe_spawn
    backend.on_read = observe_read
    repository.mark_running = observe_ready  # type: ignore[method-assign]

    result = service.start(
        "边界实验",
        shell="bash",
        command=("bash",),
        size=TerminalSize(80, 24),
    )

    metadata = repository.list_sessions()[0].metadata
    assert observed[:3] == [
        ("spawn", "running"),
        ("ready", "running"),
        ("read", "running"),
    ]
    assert result.status == "completed"
    assert result.exit_code == 7
    assert metadata.status == "completed"
    assert metadata.exit_code == 7
    assert "OLD_MARKER" not in result.session.cast.read_text(encoding="utf-8")
    assert "NEW_MARKER" in result.session.cast.read_text(encoding="utf-8")


def test_recording_boundary_and_multiple_captures_preserve_ordered_session_data(
    tmp_path: Path,
) -> None:
    backend = ScriptedBackend(["\x1b[31mNEW_MARKER 中文\x1b[0m".encode(), b""], exit_code=0)
    output = MemoryOutput()
    output.data.extend(b"OLD_MARKER")
    repository = SessionRepository(tmp_path / "sessions")
    service = LabService(
        repository=repository,
        config=CSBoxConfig(),
        cwd=tmp_path,
        backend_factory=lambda: backend,
        input_adapter_factory=lambda: MemoryInput([b"\x1b[24~", b"\x1b[24~", b""]),
        output_adapter_factory=lambda: output,
        terminal_state_factory=TerminalState,
    )

    result = service.start(
        "多 Capture 边界实验",
        shell="bash",
        command=("bash",),
        size=TerminalSize(73, 11),
    )

    cast_text = result.session.cast.read_text(encoding="utf-8")
    cast_events = AsciicastV3Reader(result.session.cast).read().events
    captures = CaptureStore(result.session.captures).load().captures
    header = json.loads(cast_text.splitlines()[0])

    assert "OLD_MARKER" not in cast_text
    assert "NEW_MARKER" in cast_text
    assert "Capture 已保存" not in cast_text
    assert b"#2" in output.data
    assert "Capture 已保存".encode() in output.data
    assert header["term"]["cols"] == 73
    assert header["term"]["rows"] == 11
    assert [event.code for event in cast_events] == ["m", "o", "m", "x"]
    assert len(captures) == 2
    assert len(result.status_events) == 2
    assert all(status.kind == "capture_succeeded" for status in result.status_events)
    assert captures[1].snapshot.cells[0][0].character == "N"


def test_lab_service_preserves_export_public_api(tmp_path: Path) -> None:
    service, _ = make_service(tmp_path, ScriptedBackend([]), [])

    assert callable(service.export)


def test_repository_recovers_ownerless_running_session_with_reason(tmp_path: Path) -> None:
    repository = SessionRepository(tmp_path / "sessions")
    paths = SessionPaths(repository.root / "stale-session")
    paths.root.mkdir(parents=True)
    write_cast_header(paths)
    write_metadata(paths, status="running")

    restarted = SessionRepository(repository.root)
    sessions = restarted.list_sessions()

    assert len(sessions) == 1
    assert sessions[0].metadata.status == "interrupted"
    assert sessions[0].metadata.status_reason == "session owner is no longer active"
    assert sessions[0].metadata.ended_at is not None


def test_repository_does_not_recover_a_session_with_an_active_owner(tmp_path: Path) -> None:
    owner = SessionRepository(tmp_path / "sessions")
    paths = owner.create_starting(
        "活动实验",
        shell="bash",
        shell_version=None,
        size=TerminalSize(80, 24),
        cwd=tmp_path,
        session_id="active-session",
    )
    write_cast_header(paths)

    restarted = SessionRepository(owner.root)

    assert restarted.list_sessions()[0].metadata.status == "running"


def test_windows_owner_probe_maps_lock_contention_to_blocking_io(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import csbox.lab.repository as repository_module

    class LockedStream:
        closed = False

        def seek(self, offset: int) -> None:
            del offset

        def read(self, size: int) -> bytes:
            del size
            raise PermissionError(13, "locked by another Windows handle")

        def close(self) -> None:
            self.closed = True

    stream = LockedStream()
    monkeypatch.setattr(repository_module, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(Path, "open", lambda path, mode: stream)

    with pytest.raises(BlockingIOError):
        repository_module._SessionOwner(Path("owner.lock"))

    assert stream.closed is True


def test_stale_recovery_holds_owner_until_interrupted_metadata_is_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import csbox.lab.repository as repository_module

    paths = SessionPaths(tmp_path / "sessions" / "stale-session")
    paths.root.mkdir(parents=True)
    write_cast_header(paths)
    write_metadata(paths, status="running")
    restarted = SessionRepository(paths.root.parent)
    real_write = repository_module._atomic_write_json
    observed_lock = []

    def observe_write(path: Path, value: object) -> None:
        contender = SessionRepository(paths.root.parent)
        owner = contender._try_acquire_owner(paths)
        observed_lock.append(owner is None)
        if owner is not None:
            owner.release()
        real_write(path, value)

    monkeypatch.setattr(repository_module, "_atomic_write_json", observe_write)

    restarted.recover_stale_running()

    assert observed_lock == [True]


def test_starting_metadata_is_written_while_owner_is_held(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import csbox.lab.repository as repository_module

    repository = SessionRepository(tmp_path / "sessions")
    paths = SessionPaths(repository.root / "new-session")
    real_write = repository_module._atomic_write_json
    observed_lock = []

    def observe_write(path: Path, value: object) -> None:
        contender = SessionRepository(repository.root)
        owner = contender._try_acquire_owner(paths)
        observed_lock.append(owner is None)
        if owner is not None:
            owner.release()
        real_write(path, value)

    monkeypatch.setattr(repository_module, "_atomic_write_json", observe_write)

    repository.create_starting(
        "创建实验",
        shell="bash",
        shell_version=None,
        size=TerminalSize(80, 24),
        cwd=tmp_path,
        session_id="new-session",
    )

    assert observed_lock == [True]


def test_only_the_active_session_owner_can_transition_lifecycle(
    tmp_path: Path,
) -> None:
    owner = SessionRepository(tmp_path / "sessions")
    mark_paths = owner.create_starting(
        "owner mark",
        shell="bash",
        shell_version=None,
        size=TerminalSize(80, 24),
        cwd=tmp_path,
        session_id="mark-owner",
    )
    finish_paths = owner.create_starting(
        "owner finish",
        shell="bash",
        shell_version=None,
        size=TerminalSize(80, 24),
        cwd=tmp_path,
        session_id="finish-owner",
    )
    contender = SessionRepository(owner.root)

    with pytest.raises(SessionRepositoryError, match="owner"):
        contender.mark_running(mark_paths)
    with pytest.raises(SessionRepositoryError, match="owner"):
        contender.finish(finish_paths, "failed", reason="foreign writer")

    assert {item.metadata.status for item in owner.list_sessions()} == {"running"}


def test_stale_recovery_rechecks_metadata_after_acquiring_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import csbox.lab.repository as repository_module

    paths = SessionPaths(tmp_path / "sessions" / "racing-session")
    paths.root.mkdir(parents=True)
    write_cast_header(paths)
    write_metadata(paths, status="running")
    repository = SessionRepository(paths.root.parent)
    real_acquire = repository._try_acquire_owner
    real_write = repository_module._atomic_write_json

    def acquire_then_finish(candidate: SessionPaths):
        owner = real_acquire(candidate)
        current = repository._read_metadata(candidate)
        completed = current.model_copy(
            update={
                "status": "completed",
                "status_reason": "child exited",
                "exit_code": 0,
                "ended_at": datetime.now(UTC),
            }
        )
        real_write(candidate.metadata, completed.model_dump(mode="json", by_alias=True))
        return owner

    monkeypatch.setattr(repository, "_try_acquire_owner", acquire_then_finish)

    assert repository.recover_stale_running() == ()
    assert repository.list_sessions()[0].metadata.status == "completed"
    assert repository.list_sessions()[0].metadata.status_reason == "child exited"


def test_critical_finish_persistence_failure_retries_as_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = ScriptedBackend([b""], exit_code=0)
    service, repository = make_service(tmp_path, backend, [None])
    real_finish = repository.finish
    calls = 0

    def fail_once(paths, status, *, exit_code=None, reason=None, ended_at=None):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("metadata persistence failed")
        return real_finish(
            paths,
            status,
            exit_code=exit_code,
            reason=reason,
            ended_at=ended_at,
        )

    monkeypatch.setattr(repository, "finish", fail_once)

    with pytest.raises(OSError, match="metadata persistence failed"):
        service.start("持久化失败实验", shell="bash", command=("bash",))

    metadata = repository.list_sessions()[0].metadata
    assert calls == 2
    assert metadata.status == "failed"
    assert metadata.status_reason == "metadata persistence failed"


def test_unrecoverable_finish_failure_releases_owner_for_stale_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = ScriptedBackend([b""], exit_code=0)
    service, repository = make_service(tmp_path, backend, [None])

    def always_fail(*args, **kwargs):
        raise OSError("metadata device unavailable")

    monkeypatch.setattr(repository, "finish", always_fail)

    with pytest.raises(OSError, match="metadata device unavailable"):
        service.start("不可恢复持久化实验", shell="bash", command=("bash",))

    restarted = SessionRepository(repository.root)
    assert restarted.list_sessions()[0].metadata.status == "interrupted"


def test_service_spawn_failure_is_failed_without_an_exit_event(tmp_path: Path) -> None:
    class SpawnFailureBackend(ScriptedBackend):
        def spawn(self, command, *, cwd=None, env=None, size=None) -> None:
            del command, cwd, env, size
            raise OSError("pty spawn failed")

    service, repository = make_service(tmp_path, SpawnFailureBackend([]), [None])

    with pytest.raises(OSError, match="pty spawn failed"):
        service.start("spawn 失败", shell="bash", command=("bash",))

    summary = repository.list_sessions()[0]
    assert summary.metadata.status == "failed"
    assert summary.metadata.status_reason == "OSError: session infrastructure failed"
    assert summary.paths.cast.read_text(encoding="utf-8").splitlines()[1:] == []


def test_service_read_failure_is_failed_with_reliable_prefix(tmp_path: Path) -> None:
    class ReadFailureBackend(ScriptedBackend):
        def read(self, max_bytes: int = 65536, timeout: float = 0.05) -> bytes | None:
            del max_bytes, timeout
            if self.reads:
                return self.reads.pop(0)
            raise OSError("pty read failed")

    service, repository = make_service(tmp_path, ReadFailureBackend([b"prefix"]), [None])

    with pytest.raises(OSError, match="pty read failed"):
        service.start("read 失败", shell="bash", command=("bash",))

    summary = repository.list_sessions()[0]
    events = AsciicastV3Reader(summary.paths.cast).read().events
    assert summary.metadata.status == "failed"
    assert summary.metadata.exit_code is None
    assert [event.code for event in events] == ["o"]


def test_service_recorder_spawn_failure_is_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import csbox.lab.service as service_module

    class FailingRecorder:
        def __init__(self, *args, **kwargs) -> None:
            del args, kwargs
            raise RecorderError("recorder start failed")

    monkeypatch.setattr(service_module, "AsciicastV3Recorder", FailingRecorder)
    service, repository = make_service(tmp_path, ScriptedBackend([]), [None])

    with pytest.raises(RecorderError, match="recorder start failed"):
        service.start("recorder 失败", shell="bash", command=("bash",))

    summary = repository.list_sessions()[0]
    assert summary.metadata.status == "failed"
    assert summary.metadata.status_reason == "RecorderError: session infrastructure failed"


def test_spawn_failure_does_not_append_a_fake_exit_event(tmp_path: Path) -> None:
    cast_path = tmp_path / "session.cast"
    recorder = AsciicastV3Recorder(cast_path, columns=80, rows=24)

    class SpawnFailureBackend(ScriptedBackend):
        def spawn(self, command, *, cwd=None, env=None, size=None) -> None:
            del command, cwd, env, size
            raise RuntimeError("spawn failed")

    proxy = TerminalProxy(
        SpawnFailureBackend([]),
        command=("missing-shell",),
        input_adapter=MemoryInput([]),
        output_adapter=MemoryOutput(),
        terminal_state_factory=TerminalState,
        dispatcher=TerminalEventDispatcher([recorder]),
        emulator=TerminalEmulator(columns=80, rows=24),
    )

    with pytest.raises(RuntimeError, match="spawn failed"):
        proxy.run()
    recorder.close()

    assert AsciicastV3Reader(cast_path).read().events == ()


def test_unknown_child_exit_is_not_encoded_as_success(tmp_path: Path) -> None:
    cast_path = tmp_path / "unknown-exit.cast"
    recorder = AsciicastV3Recorder(cast_path, columns=80, rows=24)
    proxy = TerminalProxy(
        ScriptedBackend([b""], exit_code=None),
        command=("bash",),
        input_adapter=MemoryInput([None]),
        output_adapter=MemoryOutput(),
        terminal_state_factory=TerminalState,
        dispatcher=TerminalEventDispatcher([recorder]),
        emulator=TerminalEmulator(columns=80, rows=24),
    )

    assert proxy.run() is None
    recorder.close()

    events = AsciicastV3Reader(cast_path).read().events
    assert [(event.code, event.data) for event in events] == [("x", "unknown")]


def test_proxy_waits_for_backend_eof_after_process_is_dead() -> None:
    class DeadBeforeEofBackend(ScriptedBackend):
        def is_alive(self) -> bool:
            return False

    backend = DeadBeforeEofBackend([None, b"late output", b""], exit_code=0)
    output = MemoryOutput()
    events: list[TerminalEvent] = []

    class EventSink:
        def handle(self, event: TerminalEvent) -> None:
            events.append(event)

    proxy = TerminalProxy(
        backend,
        command=("bash",),
        input_adapter=MemoryInput([b""]),
        output_adapter=output,
        terminal_state_factory=TerminalState,
        dispatcher=TerminalEventDispatcher([EventSink()]),
        emulator=TerminalEmulator(columns=80, rows=24),
    )

    assert proxy.run() == 0
    assert bytes(output.data) == b"late output"
    assert [event.type for event in events] == [
        TerminalEventType.OUTPUT,
        TerminalEventType.EXIT,
    ]


def test_read_failure_does_not_append_a_success_exit_event(tmp_path: Path) -> None:
    class ReadFailureBackend(ScriptedBackend):
        def read(self, max_bytes: int = 65536, timeout: float = 0.05) -> bytes | None:
            del max_bytes, timeout
            if self.reads:
                return self.reads.pop(0)
            raise OSError("pty read failed")

    cast_path = tmp_path / "session.cast"
    recorder = AsciicastV3Recorder(cast_path, columns=80, rows=24)
    proxy = TerminalProxy(
        ReadFailureBackend([b"reliable prefix"]),
        command=("bash",),
        input_adapter=MemoryInput([None]),
        output_adapter=MemoryOutput(),
        terminal_state_factory=TerminalState,
        dispatcher=TerminalEventDispatcher([recorder]),
        emulator=TerminalEmulator(columns=80, rows=24),
    )

    with pytest.raises(OSError, match="pty read failed"):
        proxy.run()
    recorder.close()

    assert [event.code for event in AsciicastV3Reader(cast_path).read().events] == ["o"]


def test_capture_failure_reports_result_and_keeps_child_session_alive() -> None:
    backend = ScriptedBackend([b"after-capture", b""], exit_code=0)
    statuses = []

    def fail_capture(snapshot, timestamp):
        del snapshot, timestamp
        raise OSError("capture sidecar failed")

    proxy = TerminalProxy(
        backend,
        command=("bash",),
        input_adapter=MemoryInput([b"\x1b[24~", b""]),
        output_adapter=MemoryOutput(),
        terminal_state_factory=TerminalState,
        dispatcher=TerminalEventDispatcher([]),
        emulator=TerminalEmulator(columns=80, rows=24),
        capture_handler=fail_capture,
        status_sink=statuses.append,
    )

    assert proxy.run() == 0
    assert backend.writes == []
    assert statuses[0].kind == "capture_failed"
    assert "Capture" in statuses[0].message


def test_capture_failure_feedback_stays_out_of_recorded_child_stream(tmp_path: Path) -> None:
    backend = ScriptedBackend([b"after-capture", b""], exit_code=0)
    output = MemoryOutput()
    presenter = LabSurfacePresenter(output, experiment_name="失败 Capture 实验")
    recorder = AsciicastV3Recorder(tmp_path / "session.cast", columns=80, rows=24)

    def fail_capture(snapshot, timestamp):
        del snapshot, timestamp
        raise OSError("capture sidecar failed")

    proxy = TerminalProxy(
        backend,
        command=("bash",),
        input_adapter=MemoryInput([b"\x1b[24~", b""]),
        output_adapter=output,
        terminal_state_factory=TerminalState,
        dispatcher=TerminalEventDispatcher([recorder]),
        emulator=TerminalEmulator(columns=80, rows=24),
        capture_handler=fail_capture,
        host_boundary=presenter.start,
        status_sink=presenter,
    )

    assert proxy.run() == 0
    recorder.close()

    cast_text = (tmp_path / "session.cast").read_text(encoding="utf-8")
    assert "after-capture" in cast_text
    assert "Capture 保存失败" not in cast_text
    assert "Capture 保存失败".encode() in output.data


def test_capture_marker_failure_rolls_back_sidecar_and_keeps_session_alive(
    tmp_path: Path,
) -> None:
    backend = ScriptedBackend([b"after-capture", b""], exit_code=0)
    store = CaptureStore(tmp_path / "captures.json")

    class CaptureEventFailure:
        def handle(self, event: TerminalEvent) -> None:
            if event.type is TerminalEventType.CAPTURE:
                raise RuntimeError("marker persistence failed")

    def create_capture(snapshot, timestamp):
        return store.create_capture(snapshot, timestamp, cwd=tmp_path)

    def rollback_capture(value: object) -> None:
        capture_id = value.capture_id
        store.delete(capture_id)

    proxy = TerminalProxy(
        backend,
        command=("bash",),
        input_adapter=MemoryInput([b"\x1b[24~", b""]),
        output_adapter=MemoryOutput(),
        terminal_state_factory=TerminalState,
        dispatcher=TerminalEventDispatcher([CaptureEventFailure()]),
        emulator=TerminalEmulator(columns=80, rows=24),
        capture_handler=create_capture,
        capture_rollback=rollback_capture,
    )

    assert proxy.run() == 0
    assert store.load().captures == ()
    assert proxy.status_events[0].kind == "capture_failed"
    assert backend.closed is True


def test_recorder_failure_rolls_back_capture_before_session_failure(tmp_path: Path) -> None:
    backend = ScriptedBackend([b""], exit_code=0)
    store = CaptureStore(tmp_path / "captures.json")

    class FailedRecorder:
        def handle(self, event: TerminalEvent) -> None:
            if event.type is TerminalEventType.CAPTURE:
                raise RecorderError("recorder unavailable")

    def create_capture(snapshot, timestamp):
        return store.create_capture(snapshot, timestamp, cwd=tmp_path)

    def rollback_capture(value: object) -> None:
        store.delete(value.capture_id)

    proxy = TerminalProxy(
        backend,
        command=("bash",),
        input_adapter=MemoryInput([b"\x1b[24~", b""]),
        output_adapter=MemoryOutput(),
        terminal_state_factory=TerminalState,
        dispatcher=TerminalEventDispatcher([FailedRecorder()]),
        emulator=TerminalEmulator(columns=80, rows=24),
        capture_handler=create_capture,
        capture_rollback=rollback_capture,
    )

    with pytest.raises(Exception) as caught:
        proxy.run()
    assert isinstance(caught.value.__cause__, RecorderError)
    assert store.load().captures == ()


def test_marker_sink_failure_rolls_back_already_recorded_capture_marker(tmp_path: Path) -> None:
    backend = ScriptedBackend([b""], exit_code=0)
    cast_path = tmp_path / "session.cast"
    recorder = AsciicastV3Recorder(cast_path, columns=80, rows=24)
    store = CaptureStore(tmp_path / "captures.json")

    class FailedMarkerSink:
        def handle(self, event: TerminalEvent) -> None:
            if event.type is TerminalEventType.CAPTURE:
                raise RuntimeError("marker sink failed")

    def create_capture(snapshot, timestamp):
        return store.create_capture(snapshot, timestamp, cwd=tmp_path)

    def rollback_capture(value: object) -> None:
        store.delete(value.capture_id)

    proxy = TerminalProxy(
        backend,
        command=("bash",),
        input_adapter=MemoryInput([b"\x1b[24~", b""]),
        output_adapter=MemoryOutput(),
        terminal_state_factory=TerminalState,
        dispatcher=TerminalEventDispatcher([recorder, FailedMarkerSink()]),
        emulator=TerminalEmulator(columns=80, rows=24),
        capture_handler=create_capture,
        capture_rollback=rollback_capture,
    )

    assert proxy.run() == 0
    recorder.close()

    assert [event.code for event in AsciicastV3Reader(cast_path).read().events] == ["x"]
    assert store.load().captures == ()


def test_capture_rollback_restores_recorder_timeline(tmp_path: Path) -> None:
    cast_path = tmp_path / "capture-rollback-timeline.cast"
    recorder = AsciicastV3Recorder(cast_path, columns=80, rows=24)
    capture = TerminalEvent(
        sequence=1,
        monotonic_time=1.0,
        relative_time=1.0,
        type=TerminalEventType.CAPTURE,
        payload="capture-1",
    )
    exit_event = TerminalEvent(
        sequence=2,
        monotonic_time=3.0,
        relative_time=3.0,
        type=TerminalEventType.EXIT,
        payload=0,
    )

    recorder.record(capture)
    recorder.rollback(capture)
    recorder.record(exit_event)
    recorder.close()

    events = AsciicastV3Reader(cast_path).read().events
    assert [(event.code, event.interval) for event in events] == [("x", 3.0)]


def test_capture_rollback_failure_escalates_recording_infrastructure_failure() -> None:
    backend = ScriptedBackend([b""], exit_code=0)

    class RecorderWithBrokenRollback:
        def handle(self, event: TerminalEvent) -> None:
            del event

        def rollback(self, event: TerminalEvent) -> None:
            del event
            raise RecorderError("capture marker rollback failed")

    class FailedMarkerSink:
        def handle(self, event: TerminalEvent) -> None:
            if event.type is TerminalEventType.CAPTURE:
                raise RuntimeError("marker sink failed")

    proxy = TerminalProxy(
        backend,
        command=("bash",),
        input_adapter=MemoryInput([b"\x1b[24~", b""]),
        output_adapter=MemoryOutput(),
        terminal_state_factory=TerminalState,
        dispatcher=TerminalEventDispatcher([RecorderWithBrokenRollback(), FailedMarkerSink()]),
        emulator=TerminalEmulator(columns=80, rows=24),
        capture_handler=lambda snapshot, timestamp: "capture-1",
    )

    with pytest.raises(RecorderError, match="rollback"):
        proxy.run()


def test_dispatcher_compensates_a_capture_sink_that_failed_after_partial_acceptance() -> None:
    calls: list[str] = []

    class PartialRecorder:
        def handle(self, event: TerminalEvent) -> None:
            del event
            calls.append("handle")
            raise RecorderError("capture queue acknowledgement failed")

        def rollback(self, event: TerminalEvent) -> None:
            del event
            calls.append("rollback")

    event = TerminalEvent(
        sequence=1,
        monotonic_time=1.0,
        relative_time=0.0,
        type=TerminalEventType.CAPTURE,
        payload="capture-1",
    )

    with pytest.raises(DispatchError):
        TerminalEventDispatcher([PartialRecorder()]).dispatch(event)

    assert calls == ["handle", "rollback"]


def test_proxy_applies_the_emulator_after_ordered_recording_dispatch() -> None:
    calls: list[str] = []

    class RecordingSink:
        def handle(self, event: TerminalEvent) -> None:
            del event
            calls.append("recorder")

    class LoggingEmulator(TerminalEmulator):
        def apply(self, event: TerminalEvent) -> None:
            calls.append("emulator")
            super().apply(event)

    proxy = TerminalProxy(
        ScriptedBackend([b"output", b""], exit_code=0),
        command=("bash",),
        input_adapter=MemoryInput([None]),
        output_adapter=MemoryOutput(),
        terminal_state_factory=TerminalState,
        dispatcher=TerminalEventDispatcher([RecordingSink()]),
        emulator=LoggingEmulator(columns=80, rows=24),
    )

    proxy.run()

    assert calls == ["recorder", "emulator", "recorder", "emulator"]


@pytest.mark.asyncio
async def test_review_empty_state_is_explicit_and_space_does_not_exit(tmp_path: Path) -> None:
    paths = SessionPaths(tmp_path / "empty-session")
    paths.root.mkdir(parents=True)
    write_cast_header(paths, columns=7, rows=3)
    write_metadata(paths, status="completed")
    controller = ReviewController.from_session(paths)

    view = controller.view()
    assert view.state == "empty"
    assert view.lifecycle_status == "completed"
    assert view.event_count == 0
    assert view.duration == 0.0

    app = ReviewApp(controller=controller, locale=load_locale())
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        await pilot.press("space")
        await pilot.pause()
        rendered = "\n".join(str(widget.renderable) for widget in app.screen.query(Static))
        assert app.is_running is True
        assert "此会话没有可播放终端事件" in rendered


@pytest.mark.asyncio
async def test_review_zero_duration_space_stays_open_with_no_playable_message(
    tmp_path: Path,
) -> None:
    paths = SessionPaths(tmp_path / "zero-duration-session")
    paths.root.mkdir(parents=True)
    paths.cast.write_text(
        json.dumps({"version": 3, "term": {"cols": 7, "rows": 3}})
        + "\n"
        + json.dumps([0.0, "o", "instant"])
        + "\n",
        encoding="utf-8",
    )
    write_metadata(paths, status="completed")
    controller = ReviewController.from_session(paths)

    app = ReviewApp(controller=controller, locale=load_locale())
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        await pilot.press("space")
        await pilot.pause()
        rendered = "\n".join(str(widget.renderable) for widget in app.screen.query(Static))

        assert app.is_running is True
        assert controller.playing is False
        assert controller.view().state == "empty"
        assert "此会话没有可播放终端事件" in rendered


def test_review_corrupt_cast_is_controlled_and_keeps_reliable_prefix(tmp_path: Path) -> None:
    paths = SessionPaths(tmp_path / "corrupt-session")
    paths.root.mkdir(parents=True)
    paths.cast.write_text("{broken header\n", encoding="utf-8")
    write_metadata(paths, status="failed")

    controller = ReviewController.from_session(paths)

    assert controller.view().state == "corrupt"
    assert controller.view().lifecycle_status == "failed"
    assert controller.view().warnings


@pytest.mark.parametrize("lifecycle_status", ("failed", "interrupted"))
def test_review_exposes_lifecycle_reason_and_partial_playback(
    tmp_path: Path,
    lifecycle_status: str,
) -> None:
    paths = SessionPaths(tmp_path / f"{lifecycle_status}-session")
    paths.root.mkdir(parents=True)
    paths.cast.write_text(
        "\n".join(
            [
                json.dumps({"version": 3, "term": {"cols": 8, "rows": 2}}),
                json.dumps([1.0, "o", "partial"]),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    write_metadata(paths, status=lifecycle_status)

    controller = ReviewController.from_session(paths)
    controller.seek(1.0)
    view = controller.view()

    assert view.state == lifecycle_status
    assert view.lifecycle_status == lifecycle_status
    assert view.lifecycle_reason is None
    assert view.event_count == 1
    assert view.capture_count == 0
    assert view.snapshot.cells[0][0].character == "p"


def test_replay_marks_a_truncated_tail_corrupt_but_keeps_valid_events(tmp_path: Path) -> None:
    paths = SessionPaths(tmp_path / "truncated-session")
    paths.root.mkdir(parents=True)
    paths.cast.write_text(
        json.dumps({"version": 3, "term": {"cols": 8, "rows": 2}})
        + "\n"
        + json.dumps([1.0, "o", "valid"])
        + "\n"
        + '[1.0,"o","unfinished"',
        encoding="utf-8",
    )
    write_metadata(paths, status="completed")

    controller = ReviewController.from_session(paths)
    controller.seek(1.0)
    view = controller.view()

    assert view.state == "corrupt"
    assert view.event_count == 1
    assert view.snapshot.cells[0][0].character == "v"


def test_review_checkpoint_persistence_failure_keeps_reliable_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import csbox.lab.replay as replay_module

    paths = SessionPaths(tmp_path / "checkpoint-failure-session")
    paths.root.mkdir(parents=True)
    paths.cast.write_text(
        json.dumps({"version": 3, "term": {"cols": 8, "rows": 2}})
        + "\n"
        + json.dumps([1.0, "o", "reliable"])
        + "\n",
        encoding="utf-8",
    )
    write_metadata(paths, status="completed")

    def fail_save(*args, **kwargs):
        del args, kwargs
        raise replay_module.CheckpointStoreError("checkpoint disk failure")

    monkeypatch.setattr(replay_module.CheckpointStore, "save", fail_save)

    controller = ReviewController.from_session(paths)
    controller.seek(1.0)
    view = controller.view()

    assert view.state == "playable"
    assert view.snapshot.cells[0][0].character == "r"
    assert any("checkpoint" in warning for warning in view.warnings)
