from __future__ import annotations

import ntpath
import os
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from csbox.core.events import TerminalEvent, TerminalEventType, TerminalSize
from csbox.core.shell import ShellUnavailableError, select_shell
from csbox.core.terminal import TerminalBackendError
from csbox.core.terminal_windows import WindowsConPTYBackend
from csbox.lab.dispatcher import TerminalEventDispatcher
from csbox.lab.models import SessionPaths
from csbox.lab.proxy import TerminalProxy
from csbox.lab.screen import TerminalEmulator
from csbox.lab.surface import AlternateScreenSurface
from csbox.lab.windows_host import LaunchIntentStore

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


def _run_process_after_output(
    argv: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    target: bytes,
    input_data: bytes,
    timeout: float = 20.0,
) -> tuple[int, bytes, bytes]:
    process = subprocess.Popen(
        argv,
        cwd=cwd,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    stdout = bytearray()
    stderr = bytearray()
    target_seen = threading.Event()
    reader_errors: list[BaseException] = []

    def collect_stdout() -> None:
        try:
            assert process.stdout is not None
            while chunk := process.stdout.read(1):
                stdout.extend(chunk)
                if target in stdout:
                    target_seen.set()
        except BaseException as exc:
            reader_errors.append(exc)
            target_seen.set()

    def collect_stderr() -> None:
        try:
            assert process.stderr is not None
            stderr.extend(process.stderr.read())
        except BaseException as exc:
            reader_errors.append(exc)

    stdout_reader = threading.Thread(target=collect_stdout, daemon=True)
    stderr_reader = threading.Thread(target=collect_stderr, daemon=True)
    stdout_reader.start()
    stderr_reader.start()
    try:
        if not target_seen.wait(timeout=10.0) or reader_errors:
            process.kill()
            process.wait(timeout=5.0)
            stdout_reader.join(timeout=5.0)
            stderr_reader.join(timeout=5.0)
            pytest.fail(
                f"dedicated host did not emit {target!r}; "
                f"stdout={bytes(stdout[-500:])!r}; stderr={bytes(stderr[-500:])!r}"
            )
        assert process.stdin is not None
        process.stdin.write(input_data)
        process.stdin.flush()
        process.stdin.close()
        return_code = process.wait(timeout=timeout)
    except BaseException:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5.0)
        raise
    finally:
        stdout_reader.join(timeout=5.0)
        stderr_reader.join(timeout=5.0)
        if process.stdout is not None:
            process.stdout.close()
        if process.stderr is not None:
            process.stderr.close()
        if process.stdin is not None and not process.stdin.closed:
            process.stdin.close()
    if reader_errors:
        raise reader_errors[0]
    return return_code, bytes(stdout), bytes(stderr)


def _without_windowsapps(path: str, local_app_data: str) -> str:
    alias_root = ntpath.normcase(
        ntpath.normpath(ntpath.join(local_app_data, "Microsoft", "WindowsApps"))
    )
    return os.pathsep.join(
        entry
        for entry in path.split(os.pathsep)
        if (
            ntpath.normcase(ntpath.normpath(entry)) != alias_root
            and not ntpath.normcase(ntpath.normpath(entry)).startswith(alias_root + ntpath.sep)
        )
    )


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


class _NativeExitRaceInput:
    def __init__(self, backend: WindowsConPTYBackend, command: bytes) -> None:
        self.backend = backend
        self.command = command
        self.phase = 0
        self.closed = False

    def read(self, max_bytes: int = 4096, timeout: float = 0.05) -> bytes | None:
        del max_bytes, timeout
        if self.phase == 0:
            self.phase = 1
            return self.command
        if self.phase == 1:
            deadline = time.monotonic() + 10.0
            while time.monotonic() < deadline:
                if not self.backend.is_alive():
                    self.phase = 2
                    return b"late-input"
                time.sleep(0.005)
            pytest.fail("native PowerShell did not exit before the pending-input race deadline")
        return None

    def close(self) -> None:
        self.closed = True


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


@pytest.mark.parametrize("path_mode", ["normal", "absent", "present", "prepended"])
def test_native_windows_dedicated_host_resolves_ps7_runs_command_and_exits_zero(
    shell_kind: str | None,
    tmp_path: Path,
    path_mode: str,
) -> None:
    if os.name != "nt":
        pytest.skip("Windows dedicated-host core regression 只在 Windows CI 执行。")
    if shell_kind != "pwsh":
        pytest.skip("请使用 --shell-kind pwsh 执行 dedicated-host PS7 core regression。")

    child_env = os.environ.copy()
    local_app_data = child_env.get("LOCALAPPDATA", "")
    original_path = child_env.get("PATH", "")
    try:
        native_profile = select_shell(
            "pwsh",
            None,
            system="Windows",
            environ=child_env,
        )
    except ShellUnavailableError as exc:
        message = "CI limitation: a native PowerShell 7 executable is unavailable."
        if os.environ.get("CSBOX_REQUIRE_NATIVE_SHELL") == "1":
            pytest.fail(f"{message} {exc}")
        pytest.skip(message)

    base_path = _without_windowsapps(original_path, local_app_data)
    native_directory = str(Path(native_profile.executable).parent)
    base_directories = {
        ntpath.normcase(ntpath.normpath(entry)) for entry in base_path.split(os.pathsep) if entry
    }
    if ntpath.normcase(ntpath.normpath(native_directory)) not in base_directories:
        base_path = os.pathsep.join(entry for entry in (native_directory, base_path) if entry)

    alias_root = Path(local_app_data) / "Microsoft" / "WindowsApps"
    alias = alias_root / "pwsh.exe"
    if not alias.is_file():
        local_app_data_path = tmp_path / "S.F. Wang" / "AppData" / "Local"
        alias_root = local_app_data_path / "Microsoft" / "WindowsApps"
        alias_root.mkdir(parents=True)
        alias = alias_root / "pwsh.exe"
        alias.write_bytes(b"not-a-native-executable")
        child_env["LOCALAPPDATA"] = str(local_app_data_path)

    if path_mode == "normal":
        selected_path = original_path
    elif path_mode == "absent":
        selected_path = base_path
    elif path_mode == "present":
        selected_path = base_path + os.pathsep + str(alias_root)
    else:
        selected_path = str(alias_root) + os.pathsep + base_path
    child_env["PATH"] = selected_path

    profile = select_shell(
        "pwsh",
        None,
        system="Windows",
        environ=child_env,
    )
    assert Path(profile.executable) == Path(native_profile.executable)
    assert not str(profile.executable).casefold().startswith(str(alias_root).casefold())

    cwd = tmp_path / f"{path_mode}-S.F. Wang" / "中文 课程"
    cwd.mkdir(parents=True)
    store = LaunchIntentStore(tmp_path / f"launches-{path_mode}")
    command = (profile.executable, "-NoLogo", "-NoProfile")
    intent = store.reserve(
        name=f"native-ps7-{path_mode}",
        shell=profile.kind.value,
        command=command,
        cwd=cwd,
        size=TerminalSize(80, 24),
    )
    loaded = LaunchIntentStore.load(intent.request_path)
    assert loaded.command == command
    assert loaded.cwd == cwd

    marker = f"CSBOX_DEDICATED_HOST_{path_mode.upper()}_HELLO"
    marker_prefix = f"CSBOX_DEDICATED_HOST_{path_mode.upper()}_"
    input_data = (
        f"$prefix = '{marker_prefix}'; $suffix = 'HELLO'; "
        "Write-Output ($prefix + $suffix); exit 0\r\n"
    ).encode()
    assert marker.encode() not in input_data
    return_code, stdout, stderr = _run_process_after_output(
        [
            sys.executable,
            "-m",
            "csbox",
            "lab",
            "_host",
            "--intent",
            str(intent.request_path),
            "--token",
            intent.token,
        ],
        cwd=cwd,
        env=child_env,
        target=b"PS ",
        input_data=input_data,
    )

    assert return_code == 0, stderr.decode("utf-8", errors="replace")
    assert marker.encode() in stdout
    assert b"PS " in stdout
    status = store.load_status(intent)
    assert status.state == "finished"
    assert status.session_status == "completed"
    assert status.exit_code == 0
    assert status.session_id is not None
    session = SessionPaths(cwd / ".csbox" / "sessions" / status.session_id)
    assert marker.encode() in session.cast.read_bytes()


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
    for size in (
        TerminalSize(100, 30),
        TerminalSize(120, 35),
        TerminalSize(160, 45),
    ):
        backend.resize(size.columns, size.rows)
    output = _read_first_output(backend)
    backend.write(
        (
            ""
            "$size = $Host.UI.RawUI.WindowSize; "
            "Write-Output ('CSBOX_SIZE_{0}x{1}' -f $size.Width, $size.Height); "
            "Write-Output 'CSBOX_NATIVE_中文_OK'; "
            "1..20 | ForEach-Object { Write-Output ('CSBOX_FLOW_{0}' -f $_) }; "
            "Write-Output (Get-Location); exit 0\r\n"
        ).encode()
    )

    output += _read_until(backend, b"CSBOX_SIZE_160x45")
    output += _read_to_eof(backend)

    assert b"CSBOX_SIZE_160x45" in output
    assert "CSBOX_NATIVE_中文_OK".encode() in output
    assert b"CSBOX_FLOW_20" in output
    assert str(tmp_path).encode() in output
    assert backend.wait(timeout=2.0) == 0


@pytest.mark.parametrize("exit_status", [0, 7])
def test_native_windows_proxy_preserves_exit_when_pending_input_loses_race(
    backend: WindowsConPTYBackend,
    shell_kind: str | None,
    tmp_path: Path,
    exit_status: int,
) -> None:
    if os.name != "nt":
        pytest.skip("Windows native ConPTY exit-race regression only runs on Windows.")
    if shell_kind != "pwsh":
        pytest.skip("Use --shell-kind pwsh for the native PowerShell 7 exit-race regression.")

    resolved = shutil.which("pwsh.exe")
    if resolved is None:
        message = "CI limitation: pwsh.exe is unavailable on this Windows runner."
        if os.environ.get("CSBOX_REQUIRE_NATIVE_SHELL") == "1":
            pytest.fail(message)
        pytest.skip(message)

    marker = f"CSBOX_NATIVE_EXIT_RACE_{exit_status}"
    command = f"Write-Output '{marker}'; exit {exit_status}\r\n".encode()
    input_adapter = _NativeExitRaceInput(backend, command)
    output = _NativeMemoryOutput()
    events: list[TerminalEvent] = []
    proxy = TerminalProxy(
        backend,
        command=(resolved, "-NoLogo", "-NoProfile"),
        input_adapter=input_adapter,
        output_adapter=output,
        terminal_state_factory=_NativeTerminalState,
        dispatcher=TerminalEventDispatcher([_NativeEventSink(events)]),
        emulator=TerminalEmulator(columns=80, rows=24),
        size=TerminalSize(80, 24),
        cwd=tmp_path,
    )

    assert proxy.run() == exit_status
    assert input_adapter.closed is True
    assert marker.encode() in output.data
    assert events[-1].type is TerminalEventType.EXIT
    assert events[-1].payload == exit_status
    input_payloads = [event.payload for event in events if event.type is TerminalEventType.INPUT]
    assert input_payloads == [command]


def test_native_windows_bad_exe_format_is_a_shell_executable_failure(
    shell_kind: str | None,
    tmp_path: Path,
) -> None:
    if os.name != "nt":
        pytest.skip("Windows native error classification 只在 Windows CI 执行。")
    if shell_kind != "pwsh":
        pytest.skip("请使用 --shell-kind pwsh 执行 native error classification。")

    invalid = tmp_path / "S.F. Wang" / "invalid pwsh.exe"
    invalid.parent.mkdir(parents=True)
    invalid.write_bytes(b"not-a-native-executable")
    backend = WindowsConPTYBackend()

    with pytest.raises(TerminalBackendError) as caught:
        backend.spawn([str(invalid), "-NoLogo"], cwd=tmp_path)

    assert caught.value.kind == "shell_executable_unlaunchable"
    assert f"resolved_executable={invalid}" in caught.value.debug_context
    assert set(caught.value.debug_context) & {"native_code=193", "native_code=216"}


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
        terminal_surface_factory=lambda: AlternateScreenSurface(output),
    )

    assert proxy.run() == 0

    output_bytes = bytes(output.data)
    assert output_bytes.startswith(b"\x1b[?1049h")
    assert output_bytes.endswith(b"\x1b[?1049l")
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
