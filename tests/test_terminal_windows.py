from __future__ import annotations

import builtins
import inspect
import os
import subprocess
import sys
import threading
import time
from collections import deque
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import ClassVar

import pytest

from csbox.core.events import TerminalSize
from csbox.core.terminal import TerminalBackendError
from csbox.core.terminal_windows import WindowsConPTYBackend


class FakePtyProcess:
    """Contract fake for pywinpty 3.0.5's high-level PtyProcess API."""

    next_process: ClassVar[FakePtyProcess | None] = None
    spawn_calls: ClassVar[list[dict[str, object]]] = []
    instances: ClassVar[list[FakePtyProcess]] = []

    def __init__(
        self,
        *,
        frames: Sequence[str | BaseException] = (),
        alive: bool = True,
        exitstatus: int | None = None,
        write_results: Sequence[int | BaseException] = (),
        close_failures: Sequence[BaseException] = (),
        read_gate: threading.Event | None = None,
    ) -> None:
        self.frames = deque(frames)
        self.alive = alive
        self.exitstatus = exitstatus
        self.write_results = deque(write_results)
        self.close_failures = deque(close_failures)
        self.writes: list[str] = []
        self.sizes: list[tuple[int, int]] = []
        self.close_calls = 0
        self.close_forces: list[bool] = []
        self.closed = False
        self.read_gate = read_gate
        self.read_started = threading.Event()
        self._condition = threading.Condition()
        type(self).instances.append(self)

    @classmethod
    def spawn(
        cls,
        argv: Sequence[str],
        *,
        cwd: str | None,
        env: Mapping[str, str] | None,
        dimensions: tuple[int, int],
        backend: object,
    ) -> FakePtyProcess:
        # Mirrors pywinpty 3.0.5 ptyprocess.py:93-94.
        resolved_backend = backend or os.environ.get("PYWINPTY_BACKEND")
        resolved_backend = int(resolved_backend) if resolved_backend is not None else None
        cls.spawn_calls.append(
            {
                "argv": list(argv),
                "cwd": cwd,
                "env": None if env is None else dict(env),
                "dimensions": dimensions,
                "backend": backend,
                "resolved_backend": resolved_backend,
            }
        )
        assert cls.next_process is not None
        return cls.next_process

    def read(self, size: int = 1024) -> str:
        if self.read_gate is not None:
            self.read_started.set()
            self.read_gate.wait()
        with self._condition:
            while not self.frames and self.alive and not self.closed:
                self._condition.wait()
            if self.frames:
                frame = self.frames.popleft()
            else:
                raise EOFError("Pty is closed")
        if isinstance(frame, BaseException):
            raise frame
        if len(frame) > size:
            with self._condition:
                self.frames.appendleft(frame[size:])
        return frame[:size]

    def write(self, data: str) -> int:
        self.writes.append(data)
        if self.write_results:
            result = self.write_results.popleft()
            if isinstance(result, BaseException):
                raise result
            return result
        return len(data.encode("utf-8"))

    def setwinsize(self, rows: int, columns: int) -> None:
        self.sizes.append((rows, columns))

    def isalive(self) -> bool:
        return self.alive

    def close(self, force: bool = False) -> None:
        self.close_calls += 1
        self.close_forces.append(force)
        if self.close_failures:
            failure = self.close_failures.popleft()
            raise failure
        with self._condition:
            self.frames.clear()
            self.alive = False
            self.closed = True
            if self.read_gate is not None:
                self.read_gate.set()
            self._condition.notify_all()

    def feed(self, frame: str | BaseException) -> None:
        with self._condition:
            self.frames.append(frame)
            self._condition.notify_all()


class FakePywinptyPtyProcess(FakePtyProcess):
    """Marks the fake as pywinpty's async high-level process adapter."""

    __module__ = "winpty.ptyprocess"


@pytest.fixture(autouse=True)
def reset_fake() -> Iterator[None]:
    FakePtyProcess.next_process = None
    FakePtyProcess.spawn_calls = []
    FakePtyProcess.instances = []
    yield
    for process in FakePtyProcess.instances:
        if not process.closed:
            process.close_failures.clear()
            process.close(force=True)


def make_backend(
    process: FakePtyProcess, *, write_timeout: float | None = None
) -> WindowsConPTYBackend:
    FakePtyProcess.next_process = process
    if write_timeout is None:
        return WindowsConPTYBackend(pty_process_factory=FakePtyProcess)
    return WindowsConPTYBackend(pty_process_factory=FakePtyProcess, write_timeout=write_timeout)


def test_background_reader_applies_bounded_output_backpressure(tmp_path: Path) -> None:
    limit = 1024
    chunk = "中" * 64
    process = FakePtyProcess(frames=[chunk] * 40 + [EOFError()], alive=False, exitstatus=0)
    FakePtyProcess.next_process = process
    backend = WindowsConPTYBackend(
        pty_process_factory=FakePtyProcess,
        max_output_buffer_bytes=limit,
    )
    spawn_backend(backend, tmp_path)

    deadline = time.monotonic() + 1.0
    with backend._output_ready:
        while backend._buffered_output_bytes < limit and time.monotonic() < deadline:
            backend._output_ready.wait(deadline - time.monotonic())
        assert backend._buffered_output_bytes <= limit
        assert process.frames, "reader should stop consuming when the bounded backlog is full"

    received = bytearray()
    while True:
        frame = backend.read(max_bytes=257, timeout=0.5)
        assert frame is not None
        if frame == b"":
            break
        received.extend(frame)

    assert bytes(received) == (chunk * 40).encode("utf-8")


def spawn_backend(backend: WindowsConPTYBackend, tmp_path: Path) -> None:
    backend.spawn(
        ["pwsh.exe", "-NoLogo"],
        cwd=tmp_path,
        env={"CSBOX_TEST": "值"},
        size=TerminalSize(100, 30),
    )


def test_fake_matches_pywinpty_305_read_write_and_close_signatures() -> None:
    read_parameters = inspect.signature(FakePtyProcess.read).parameters
    write_parameters = inspect.signature(FakePtyProcess.write).parameters
    close_parameters = inspect.signature(FakePtyProcess.close).parameters

    assert tuple(read_parameters) == ("self", "size")
    assert read_parameters["size"].default == 1024
    assert tuple(write_parameters) == ("self", "data")
    assert tuple(close_parameters) == ("self", "force")
    assert close_parameters["force"].default is False


def test_spawn_forces_conpty_despite_hostile_backend_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PYWINPTY_BACKEND", "1")
    process = FakePtyProcess()
    backend = make_backend(process)

    spawn_backend(backend, tmp_path)

    call = FakePtyProcess.spawn_calls[0]
    assert call["dimensions"] == (30, 100)
    assert call["backend"] == "0"
    assert call["resolved_backend"] == 0


def test_spawn_merges_environment_overrides_with_parent_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", "C:\\Windows\\System32")
    monkeypatch.setenv("SystemRoot", "C:\\Windows")
    backend = make_backend(FakePtyProcess())

    spawn_backend(backend, tmp_path)

    spawned_env = FakePtyProcess.spawn_calls[0]["env"]
    assert isinstance(spawned_env, dict)
    assert spawned_env["PATH"] == "C:\\Windows\\System32"
    assert (
        next(value for name, value in spawned_env.items() if name.casefold() == "systemroot")
        == "C:\\Windows"
    )
    assert spawned_env["CSBOX_TEST"] == "值"


def test_background_reader_encodes_utf8_and_returns_none_without_data(tmp_path: Path) -> None:
    process = FakePtyProcess()
    backend = make_backend(process)
    spawn_backend(backend, tmp_path)

    started = time.monotonic()
    assert backend.read(timeout=0.01) is None
    assert time.monotonic() - started < 0.2

    process.feed("红色中文")
    assert backend.read(timeout=0.5) == "红色中文".encode()


def test_read_respects_max_bytes_without_losing_utf8_data(tmp_path: Path) -> None:
    process = FakePtyProcess(frames=["红色"])
    backend = make_backend(process)
    spawn_backend(backend, tmp_path)

    first = backend.read(max_bytes=4, timeout=0.5)
    second = backend.read(max_bytes=4, timeout=0.5)

    assert first is not None
    assert second is not None
    assert first + second == "红色".encode()
    assert len(first) <= 4
    assert len(second) <= 4


def test_write_buffers_split_utf8_until_a_complete_character(tmp_path: Path) -> None:
    process = FakePtyProcess()
    backend = make_backend(process)
    spawn_backend(backend, tmp_path)
    encoded = "中\n".encode()

    assert backend.write(encoded[:2]) == 2
    assert process.writes == []
    assert backend.write(encoded[2:]) == len(encoded[2:])
    assert process.writes == ["中\n"]


def test_write_retries_partial_progress_without_losing_bytes(tmp_path: Path) -> None:
    process = FakePtyProcess(write_results=[1, 2])
    backend = make_backend(process)
    spawn_backend(backend, tmp_path)

    assert backend.write(b"abc") == 3
    assert process.writes == ["abc", "bc"]


def test_write_tolerates_transient_zero_progress_during_shell_startup(tmp_path: Path) -> None:
    process = FakePtyProcess(write_results=[0] * 200 + [3])
    backend = make_backend(process)
    spawn_backend(backend, tmp_path)

    assert backend.write(b"abc") == 3
    assert process.writes[-1] == "abc"


def test_pywinpty_async_zero_result_does_not_resend_submitted_input(tmp_path: Path) -> None:
    process = FakePtyProcess(write_results=[0])
    FakePywinptyPtyProcess.next_process = process
    backend = WindowsConPTYBackend(pty_process_factory=FakePywinptyPtyProcess)
    spawn_backend(backend, tmp_path)

    assert backend.write(b"abc") == 3
    assert process.writes == ["abc"]


def test_write_zero_progress_is_bounded_and_preserves_cause(tmp_path: Path) -> None:
    process = FakePtyProcess(write_results=[0] * 100)
    backend = make_backend(process, write_timeout=0.01)
    spawn_backend(backend, tmp_path)

    started = time.monotonic()
    with pytest.raises(TerminalBackendError) as caught:
        backend.write(b"blocked")

    assert time.monotonic() - started < 0.2
    assert isinstance(caught.value.cause, BlockingIOError)
    assert process.writes


def test_resize_uses_rows_before_columns(tmp_path: Path) -> None:
    process = FakePtyProcess()
    backend = make_backend(process)
    spawn_backend(backend, tmp_path)

    backend.resize(120, 40)

    assert process.sizes == [(40, 120)]


def test_eof_error_becomes_eof_and_captures_exit_status(tmp_path: Path) -> None:
    process = FakePtyProcess(frames=[EOFError()], alive=False, exitstatus=9)
    backend = make_backend(process)
    spawn_backend(backend, tmp_path)

    assert backend.read(timeout=0.5) == b""
    assert backend.exit_code == 9
    assert backend.wait(timeout=0.01) == 9
    assert not backend.is_alive()


def test_closed_conpty_output_handle_after_child_exit_is_expected_eof(tmp_path: Path) -> None:
    class ClosedOutputHandle(OSError):
        winerror = 109  # ERROR_BROKEN_PIPE

    process = FakePtyProcess(
        frames=[ClosedOutputHandle("pipe closed")],
        alive=False,
        exitstatus=0,
    )
    backend = make_backend(process)
    spawn_backend(backend, tmp_path)

    assert backend.read(timeout=0.5) == b""
    assert backend.exit_code == 0


def test_eof_while_child_is_alive_is_backend_failure(tmp_path: Path) -> None:
    process = FakePtyProcess(frames=[EOFError("pipe closed")], alive=True)
    backend = make_backend(process)
    spawn_backend(backend, tmp_path)

    with pytest.raises(TerminalBackendError) as caught:
        backend.read(timeout=1.0)

    assert isinstance(caught.value.cause, RuntimeError)
    backend.close()
    assert process.close_forces == [True]


def test_empty_pywinpty_sentinel_is_not_eof(tmp_path: Path) -> None:
    class SentinelThenExitProcess(FakePtyProcess):
        def read(self, size: int = 1024) -> str:
            try:
                return super().read(size)
            except EOFError:
                self.alive = False
                raise

    process = SentinelThenExitProcess(frames=["", "after-sentinel", EOFError()])
    backend = make_backend(process)
    spawn_backend(backend, tmp_path)

    assert backend.read(timeout=0.5) == b"after-sentinel"
    assert backend.read(timeout=0.5) == b""
    backend.close()


def test_eof_waits_for_a_racing_child_exit(tmp_path: Path) -> None:
    class RacyExitProcess(FakePtyProcess):
        def __init__(self, **kwargs) -> None:
            super().__init__(**kwargs)
            self.status_checks = 0

        def isalive(self) -> bool:
            self.status_checks += 1
            if self.status_checks >= 2:
                self.alive = False
            return super().isalive()

    process = RacyExitProcess(frames=[EOFError()], alive=True, exitstatus=7)
    backend = make_backend(process)
    spawn_backend(backend, tmp_path)

    assert backend.read(timeout=0.5) == b""
    assert backend.exit_code == 7
    backend.close()


def test_closed_conpty_handle_during_natural_cleanup_is_not_backend_failure(
    tmp_path: Path,
) -> None:
    class ClosedHandle(OSError):
        winerror = 6  # ERROR_INVALID_HANDLE

    process = FakePtyProcess(
        frames=[EOFError()],
        alive=False,
        exitstatus=0,
        close_failures=[ClosedHandle("already closed")],
    )
    backend = make_backend(process)
    spawn_backend(backend, tmp_path)

    assert backend.read(timeout=0.5) == b""
    backend.close()
    assert backend.exit_code == 0


def test_close_waits_for_delayed_final_frame_before_closing_dead_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    read_gate = threading.Event()
    process = FakePtyProcess(frames=["最后一帧"], alive=False, exitstatus=0, read_gate=read_gate)
    backend = make_backend(process)
    spawn_backend(backend, tmp_path)
    assert process.read_started.wait(0.5)

    reader = backend._reader_thread
    assert reader is not None
    real_join = reader.join
    join_close_states: list[bool] = []

    def release_delayed_frame_while_joining(timeout: float | None = None) -> None:
        join_close_states.append(process.closed)
        if not process.closed:
            read_gate.set()
        real_join(timeout)

    monkeypatch.setattr(reader, "join", release_delayed_frame_while_joining)

    backend.close()
    backend.close()

    assert join_close_states[0] is False
    assert process.close_calls == 1
    assert process.close_forces == [False]
    assert backend.read(timeout=0.1) == "最后一帧".encode()
    assert backend.read(timeout=0.1) == b""
    assert backend.exit_code == 0


def test_close_dead_process_has_finite_bound_when_reader_never_finishes(
    tmp_path: Path,
) -> None:
    read_gate = threading.Event()
    process = FakePtyProcess(
        frames=["无法读取的尾帧"], alive=False, exitstatus=0, read_gate=read_gate
    )
    backend = make_backend(process)
    spawn_backend(backend, tmp_path)
    assert process.read_started.wait(0.5)

    started = time.monotonic()
    backend.close()

    assert time.monotonic() - started < 1.5
    assert process.close_forces == [False]
    assert backend.read(timeout=0.1) == b""


def test_close_forces_a_live_process_after_a_bounded_grace_period(tmp_path: Path) -> None:
    process = FakePtyProcess(alive=True)
    backend = make_backend(process)
    spawn_backend(backend, tmp_path)

    started = time.monotonic()
    backend.close()

    assert time.monotonic() - started < 1.0
    assert process.close_forces == [True]


def test_close_failure_is_retryable_and_does_not_mark_backend_closed(tmp_path: Path) -> None:
    process = FakePtyProcess(alive=True, close_failures=[OSError("busy")])
    backend = make_backend(process)
    spawn_backend(backend, tmp_path)

    with pytest.raises(TerminalBackendError) as caught:
        backend.close()

    assert caught.value.cause.args == ("busy",)
    assert backend.is_alive()
    backend.close()
    assert process.close_calls == 2
    assert backend.read(timeout=0.1) == b""


def test_invalid_resize_and_invalid_utf8_input_are_rejected(tmp_path: Path) -> None:
    backend = make_backend(FakePtyProcess())
    spawn_backend(backend, tmp_path)

    with pytest.raises(ValueError, match="正数"):
        backend.resize(0, 24)
    with pytest.raises(UnicodeDecodeError):
        backend.write(b"\xff")


def test_missing_pywinpty_has_chinese_guidance_and_preserves_cause(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_import = builtins.__import__

    def fail_winpty(name: str, *args: object, **kwargs: object) -> object:
        if name == "winpty":
            raise ModuleNotFoundError("No module named 'winpty'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fail_winpty)
    backend = WindowsConPTYBackend()

    with pytest.raises(TerminalBackendError) as caught:
        backend.spawn(["pwsh.exe"])

    assert "未安装 pywinpty" in caught.value.user_message
    assert isinstance(caught.value.cause, ModuleNotFoundError)
    assert caught.value.__cause__ is caught.value.cause


def test_importing_terminal_core_on_linux_does_not_import_winpty() -> None:
    code = "import sys; import csbox.core.terminal; assert 'winpty' not in sys.modules"

    completed = subprocess.run([sys.executable, "-c", code], check=False)

    assert completed.returncode == 0
