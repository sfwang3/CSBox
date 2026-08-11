from __future__ import annotations

import os
import shutil
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from csbox.core.events import TerminalSize
from csbox.core.terminal_windows import WindowsConPTYBackend

pytestmark = [pytest.mark.pty, pytest.mark.windows, pytest.mark.integration]


@pytest.fixture
def backend() -> Iterator[WindowsConPTYBackend]:
    instance = WindowsConPTYBackend()
    try:
        yield instance
    finally:
        instance.close()


def _read_until(
    backend: WindowsConPTYBackend,
    target: bytes,
    *,
    timeout: float = 10.0,
) -> bytes:
    deadline = time.monotonic() + timeout
    output = bytearray()
    while time.monotonic() < deadline:
        chunk = backend.read(timeout=min(0.1, max(0.0, deadline - time.monotonic())))
        if chunk is None:
            continue
        if chunk == b"":
            break
        output.extend(chunk)
        if target in output:
            return bytes(output)
    pytest.fail(f"timed out waiting for {target!r}; output={bytes(output[-500:])!r}")


def _read_to_eof(backend: WindowsConPTYBackend, *, timeout: float = 10.0) -> bytes:
    deadline = time.monotonic() + timeout
    output = bytearray()
    while time.monotonic() < deadline:
        chunk = backend.read(timeout=min(0.1, max(0.0, deadline - time.monotonic())))
        if chunk is None:
            continue
        if chunk == b"":
            return bytes(output)
        output.extend(chunk)
    pytest.fail(f"timed out waiting for Windows ConPTY EOF; output={bytes(output[-500:])!r}")


def test_native_windows_conpty_shell_unicode_resize_and_exit(
    backend: WindowsConPTYBackend,
    shell_kind: str | None,
    tmp_path: Path,
) -> None:
    if os.name != "nt":
        pytest.skip("Windows 原生 ConPTY smoke 只在 Windows CI 执行。")
    if shell_kind is None:
        pytest.skip("请使用 --shell-kind powershell 或 --shell-kind pwsh 执行原生 Shell smoke。")

    executable = "powershell.exe" if shell_kind == "powershell" else "pwsh.exe"
    resolved = shutil.which(executable)
    if resolved is None:
        message = f"CI limitation: {executable} is unavailable on this Windows runner."
        if os.environ.get("CSBOX_REQUIRE_NATIVE_SHELL") == "1":
            pytest.fail(message)
        pytest.skip(message)

    backend.spawn(
        [resolved, "-NoLogo", "-NoProfile", "-NonInteractive"],
        cwd=tmp_path,
        size=TerminalSize(80, 24),
    )
    backend.resize(100, 30)
    backend.write(
        (
            ""
            "$size = $Host.UI.RawUI.WindowSize; "
            "Write-Output ('CSBOX_SIZE_{0}x{1}' -f $size.Width, $size.Height); "
            "Write-Output 'CSBOX_NATIVE_中文_OK'; Write-Output (Get-Location); exit 0\r\n"
        ).encode()
    )

    output = _read_until(backend, b"CSBOX_SIZE_100x30")
    output += _read_to_eof(backend)

    assert b"CSBOX_SIZE_100x30" in output
    assert "CSBOX_NATIVE_中文_OK".encode() in output
    assert str(tmp_path).encode() in output
    assert backend.wait(timeout=2.0) == 0
