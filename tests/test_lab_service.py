from __future__ import annotations

import json
from pathlib import Path

import pytest

from csbox.config.models import CSBoxConfig
from csbox.core.events import TerminalSize
from csbox.lab.models import SessionPaths
from csbox.lab.repository import SessionRepository
from csbox.lab.service import LabService


class ScriptedBackend:
    def __init__(self, reads: list[bytes | None], *, crash: Exception | None = None) -> None:
        self.reads = list(reads)
        self.crash = crash
        self.writes: list[bytes] = []
        self.spawn_checked = False
        self.closed = False

    def spawn(self, command, *, cwd=None, env=None, size=None) -> None:
        del command, cwd, env, size
        self.spawn_checked = True

    def read(self, max_bytes: int = 65536, timeout: float = 0.05) -> bytes | None:
        del max_bytes, timeout
        if self.reads:
            return self.reads.pop(0)
        if self.crash is not None:
            raise self.crash
        return b""

    def write(self, data: bytes) -> int:
        self.writes.append(data)
        return len(data)

    def resize(self, columns: int, rows: int) -> None:
        del columns, rows

    def is_alive(self) -> bool:
        return bool(self.reads) and not self.closed

    def wait(self, timeout: float = 0.0) -> int | None:
        del timeout
        return 0

    @property
    def exit_code(self) -> int | None:
        return 0

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
    def __init__(self) -> None:
        self.restored = False

    def __enter__(self) -> TerminalState:
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.restored = True


def write_empty_cast(paths: SessionPaths) -> None:
    paths.cast.write_text(
        '{"version":3,"term":{"cols":80,"rows":24,"type":"xterm-256color"}}\n',
        encoding="utf-8",
    )


def make_service(tmp_path: Path, backend: ScriptedBackend, input_chunks: list[bytes | None]):
    repository = SessionRepository(tmp_path / "sessions")
    output = MemoryOutput()
    state = TerminalState()
    service = LabService(
        repository=repository,
        config=CSBoxConfig(),
        backend_factory=lambda: backend,
        input_adapter_factory=lambda: MemoryInput(input_chunks),
        output_adapter_factory=lambda: output,
        terminal_state_factory=lambda: state,
        cwd=tmp_path,
    )
    return service, repository, output, state


def test_service_creates_running_metadata_before_spawning_and_persists_capture(
    tmp_path: Path,
) -> None:
    backend = ScriptedBackend(["\x1b[31m输出中文\x1b[0m\n".encode(), b"", None])
    service, repository, output, state = make_service(tmp_path, backend, [b"\x1b[24~", b""])

    original_spawn = backend.spawn

    def observe_spawn(command, *, cwd=None, env=None, size=None) -> None:
        sessions = repository.list_sessions()
        assert len(sessions) == 1
        assert sessions[0].metadata.status == "running"
        original_spawn(command, cwd=cwd, env=env, size=size)

    backend.spawn = observe_spawn  # type: ignore[method-assign]
    result = service.start("中文实验", shell="bash", command=("bash",), size=TerminalSize(80, 24))

    assert result.status == "completed"
    assert result.exit_code == 0
    assert backend.spawn_checked is True
    assert bytes(output.data).startswith(b"\x1b[?1049h")
    assert bytes(output.data).endswith(b"\x1b[?1049l")
    assert "输出中文\x1b[0m\n".encode() in bytes(output.data)
    assert state.restored is True
    session = SessionPaths(result.session.root)
    metadata = json.loads(session.metadata.read_text(encoding="utf-8"))
    assert metadata["status"] == "completed"
    captures = json.loads(session.captures.read_text(encoding="utf-8"))
    assert len(captures["captures"]) == 1
    assert b"\x1b[24~" not in backend.writes
    assert session.cast.is_file()


def test_dedicated_host_mode_uses_fresh_main_buffer_without_alternate_screen(
    tmp_path: Path,
) -> None:
    backend = ScriptedBackend([b"child output", b""])
    service, repository, output, state = make_service(tmp_path, backend, [b"\x1b[24~", b""])
    ready: list[SessionPaths] = []

    result = service.start(
        "专用窗口实验",
        shell="bash",
        command=("bash",),
        size=TerminalSize(80, 24),
        dedicated_host=True,
        ready_callback=ready.append,
    )

    output_bytes = bytes(output.data)
    assert result.status == "completed"
    assert result.exit_code == 0
    assert ready == [result.session]
    assert b"child output" in output_bytes
    assert b"\x1b[?1049h" not in output_bytes
    assert b"\x1b[?1049l" not in output_bytes
    assert b"\nCSBox Lab" not in output_bytes
    assert b"\x1b]0;" in output_bytes
    assert b"\x1b]0;" not in result.session.cast.read_bytes()
    assert repository.list_sessions()[0].metadata.status == "completed"
    assert state.restored is True


def test_dedicated_host_mode_persists_nonzero_child_exit_without_fabricating_failure(
    tmp_path: Path,
) -> None:
    class NonZeroBackend(ScriptedBackend):
        @property
        def exit_code(self) -> int:
            return 17

        def wait(self, timeout: float = 0.0) -> int:
            del timeout
            return 17

    backend = NonZeroBackend([b"child reported an error", b""])
    service, repository, _, _ = make_service(tmp_path, backend, [None])

    result = service.start(
        "非零退出实验",
        shell="bash",
        command=("bash",),
        dedicated_host=True,
    )

    assert result.status == "completed"
    assert result.exit_code == 17
    metadata = repository.list_sessions()[0].metadata
    assert metadata.status == "completed"
    assert metadata.exit_code == 17


def test_parent_service_returns_only_after_injected_launcher_reports_ready(
    tmp_path: Path,
) -> None:
    class ReadyLauncher:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        def start(self, **kwargs: object) -> object:
            self.calls.append(kwargs)
            return type("Launch", (), {"session_id": "session-ready", "status": "running"})()

    launcher = ReadyLauncher()
    repository = SessionRepository(tmp_path / "sessions")
    service = LabService(
        repository=repository,
        config=CSBoxConfig(),
        cwd=tmp_path,
        windows_launcher=launcher,  # type: ignore[arg-type]
    )

    result = service.start("父进程只等待 READY", shell="bash", command=("bash",))

    assert result.session.root == repository.root / "session-ready"
    assert result.status == "running"
    assert launcher.calls[0]["command"] == ("bash",)
    assert repository.list_sessions() == ()


def test_service_marks_failed_and_keeps_recording_when_shell_crashes(tmp_path: Path) -> None:
    backend = ScriptedBackend([b"before crash"], crash=RuntimeError("shell crashed"))
    service, repository, _, state = make_service(tmp_path, backend, [None])

    with pytest.raises(RuntimeError, match="shell crashed"):
        service.start("崩溃实验", shell="bash", command=("bash",))

    session = repository.list_sessions()[0]
    assert session.metadata.status == "failed"
    assert session.paths.cast.read_text(encoding="utf-8").find("before crash") >= 0
    assert state.restored is True


def test_service_reports_cleanup_failure_after_a_completed_child_exit(tmp_path: Path) -> None:
    class CleanupFailureBackend(ScriptedBackend):
        def close(self) -> None:
            self.closed = True
            raise OSError("closed handle")

    backend = CleanupFailureBackend([b"finished", b""])
    service, repository, _, state = make_service(tmp_path, backend, [None])

    with pytest.raises(RuntimeError, match="实验运行结束，但终端清理失败"):
        service.start("清理阶段实验", shell="bash", command=("bash",))

    metadata = repository.list_sessions()[0].metadata
    assert metadata.status == "failed"
    assert metadata.status_reason == "实验运行结束，但终端清理失败。"
    assert metadata.exit_code == 0
    assert state.restored is True


def test_service_marks_keyboard_interrupt_as_interrupted(tmp_path: Path) -> None:
    backend = ScriptedBackend([], crash=KeyboardInterrupt())
    service, repository, _, _ = make_service(tmp_path, backend, [None])

    with pytest.raises(KeyboardInterrupt):
        service.start("中断实验", shell="bash", command=("bash",))

    assert repository.list_sessions()[0].metadata.status == "interrupted"


def test_session_repository_resolves_exact_and_unique_prefixes(tmp_path: Path) -> None:
    repository = SessionRepository(tmp_path / "sessions")
    first = repository.create_running(
        "第一实验",
        platform="linux",
        shell="bash",
        shell_version=None,
        size=TerminalSize(80, 24),
        cwd=tmp_path,
    )
    write_empty_cast(first)
    repository.finish(first, "completed", exit_code=0)

    assert repository.resolve(first.root.name).root == first.root
    assert repository.resolve(first.root.name[:8]).root == first.root
    assert repository.list_sessions()[0].metadata.status == "completed"


def test_session_repository_prefers_exact_id_over_a_longer_prefix(tmp_path: Path) -> None:
    repository = SessionRepository(tmp_path / "sessions")
    for identifier in ("abc", "abcdef"):
        paths = repository.create_running(
            identifier,
            platform="linux",
            shell="bash",
            shell_version=None,
            size=TerminalSize(80, 24),
            cwd=tmp_path,
            session_id=identifier,
        )
        write_empty_cast(paths)
        repository.finish(paths, "completed", exit_code=0)

    assert repository.resolve("abc").root.name == "abc"


def test_session_repository_skips_corrupt_metadata_when_listing(tmp_path: Path) -> None:
    repository = SessionRepository(tmp_path / "sessions")
    valid = repository.create_running(
        "有效实验",
        platform="linux",
        shell="bash",
        shell_version=None,
        size=TerminalSize(80, 24),
        cwd=tmp_path,
        session_id="valid",
    )
    write_empty_cast(valid)
    repository.finish(valid, "completed", exit_code=0)
    corrupt = repository.root / "corrupt"
    corrupt.mkdir()
    (corrupt / "metadata.json").write_text("{", encoding="utf-8")

    sessions = repository.list_sessions()

    assert [item.paths.root.name for item in sessions] == ["valid"]


def test_session_repository_generates_timestamped_name_for_blank_input(tmp_path: Path) -> None:
    repository = SessionRepository(tmp_path / "sessions")
    paths = repository.create_running(
        "",
        platform="linux",
        shell="bash",
        shell_version=None,
        size=TerminalSize(80, 24),
        cwd=tmp_path,
    )
    write_empty_cast(paths)

    assert repository.list_sessions()[0].metadata.experiment_name.startswith("实验-")
