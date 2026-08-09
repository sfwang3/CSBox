from __future__ import annotations

import builtins
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import ClassVar

import pytest

from csbox.core.events import TerminalSize
from csbox.core.terminal import TerminalBackendError
from csbox.core.terminal_windows import WindowsConPTYBackend


class FakePtyProcess:
    next_process: ClassVar[FakePtyProcess | None] = None
    spawn_calls: ClassVar[list[dict[str, object]]] = []

    def __init__(
        self,
        *,
        frames: Sequence[str | BaseException] = (),
        alive: bool = True,
        exitstatus: int | None = None,
    ) -> None:
        self.frames = list(frames)
        self.alive = alive
        self.exitstatus = exitstatus
        self.writes: list[str] = []
        self.sizes: list[tuple[int, int]] = []
        self.close_calls = 0

    @classmethod
    def spawn(
        cls,
        argv: Sequence[str],
        *,
        cwd: str | None,
        env: Mapping[str, str] | None,
        dimensions: tuple[int, int],
        backend: int,
    ) -> FakePtyProcess:
        cls.spawn_calls.append(
            {
                "argv": list(argv),
                "cwd": cwd,
                "env": None if env is None else dict(env),
                "dimensions": dimensions,
                "backend": backend,
            }
        )
        assert cls.next_process is not None
        return cls.next_process

    def read(self, size: int = 1000, blocking: bool = False) -> str:
        del size, blocking
        if not self.frames:
            return ""
        frame = self.frames.pop(0)
        if isinstance(frame, BaseException):
            raise frame
        return frame

    def write(self, data: str) -> None:
        self.writes.append(data)

    def setwinsize(self, rows: int, columns: int) -> None:
        self.sizes.append((rows, columns))

    def isalive(self) -> bool:
        return self.alive

    def close(self) -> None:
        self.close_calls += 1
        self.alive = False


@pytest.fixture(autouse=True)
def reset_fake() -> None:
    FakePtyProcess.next_process = None
    FakePtyProcess.spawn_calls = []


def make_backend(process: FakePtyProcess) -> WindowsConPTYBackend:
    FakePtyProcess.next_process = process
    return WindowsConPTYBackend(pty_process_factory=FakePtyProcess)


def spawn_backend(backend: WindowsConPTYBackend, tmp_path: Path) -> None:
    backend.spawn(
        ["pwsh.exe", "-NoLogo"],
        cwd=tmp_path,
        env={"CSBOX_TEST": "值"},
        size=TerminalSize(100, 30),
    )


def test_spawn_explicitly_requests_conpty_with_rows_before_columns(tmp_path: Path) -> None:
    process = FakePtyProcess()
    backend = make_backend(process)

    spawn_backend(backend, tmp_path)

    assert FakePtyProcess.spawn_calls == [
        {
            "argv": ["pwsh.exe", "-NoLogo"],
            "cwd": str(tmp_path),
            "env": {"CSBOX_TEST": "值"},
            "dimensions": (30, 100),
            "backend": 0,
        }
    ]


def test_read_encodes_utf8_and_distinguishes_no_data(tmp_path: Path) -> None:
    process = FakePtyProcess(frames=["红色中文", ""], alive=True)
    backend = make_backend(process)
    spawn_backend(backend, tmp_path)

    assert backend.read() == "红色中文".encode()
    assert backend.read() is None


def test_read_respects_max_bytes_without_losing_utf8_data(tmp_path: Path) -> None:
    process = FakePtyProcess(frames=["红色"], alive=True)
    backend = make_backend(process)
    spawn_backend(backend, tmp_path)

    first = backend.read(max_bytes=4)
    second = backend.read(max_bytes=4)

    assert first is not None
    assert second is not None
    assert first + second == "红色".encode()
    assert len(first) <= 4
    assert len(second) <= 4


def test_write_decodes_utf8_and_resize_uses_rows_before_columns(tmp_path: Path) -> None:
    process = FakePtyProcess()
    backend = make_backend(process)
    spawn_backend(backend, tmp_path)

    data = "输入中文\n".encode()

    assert backend.write(data) == len(data)
    backend.resize(120, 40)
    assert process.writes == ["输入中文\n"]
    assert process.sizes == [(40, 120)]


def test_eof_error_becomes_eof_and_captures_exit_status(tmp_path: Path) -> None:
    process = FakePtyProcess(frames=[EOFError()], alive=False, exitstatus=9)
    backend = make_backend(process)
    spawn_backend(backend, tmp_path)

    assert backend.read() == b""
    assert backend.exit_code == 9
    assert backend.wait(timeout=0.01) == 9
    assert not backend.is_alive()


def test_close_drains_last_frame_and_is_idempotent(tmp_path: Path) -> None:
    process = FakePtyProcess(frames=["最后一帧", ""], alive=False, exitstatus=0)
    backend = make_backend(process)
    spawn_backend(backend, tmp_path)

    backend.close()
    backend.close()

    assert process.close_calls == 1
    assert backend.read() == "最后一帧".encode()
    assert backend.read() == b""
    assert backend.exit_code == 0


def test_invalid_resize_and_non_utf8_input_are_rejected(tmp_path: Path) -> None:
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
