from __future__ import annotations

import os
import shutil
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from csbox.core.events import TerminalEvent, TerminalEventType, TerminalSize
from csbox.core.terminal_windows import WindowsConPTYBackend
from csbox.lab.dispatcher import TerminalEventDispatcher
from csbox.lab.proxy import TerminalProxy
from csbox.lab.screen import TerminalEmulator

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


def _read_first_output(backend: WindowsConPTYBackend, *, timeout: float = 10.0) -> bytes:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        chunk = backend.read(timeout=min(0.1, max(0.0, deadline - time.monotonic())))
        if chunk is None:
            continue
        if chunk == b"":
            break
        if chunk:
            return chunk
    pytest.fail("timed out waiting for the interactive Windows Shell startup output")


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


class _NativeScriptedInput:
    def __init__(self, chunks: list[bytes | None], notices: list[TerminalSize]) -> None:
        self.chunks = list(chunks)
        self.notices = list(notices)

    def read(self, max_bytes: int = 4096, timeout: float = 0.05) -> bytes | None:
        del max_bytes, timeout
        return self.chunks.pop(0) if self.chunks else b""

    def drain_resize_notices(self) -> tuple[TerminalSize, ...]:
        notices = tuple(self.notices)
        self.notices.clear()
        return notices


class _NativeMemoryOutput:
    def __init__(self) -> None:
        self.data = bytearray()

    def write(self, data: bytes) -> int:
        self.data.extend(data)
        return len(data)


class _NativeTerminalState:
    def __enter__(self) -> _NativeTerminalState:
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        del exc_type, exc, traceback


class _NativeEventSink:
    def __init__(self, events: list[TerminalEvent]) -> None:
        self.events = events

    def handle(self, event: TerminalEvent) -> None:
        self.events.append(event)


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
        [resolved, "-NoLogo", "-NoProfile"],
        cwd=tmp_path,
        size=TerminalSize(80, 24),
    )
    backend.resize(100, 30)
    output = _read_first_output(backend)
    backend.write(
        (
            ""
            "$size = $Host.UI.RawUI.WindowSize; "
            "Write-Output ('CSBOX_SIZE_{0}x{1}' -f $size.Width, $size.Height); "
            "Write-Output 'CSBOX_NATIVE_中文_OK'; Write-Output (Get-Location); exit 0\r\n"
        ).encode()
    )

    output += _read_until(backend, b"CSBOX_SIZE_100x30")
    output += _read_to_eof(backend)

    assert b"CSBOX_SIZE_100x30" in output
    assert "CSBOX_NATIVE_中文_OK".encode() in output
    assert str(tmp_path).encode() in output
    assert backend.wait(timeout=2.0) == 0


def test_native_windows_proxy_orders_resize_and_consumes_both_capture_bindings(
    backend: WindowsConPTYBackend,
    shell_kind: str | None,
    tmp_path: Path,
) -> None:
    if os.name != "nt":
        pytest.skip("Windows 原生 ConPTY proxy smoke 只在 Windows CI 执行。")
    if shell_kind is None:
        pytest.skip("请使用 --shell-kind powershell 或 --shell-kind pwsh 执行原生 Shell smoke。")

    executable = "powershell.exe" if shell_kind == "powershell" else "pwsh.exe"
    resolved = shutil.which(executable)
    if resolved is None:
        message = f"CI limitation: {executable} is unavailable on this Windows runner."
        if os.environ.get("CSBOX_REQUIRE_NATIVE_SHELL") == "1":
            pytest.fail(message)
        pytest.skip(message)

    command = (
        "Write-Output 'CSBOX_PROXY_中文_OK'; "
        "$size = $Host.UI.RawUI.WindowSize; "
        "Write-Output ('CSBOX_PROXY_SIZE_{0}x{1}' -f $size.Width, $size.Height); exit 0\r\n"
    ).encode()
    input_adapter = _NativeScriptedInput(
        [None, b"\x1b[24~\x00", command, b""],
        [TerminalSize(100, 30)],
    )
    output = _NativeMemoryOutput()
    events: list[TerminalEvent] = []
    captures: list[object] = []
    proxy = TerminalProxy(
        backend,
        command=(resolved, "-NoLogo", "-NoProfile"),
        input_adapter=input_adapter,
        output_adapter=output,
        terminal_state_factory=_NativeTerminalState,
        dispatcher=TerminalEventDispatcher([_NativeEventSink(events)]),
        emulator=TerminalEmulator(columns=80, rows=24),
        size=TerminalSize(80, 24),
        capture_handler=lambda snapshot, timestamp: (
            captures.append((snapshot, timestamp)) or "capture"
        ),
    )

    assert proxy.run() == 0

    output_bytes = bytes(output.data)
    assert b"CSBOX_PROXY_\xe4\xb8\xad\xe6\x96\x87_OK" in output_bytes
    assert b"CSBOX_PROXY_SIZE_100x30" in output_bytes
    assert len(captures) == 2
    assert not [
        event
        for event in events
        if event.type is TerminalEventType.INPUT
        and (b"\x1b[24~" in event.payload or b"\x00" in event.payload)
    ]
    resize_events = [event for event in events if event.type is TerminalEventType.RESIZE]
    assert [event.payload for event in resize_events] == [TerminalSize(100, 30)]
    assert events[-1].type is TerminalEventType.EXIT
