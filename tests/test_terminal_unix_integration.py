from __future__ import annotations

import os
import shutil
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from csbox.core.events import TerminalSize
from csbox.core.terminal import TerminalBackendError, UnixPTYBackend

pytestmark = [
    pytest.mark.pty,
    pytest.mark.skipif(os.name == "nt", reason="Unix PTY integration only"),
]


@pytest.fixture
def backend() -> Iterator[UnixPTYBackend]:
    instance = UnixPTYBackend()
    try:
        yield instance
    finally:
        instance.close()


def read_until(
    backend: UnixPTYBackend,
    target: bytes,
    *,
    timeout: float = 5.0,
) -> bytes:
    deadline = time.monotonic() + timeout
    output = bytearray()
    while time.monotonic() < deadline:
        chunk = backend.read(timeout=min(0.05, max(0.0, deadline - time.monotonic())))
        if chunk is None:
            continue
        if chunk == b"":
            break
        output.extend(chunk)
        if target in output:
            return bytes(output)
    pytest.fail(f"timed out waiting for {target!r}; output={bytes(output)!r}")


def read_to_eof(backend: UnixPTYBackend, *, timeout: float = 5.0) -> bytes:
    deadline = time.monotonic() + timeout
    output = bytearray()
    while time.monotonic() < deadline:
        chunk = backend.read(timeout=min(0.05, max(0.0, deadline - time.monotonic())))
        if chunk is None:
            continue
        if chunk == b"":
            return bytes(output)
        output.extend(chunk)
    pytest.fail(f"timed out waiting for EOF; output tail={bytes(output[-200:])!r}")


def bash_command() -> list[str]:
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash is unavailable")
    return [bash, "--noprofile", "--norc"]


def test_real_pty_preserves_unicode_ansi_cwd_and_resize(
    backend: UnixPTYBackend, tmp_path: Path
) -> None:
    backend.spawn(bash_command(), cwd=tmp_path, size=TerminalSize(80, 24))

    unicode_command = "printf '\\033[31m红色中文\\033[0m\\n'\n".encode()
    backend.write(unicode_command)
    output = read_until(backend, b"\x1b[0m")
    assert b"\x1b[31m" in output
    assert "红色中文".encode() in output

    backend.write(b"pwd\n")
    assert os.fsencode(tmp_path) in read_until(backend, os.fsencode(tmp_path))

    backend.resize(100, 30)
    backend.write(b"stty size\nexit\n")
    assert b"30 100" in read_until(backend, b"30 100")
    read_to_eof(backend)
    assert backend.wait(timeout=1.0) == 0
    assert backend.exit_code == 0


def test_spawn_reports_and_reaps_an_invalid_command(backend: UnixPTYBackend) -> None:
    command = "/definitely/missing/csbox-command"

    with pytest.raises(TerminalBackendError) as caught:
        backend.spawn([command], size=TerminalSize(80, 24))

    assert "无法启动" in caught.value.user_message
    assert isinstance(caught.value.cause, FileNotFoundError)
    assert caught.value.__cause__ is caught.value.cause
    assert not backend.is_alive()


def test_spawn_reports_and_reaps_an_invalid_cwd(backend: UnixPTYBackend, tmp_path: Path) -> None:
    missing_cwd = tmp_path / "missing"

    with pytest.raises(TerminalBackendError) as caught:
        backend.spawn(
            [shutil.which("sh") or "/bin/sh", "-c", "exit 0"],
            cwd=missing_cwd,
            size=TerminalSize(80, 24),
        )

    assert "无法启动" in caught.value.user_message
    assert isinstance(caught.value.cause, FileNotFoundError)
    assert caught.value.__cause__ is caught.value.cause
    assert not backend.is_alive()


def test_child_observes_initial_size_before_its_first_output(
    backend: UnixPTYBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    import pty

    real_fork = pty.fork

    def delay_only_the_parent() -> tuple[int, int]:
        pid, master_fd = real_fork()
        if pid != 0:
            time.sleep(0.1)
        return pid, master_fd

    monkeypatch.setattr(pty, "fork", delay_only_the_parent)
    shell = shutil.which("sh") or "/bin/sh"

    backend.spawn([shell, "-c", "stty size"], size=TerminalSize(123, 41))
    output = read_to_eof(backend)

    assert b"41 123" in output
    assert backend.wait(timeout=1.0) == 0


def test_read_distinguishes_no_data_from_eof(backend: UnixPTYBackend) -> None:
    shell = shutil.which("sh") or "/bin/sh"
    backend.spawn([shell, "-c", "sleep 0.3"], size=TerminalSize(80, 24))

    assert backend.read(timeout=0.01) is None
    read_to_eof(backend)
    assert backend.read(timeout=0.01) == b""


def test_ctrl_c_interrupts_a_foreground_command(backend: UnixPTYBackend) -> None:
    backend.spawn(bash_command(), size=TerminalSize(80, 24))
    backend.write(b"stty -echo\n")
    time.sleep(0.1)
    while backend.read(timeout=0.01) not in (None, b""):
        pass

    started = time.monotonic()
    backend.write(b"sleep 30\n")
    time.sleep(0.1)
    backend.write(b"\x03")
    backend.write(b"printf 'CSBOX_CTRL_C_OK\\n'\nexit\n")

    assert b"CSBOX_CTRL_C_OK" in read_until(backend, b"CSBOX_CTRL_C_OK")
    assert time.monotonic() - started < 5.0
    read_to_eof(backend)
    assert backend.wait(timeout=1.0) == 0


def test_long_output_completes_without_deadlock(backend: UnixPTYBackend) -> None:
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash is unavailable")
    backend.spawn(
        [bash, "-c", "for ((i=0; i<12000; i++)); do printf 'line-%05d\\n' \"$i\"; done"],
        size=TerminalSize(80, 24),
    )

    output = read_to_eof(backend, timeout=10.0)

    assert b"line-00000" in output
    assert b"line-11999" in output
    assert backend.wait(timeout=1.0) == 0


def test_write_retries_eagain_after_the_master_becomes_writable(
    backend: UnixPTYBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend.spawn(bash_command(), size=TerminalSize(80, 24))
    real_write = os.write
    injected = False

    def eagain_once(fd: int, data: bytes) -> int:
        nonlocal injected
        if not injected:
            injected = True
            raise BlockingIOError(11, "temporarily unavailable")
        return real_write(fd, data)

    monkeypatch.setattr(os, "write", eagain_once)

    assert backend.write(b"printf 'EAGAIN_OK\\n'\nexit\n") > 0
    assert b"EAGAIN_OK" in read_until(backend, b"EAGAIN_OK")
    read_to_eof(backend)
    assert backend.wait(timeout=1.0) == 0


def test_large_paste_completes_through_nonblocking_backpressure(
    backend: UnixPTYBackend,
) -> None:
    payload = b"x" * (512 * 1024)
    shell = shutil.which("sh") or "/bin/sh"
    python = shutil.which("python") or shutil.which("python3")
    if python is None:
        pytest.skip("python is unavailable")
    reader = "; ".join(
        (
            "import sys",
            "data=sys.stdin.buffer.read(524288)",
            "sys.stdout.write(f'PASTE={len(data)}\\n')",
        )
    )
    backend.spawn(
        [
            shell,
            "-c",
            f"stty raw -echo; printf 'PASTE_READY\\n'; sleep 0.1; {python} -c \"{reader}\"",
        ],
        size=TerminalSize(80, 24),
    )
    read_until(backend, b"PASTE_READY")

    deadline = time.monotonic() + 10.0
    offset = 0
    while offset < len(payload) and time.monotonic() < deadline:
        written = backend.write(payload[offset:])
        assert written > 0
        offset += written

    assert offset == len(payload)
    assert b"PASTE=524288" in read_until(backend, b"PASTE=524288", timeout=10.0)
    read_to_eof(backend)
    assert backend.wait(timeout=1.0) == 0


def test_wait_returns_exit_code_and_reaps_child(backend: UnixPTYBackend) -> None:
    shell = shutil.which("sh") or "/bin/sh"
    backend.spawn([shell, "-c", "exit 7"], size=TerminalSize(80, 24))

    read_to_eof(backend)

    assert backend.wait(timeout=1.0) == 7
    assert backend.exit_code == 7
    assert not backend.is_alive()


def test_wait_times_out_and_close_is_idempotent(backend: UnixPTYBackend) -> None:
    shell = shutil.which("sh") or "/bin/sh"
    backend.spawn([shell, "-c", "sleep 30"], size=TerminalSize(80, 24))

    assert backend.wait(timeout=0.01) is None
    assert backend.is_alive()

    backend.close()
    backend.close()

    assert not backend.is_alive()
    assert backend.exit_code is not None


@pytest.mark.parametrize(("columns", "rows"), [(0, 24), (80, 0), (-1, 24), (80, -1)])
def test_resize_rejects_invalid_dimensions(
    backend: UnixPTYBackend, columns: int, rows: int
) -> None:
    shell = shutil.which("sh") or "/bin/sh"
    backend.spawn([shell, "-c", "sleep 30"], size=TerminalSize(80, 24))

    with pytest.raises(ValueError, match="正数"):
        backend.resize(columns, rows)
