from __future__ import annotations

import os
import select
import signal
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Protocol

from csbox.core.events import TerminalEvent, TerminalEventClock, TerminalEventType, TerminalSize
from csbox.core.terminal import TerminalBackend, TerminalBackendError, TerminalProcessExited
from csbox.lab.dispatcher import DispatchError, TerminalEventDispatcher
from csbox.lab.keymap import CaptureKeyMatcher, CaptureMatch
from csbox.lab.recorder import RecorderError
from csbox.lab.screen import TerminalEmulator, TerminalSnapshot
from csbox.lab.windows_input import WindowsConsoleInputOwner


class InputAdapter(Protocol):
    def read(self, max_bytes: int = 4096, timeout: float = 0.05) -> bytes | None: ...


class OutputAdapter(Protocol):
    def write(self, data: bytes) -> int: ...


class TerminalCleanupError(RuntimeError):
    """The child finished, but terminal/resource restoration failed."""

    def __init__(self, cause: BaseException, exit_code: int | None) -> None:
        super().__init__("实验运行结束，但终端清理失败。")
        self.cause = cause
        self.exit_code = exit_code


class CaptureHandler(Protocol):
    def __call__(self, snapshot: TerminalSnapshot, timestamp: float) -> object: ...


@dataclass(frozen=True, slots=True)
class TerminalStatus:
    kind: str
    message: str
    relative_time: float
    capture_id: str | None = None


StatusSink = Callable[[TerminalStatus], None]


DEFAULT_PROXY_SIZE = TerminalSize(80, 24)
EXIT_STATUS_WAIT_SECONDS = 0.5


class FileInputAdapter:
    """Read bytes from a terminal/file without blocking output polling."""

    supports_capture = True

    def __init__(
        self,
        stream: object | None = None,
        *,
        windows_owner: WindowsConsoleInputOwner | None = None,
    ) -> None:
        self.stream = stream or sys.stdin.buffer
        self._windows_owner = windows_owner
        self._windows_owner_initialized = windows_owner is not None
        self._windows_owner_error: RuntimeError | None = None

    def read(self, max_bytes: int = 4096, timeout: float = 0.05) -> bytes | None:
        if os.name == "nt":
            if _is_windows_console(self.stream):
                owner = self._ensure_windows_owner()
                if owner is None:
                    raise RuntimeError("authoritative Windows console input owner is unavailable")
                return owner.read(max_bytes, timeout)
            return _read_windows_pipe(self.stream, max_bytes, timeout)
        file_descriptor = self.stream.fileno()  # type: ignore[attr-defined]
        try:
            ready, _, _ = select.select([file_descriptor], [], [], timeout)
        except (OSError, ValueError):
            # A Windows console is not selectable.  The console branch above
            # handles the normal case; keep redirected input non-fatal when a
            # host provides a handle that cannot be polled with select().
            time.sleep(max(0.0, timeout))
            return None
        if not ready:
            return None
        return os.read(file_descriptor, max_bytes)

    def drain_resize_notices(self) -> tuple[TerminalSize, ...]:
        if os.name != "nt" or not _is_windows_console(self.stream):
            return ()
        owner = self._ensure_windows_owner()
        if owner is None:
            raise RuntimeError("authoritative Windows console input owner is unavailable")
        return owner.drain_resize_notices()

    def close(self) -> None:
        owner = self._windows_owner
        if owner is not None:
            owner.close()

    def _ensure_windows_owner(self) -> WindowsConsoleInputOwner | None:
        if self._windows_owner_initialized:
            if self._windows_owner_error is not None:
                raise self._windows_owner_error
            return self._windows_owner
        self._windows_owner_initialized = True
        if os.name != "nt":
            return None
        try:
            file_descriptor = self.stream.fileno()  # type: ignore[attr-defined]
            handle = _windows_console_handle(file_descriptor)
            self._windows_owner = WindowsConsoleInputOwner(handle=handle)
        except (AttributeError, OSError, ValueError, RuntimeError, ImportError) as error:
            self._windows_owner_error = RuntimeError(
                "authoritative Windows console input owner is unavailable"
            )
            raise self._windows_owner_error from error
        return self._windows_owner


class FileOutputAdapter:
    def __init__(self, stream: object | None = None) -> None:
        self.stream = stream or sys.stdout.buffer

    def write(self, data: bytes) -> int:
        written = self.stream.write(data)  # type: ignore[attr-defined]
        self.stream.flush()  # type: ignore[attr-defined]
        return written


class RawTerminalState:
    """Context manager that restores Unix stdin flags after proxying."""

    def __init__(self, stream: object | None = None) -> None:
        self.stream = stream or sys.stdin
        self._fd: int | None = None
        self._attributes: list[object] | None = None
        self._termios: Any | None = None
        self._windows_handle: int | None = None
        self._windows_mode: int | None = None
        self._windows_output_handle: int | None = None
        self._windows_output_mode: int | None = None

    def __enter__(self) -> RawTerminalState:
        if os.name == "nt":
            self._enter_windows_console()
        else:
            try:
                import termios
                import tty

                fd = self.stream.fileno()  # type: ignore[attr-defined]
                if os.isatty(fd):
                    self._fd = fd
                    self._attributes = termios.tcgetattr(fd)
                    self._termios = termios
                    tty.setraw(fd)
            except (AttributeError, OSError, ImportError):
                self._fd = None
                self._attributes = None
                self._termios = None
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        if self._fd is not None and self._attributes is not None:
            self._termios.tcsetattr(self._fd, self._termios.TCSADRAIN, self._attributes)
        if self._windows_handle is not None and self._windows_mode is not None:
            _set_windows_console_mode(self._windows_handle, self._windows_mode)
        if self._windows_output_handle is not None and self._windows_output_mode is not None:
            _set_windows_console_mode(self._windows_output_handle, self._windows_output_mode)

    def _enter_windows_console(self) -> None:
        try:
            file_descriptor = self.stream.fileno()  # type: ignore[attr-defined]
            handle = _windows_console_handle(file_descriptor)
            original_mode = _get_windows_console_mode(handle)
        except (AttributeError, OSError, ValueError, RuntimeError):
            return
        # Own native KEY_EVENT and WINDOW_BUFFER_SIZE_EVENT records while
        # leaving mouse-wheel input with the terminal host.
        new_mode = original_mode & ~(
            _ENABLE_LINE_INPUT
            | _ENABLE_ECHO_INPUT
            | _ENABLE_PROCESSED_INPUT
            | _ENABLE_MOUSE_INPUT
            | _ENABLE_QUICK_EDIT_MODE
            | _ENABLE_VIRTUAL_TERMINAL_INPUT
        )
        new_mode |= _ENABLE_EXTENDED_FLAGS | _ENABLE_WINDOW_INPUT
        try:
            _set_windows_console_mode(handle, new_mode)
        except OSError as error:
            raise RuntimeError("failed to configure Windows console input mode") from error
        self._windows_handle = handle
        self._windows_mode = original_mode
        self._enter_windows_output()

    def _enter_windows_output(self) -> None:
        try:
            file_descriptor = sys.stdout.fileno()
            handle = _windows_console_handle(file_descriptor)
            original_mode = _get_windows_console_mode(handle)
            _set_windows_console_mode(
                handle,
                original_mode | _ENABLE_VIRTUAL_TERMINAL_PROCESSING,
            )
        except (AttributeError, OSError, ValueError, RuntimeError):
            return
        self._windows_output_handle = handle
        self._windows_output_mode = original_mode


def current_terminal_size(fallback: TerminalSize | None = None) -> TerminalSize:
    try:
        size = os.get_terminal_size(sys.stdout.fileno()) if sys.stdout.isatty() else None
    except OSError:
        size = None
    if size is None:
        return fallback or DEFAULT_PROXY_SIZE
    return TerminalSize(columns=size.columns, rows=size.lines)


class TerminalProxy:
    """Bridge a real backend and terminal streams through domain events."""

    def __init__(
        self,
        backend: TerminalBackend,
        *,
        command: Sequence[str],
        input_adapter: InputAdapter | None = None,
        output_adapter: OutputAdapter | None = None,
        terminal_state_factory: Callable[[], object] | None = None,
        dispatcher: TerminalEventDispatcher,
        emulator: TerminalEmulator,
        capture_key: str = "f12",
        capture_handler: CaptureHandler | None = None,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        size: TerminalSize | None = None,
        size_provider: Callable[[], TerminalSize] = current_terminal_size,
        event_clock: TerminalEventClock | None = None,
        started_callback: Callable[[], object] | None = None,
        capture_rollback: Callable[[object], object] | None = None,
        status_sink: StatusSink | None = None,
        host_boundary: Callable[[], object] | None = None,
        terminal_surface_factory: Callable[[], object] | None = None,
    ) -> None:
        self.backend = backend
        self.command = tuple(command)
        self.input_adapter = input_adapter or FileInputAdapter()
        self.output_adapter = output_adapter or FileOutputAdapter()
        self.terminal_state_factory = terminal_state_factory or RawTerminalState
        self.dispatcher = dispatcher
        self.emulator = emulator
        self.matcher = CaptureKeyMatcher(capture_key)
        self.capture_handler = capture_handler
        self.cwd = cwd
        self.env = env
        self.size = size or DEFAULT_PROXY_SIZE
        self.size_provider = size_provider
        self.event_clock = event_clock or TerminalEventClock()
        self.started_callback = started_callback
        self.capture_rollback = capture_rollback
        self.status_sink = status_sink
        self.host_boundary = host_boundary
        self.terminal_surface_factory = terminal_surface_factory
        self.status_events: list[TerminalStatus] = []
        self._resize_pending = False
        self._pending_resize_size: TerminalSize | None = None
        self._pending_resize_generation = 0
        self._resize_generation = 0
        self._committed_resize_generation = 0
        self._confirmed_size = self.size
        self._last_event_relative_time = 0.0
        self._previous_resize_handler: object | None = None
        self._exit_emitted = False
        self._spawned = False
        self._backend_closed = False
        self._backend_close_attempted = False
        self._input_owner_stopped = False
        self._observed_child_exit_code: int | None = None

    def run(self) -> int | None:
        failure: BaseException | None = None
        result: int | None = None
        terminal_state = self.terminal_state_factory()
        terminal_surface = (
            self.terminal_surface_factory() if self.terminal_surface_factory is not None else None
        )
        state_entered = False
        surface_entered = False
        surface_restore_allowed = True
        try:
            terminal_state.__enter__()  # type: ignore[union-attr]
            state_entered = True
            try:
                if terminal_surface is not None:
                    terminal_surface.__enter__()
                    surface_entered = True
                try:
                    self._install_resize_handler()
                    try:
                        if self.host_boundary is not None:
                            self.host_boundary()
                        self.backend.spawn(
                            self.command,
                            cwd=self.cwd,
                            env=self.env,
                            size=self.size,
                        )
                        self._spawned = True
                        self.event_clock.start()
                        if self.started_callback is not None:
                            self.started_callback()
                        result = self._run_loop()
                    except BaseException as exc:
                        failure = exc
                finally:
                    try:
                        if self._spawned and not self._exit_emitted and failure is None:
                            self._emit_exit(self.backend.exit_code)
                    except BaseException as exc:
                        if failure is None:
                            failure = exc
                    self._restore_resize_handler()
                    failure = self._stop_input_owner(failure)
                    if failure is not None and self._spawned:
                        failure = self._close_backend(failure, result)
                        if not self._backend_closed:
                            failure = self._close_backend(failure, result, retry=True)
                            surface_restore_allowed = self._backend_closed
            except BaseException as exc:
                if failure is None:
                    failure = exc
            finally:
                if surface_entered and surface_restore_allowed:
                    try:
                        terminal_surface.__exit__(None, None, None)
                    except BaseException as exc:
                        if failure is None:
                            failure = self._cleanup_error(exc, result)
                if state_entered:
                    try:
                        terminal_state.__exit__(None, None, None)
                    except BaseException as exc:
                        if failure is None:
                            failure = self._cleanup_error(exc, result)
        except BaseException as exc:
            if failure is None:
                failure = exc
        finally:
            if self._spawned and not self._backend_closed and surface_restore_allowed:
                failure = self._close_backend(failure, result)
        if failure is not None:
            raise failure
        return result

    def notify_resize(self, size: TerminalSize | None = None) -> None:
        self._resize_generation += 1
        self._resize_pending = True
        self._pending_resize_size = size
        self._pending_resize_generation = self._resize_generation

    def _run_loop(self) -> int | None:
        input_closed = False
        while True:
            if not input_closed:
                try:
                    input_data = self.input_adapter.read()
                    self._collect_resize_notices()
                    if input_data == b"":
                        self._handle_input_flush()
                        input_closed = True
                    elif input_data is not None:
                        self._handle_input(input_data)
                except TerminalProcessExited as child_exit:
                    self._observed_child_exit_code = child_exit.exit_code
                    cleanup_failure = self._stop_input_owner(None)
                    if cleanup_failure is not None:
                        raise cleanup_failure from child_exit
                    input_closed = True
                    self._resize_pending = False
                    self._pending_resize_size = None

            output = self.backend.read(timeout=0.0 if self._resize_pending else 0.05)
            if output is None:
                if self._resize_pending:
                    self._apply_pending_resize()
                    continue
                else:
                    continue
            if output == b"":
                cleanup_failure = self._stop_input_owner(None)
                if cleanup_failure is not None:
                    raise cleanup_failure
                input_closed = True
                exit_code = self._child_exit_code()
                self._emit_exit(exit_code)
                return exit_code
            if not isinstance(output, bytes):
                raise TypeError("terminal backend read() must return bytes, None, or b''")
            self._write_output_all(output)
            self._emit(self.event_clock.next(TerminalEventType.OUTPUT, output))
            self._refresh_status()

    def _handle_input(self, data: bytes) -> None:
        match = self.matcher.feed(data)
        self._handle_match(match)

    def _handle_input_flush(self) -> None:
        self._handle_match(self.matcher.flush())

    def _handle_match(self, match: CaptureMatch) -> None:
        for kind, value in match.actions:
            if kind == "forward":
                self._write_all(value)
                self._emit(self.event_clock.next(TerminalEventType.INPUT, value))
                continue
            event = self.event_clock.next(TerminalEventType.CAPTURE, "capture")
            snapshot = replace(self.emulator.snapshot(), relative_time=event.relative_time)
            try:
                capture_value = (
                    self.capture_handler(snapshot, event.relative_time)
                    if self.capture_handler
                    else event.payload
                )
            except Exception:
                self._publish_status(
                    TerminalStatus(
                        kind="capture_failed",
                        message="Capture 保存失败；session 继续录制。",
                        relative_time=event.relative_time,
                    )
                )
                continue

            capture_id = _capture_id(capture_value)
            try:
                self._emit(replace(event, payload=capture_id))
            except DispatchError as error:
                self._rollback_capture(capture_value)
                if error.rollback_errors:
                    raise RecorderError(
                        "Capture transaction rollback failed"
                    ) from error.rollback_errors[0]
                if isinstance(error.__cause__, RecorderError):
                    raise
                self._publish_status(
                    TerminalStatus(
                        kind="capture_failed",
                        message="Capture marker 保存失败；session 继续录制。",
                        relative_time=event.relative_time,
                        capture_id=capture_id,
                    )
                )
                continue
            self._publish_status(
                TerminalStatus(
                    kind="capture_succeeded",
                    message="Capture 已保存。",
                    relative_time=event.relative_time,
                    capture_id=capture_id,
                )
            )

    def _rollback_capture(self, value: object) -> None:
        if self.capture_rollback is None:
            return
        self.capture_rollback(value)

    def _publish_status(self, status: TerminalStatus) -> None:
        self.status_events.append(status)
        if self.status_sink is not None:
            with suppress(Exception):
                self.status_sink(status)

    def _refresh_status(self) -> None:
        refresh = getattr(self.status_sink, "refresh", None)
        if callable(refresh):
            with suppress(Exception):
                refresh()

    def _stop_input_owner(self, failure: BaseException | None) -> BaseException | None:
        if self._input_owner_stopped:
            return failure
        self._input_owner_stopped = True
        close = getattr(self.input_adapter, "close", None)
        if not callable(close):
            return failure
        try:
            close()
        except BaseException as exc:
            return failure if failure is not None else self._cleanup_error(exc, None)
        return failure

    def _child_exit_code(self) -> int | None:
        exit_code = self.backend.exit_code
        if exit_code is not None:
            return exit_code
        # A ConPTY output EOF and the process handle's exit-status update can
        # cross, so give the native backend a bounded status wait after output
        # has already been drained.
        exit_code = self.backend.wait(EXIT_STATUS_WAIT_SECONDS)
        if exit_code is not None:
            return exit_code
        exit_code = self.backend.exit_code
        if exit_code is not None:
            return exit_code
        if self.backend.is_alive():
            cause = RuntimeError(
                "terminal output reached EOF while the child process was still alive"
            )
            raise TerminalBackendError(
                "终端输出已结束，但子进程仍在运行，无法确认正常退出。", cause
            ) from cause
        return self._observed_child_exit_code

    def _close_backend(
        self,
        failure: BaseException | None,
        result: int | None,
        *,
        retry: bool = False,
    ) -> BaseException | None:
        if self._backend_closed or (self._backend_close_attempted and not retry):
            return failure
        self._backend_close_attempted = True
        try:
            self.backend.close()
            self._backend_closed = True
        except BaseException as exc:
            if failure is None:
                return self._cleanup_error(exc, result)
        return failure

    def _cleanup_error(self, cause: BaseException, result: int | None) -> TerminalCleanupError:
        exit_code = result
        if exit_code is None:
            with suppress(BaseException):
                exit_code = self.backend.exit_code
        if exit_code is None:
            exit_code = self._observed_child_exit_code
        return TerminalCleanupError(cause, exit_code)

    def _write_all(self, data: bytes) -> None:
        offset = 0
        while offset < len(data):
            written = self.backend.write(data[offset:])
            if type(written) is not int or written <= 0:
                raise BrokenPipeError("终端未能接受完整输入。")
            offset += written

    def _write_output_all(self, data: bytes) -> None:
        offset = 0
        while offset < len(data):
            written = self.output_adapter.write(data[offset:])
            if type(written) is not int or written <= 0:
                raise BrokenPipeError("本地终端未能接收完整输出。")
            offset += written

    def _emit_exit(self, exit_code: int | None) -> None:
        self._emit(self.event_clock.next(TerminalEventType.EXIT, exit_code))
        self._exit_emitted = True

    def _emit(self, event: TerminalEvent) -> None:
        self.dispatcher.dispatch(event)
        self.emulator.apply(event)
        self._last_event_relative_time = event.relative_time

    def _apply_pending_resize(self) -> None:
        if not self._resize_pending:
            return
        generation = self._pending_resize_generation
        requested_size = self._pending_resize_size
        self._resize_pending = False
        self._pending_resize_size = None
        try:
            size = requested_size or self.size_provider()
            if size == self._confirmed_size:
                self._committed_resize_generation = generation
                return
            self.backend.resize(size.columns, size.rows)
        except Exception as error:
            self._publish_status(
                TerminalStatus(
                    kind="resize_failed",
                    message=(
                        f"Resize 未确认；保留终端尺寸 "
                        f"{self._confirmed_size.columns}x{self._confirmed_size.rows}。{error}"
                    ),
                    relative_time=self._last_event_relative_time,
                )
            )
            return
        self._confirmed_size = size
        self.size = size
        self._committed_resize_generation = generation
        self._emit(self.event_clock.next(TerminalEventType.RESIZE, size))

    def _collect_resize_notices(self) -> None:
        drain = getattr(self.input_adapter, "drain_resize_notices", None)
        if not callable(drain):
            return
        for size in drain():
            if not isinstance(size, TerminalSize):
                raise TypeError("input adapter resize notices must contain TerminalSize values")
            self.notify_resize(size)

    def _install_resize_handler(self) -> None:
        if os.name == "nt" or not hasattr(signal, "SIGWINCH"):
            return
        try:
            self._previous_resize_handler = signal.getsignal(signal.SIGWINCH)
            signal.signal(signal.SIGWINCH, self._handle_sigwinch)
        except (OSError, ValueError):
            self._previous_resize_handler = None

    def _restore_resize_handler(self) -> None:
        if self._previous_resize_handler is None:
            return
        with suppress(OSError, ValueError):
            signal.signal(signal.SIGWINCH, self._previous_resize_handler)  # type: ignore[arg-type]
        self._previous_resize_handler = None

    def _handle_sigwinch(self, signum: int, frame: object) -> None:
        del signum, frame
        self.notify_resize()


def _capture_id(value: object) -> str:
    if value is None:
        return "capture"
    identifier = getattr(value, "capture_id", None)
    return str(identifier if identifier is not None else value)


_ENABLE_PROCESSED_INPUT = 0x0001
_ENABLE_LINE_INPUT = 0x0002
_ENABLE_ECHO_INPUT = 0x0004
_ENABLE_WINDOW_INPUT = 0x0008
_ENABLE_MOUSE_INPUT = 0x0010
_ENABLE_QUICK_EDIT_MODE = 0x0040
_ENABLE_EXTENDED_FLAGS = 0x0080
_ENABLE_VIRTUAL_TERMINAL_INPUT = 0x0200
_ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004


def _is_windows_console(stream: object) -> bool:
    if os.name != "nt":
        return False
    try:
        return bool(stream.isatty())  # type: ignore[attr-defined]
    except (AttributeError, OSError, ValueError):
        return False


_WINDOWS_SCAN_CODES = {
    "\x00H": b"\x1b[A",
    "\x00P": b"\x1b[B",
    "\x00M": b"\x1b[C",
    "\x00K": b"\x1b[D",
    "\x00G": b"\x1b[H",
    "\x00O": b"\x1b[F",
    "\x00S": b"\x1b[3~",
    "\x00R": b"\x1b[2~",
    "\x00;": b"\x1bOP",
    "\x00<": b"\x1bOQ",
    "\x00=": b"\x1bOR",
    "\x00>": b"\x1bOS",
    "\x00?": b"\x1b[15~",
    "\x00@": b"\x1b[17~",
    "\x00A": b"\x1b[18~",
    "\x00B": b"\x1b[19~",
    "\x00C": b"\x1b[20~",
    "\x00D": b"\x1b[21~",
    "\x00E": b"\x1b[23~",
    "\x00F": b"\x1b[24~",
    "\x00\x86": b"\x1b[24~",
}


def _encode_windows_input(value: str) -> bytes:
    encoded = bytearray()
    index = 0
    while index < len(value):
        scan_code = value[index : index + 2]
        if scan_code in _WINDOWS_SCAN_CODES:
            encoded.extend(_WINDOWS_SCAN_CODES[scan_code])
            index += 2
            continue
        encoded.extend(value[index].encode("utf-8", errors="surrogatepass"))
        index += 1
    return bytes(encoded)


def _read_windows_pipe(stream: object, max_bytes: int, timeout: float) -> bytes | None:
    """Poll a redirected Windows stdin pipe without blocking the PTY loop."""

    try:
        import ctypes
        import msvcrt
        from ctypes import wintypes

        handle = msvcrt.get_osfhandle(stream.fileno())  # type: ignore[attr-defined]
        available = wintypes.DWORD()
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.PeekNamedPipe.argtypes = [
            wintypes.HANDLE,
            wintypes.LPVOID,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
            ctypes.POINTER(wintypes.DWORD),
            ctypes.POINTER(wintypes.DWORD),
        ]
        kernel32.PeekNamedPipe.restype = wintypes.BOOL
        kernel32.GetFileType.argtypes = [wintypes.HANDLE]
        kernel32.GetFileType.restype = wintypes.DWORD
        if not kernel32.PeekNamedPipe(
            handle,
            None,
            0,
            None,
            ctypes.byref(available),
            None,
        ):
            if kernel32.GetFileType(handle) == 1:  # FILE_TYPE_DISK.
                return os.read(stream.fileno(), max_bytes)  # type: ignore[attr-defined]
            time.sleep(max(0.0, timeout))
            return None
        if not available.value:
            time.sleep(max(0.0, timeout))
            return None
        return os.read(stream.fileno(), min(max_bytes, available.value))  # type: ignore[attr-defined]
    except (AttributeError, OSError, ValueError):
        try:
            file_descriptor = stream.fileno()  # type: ignore[attr-defined]
            file_type = kernel32.GetFileType(handle)
        except (AttributeError, OSError, UnboundLocalError):
            time.sleep(max(0.0, timeout))
            return None
        if file_type == 1:  # FILE_TYPE_DISK: regular redirected stdin.
            return os.read(file_descriptor, max_bytes)
        time.sleep(max(0.0, timeout))
        return None


def _windows_console_handle(file_descriptor: int) -> int:
    import msvcrt

    handle = int(msvcrt.get_osfhandle(file_descriptor))
    if handle == -1:
        raise OSError("invalid Windows console handle")
    return handle


def _get_windows_console_mode(handle: int) -> int:
    import ctypes
    from ctypes import wintypes

    mode = ctypes.c_uint32()
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetConsoleMode.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.GetConsoleMode.restype = wintypes.BOOL
    if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
        raise RuntimeError("input handle is not a Windows console")
    return int(mode.value)


def _set_windows_console_mode(handle: int, mode: int) -> None:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.SetConsoleMode.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.SetConsoleMode.restype = wintypes.BOOL
    if not kernel32.SetConsoleMode(handle, mode):
        raise OSError("could not set Windows console mode")
