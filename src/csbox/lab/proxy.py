from __future__ import annotations

import os
import select
import signal
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import replace
from pathlib import Path
from typing import Any, Protocol

from csbox.core.events import TerminalEvent, TerminalEventClock, TerminalEventType, TerminalSize
from csbox.core.terminal import TerminalBackend
from csbox.lab.dispatcher import TerminalEventDispatcher
from csbox.lab.keymap import CaptureKeyMatcher, CaptureMatch
from csbox.lab.screen import TerminalEmulator, TerminalSnapshot


class InputAdapter(Protocol):
    def read(self, max_bytes: int = 4096, timeout: float = 0.05) -> bytes | None: ...


class OutputAdapter(Protocol):
    def write(self, data: bytes) -> int: ...


class CaptureHandler(Protocol):
    def __call__(self, snapshot: TerminalSnapshot, timestamp: float) -> object: ...


DEFAULT_PROXY_SIZE = TerminalSize(80, 24)


class FileInputAdapter:
    """Read bytes from a terminal/file without blocking output polling."""

    supports_capture = True

    def __init__(self, stream: object | None = None) -> None:
        self.stream = stream or sys.stdin.buffer

    def read(self, max_bytes: int = 4096, timeout: float = 0.05) -> bytes | None:
        if os.name == "nt":
            if _is_windows_console(self.stream):
                return _read_windows_console(max_bytes, timeout)
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

    def _enter_windows_console(self) -> None:
        try:
            file_descriptor = self.stream.fileno()  # type: ignore[attr-defined]
            handle = _windows_console_handle(file_descriptor)
            original_mode = _get_windows_console_mode(handle)
        except (AttributeError, OSError, ValueError, RuntimeError):
            return
        # Read individual keypresses and let the application receive Ctrl+C as
        # a byte.  Virtual-terminal input also preserves ANSI key sequences in
        # hosts that expose them.
        new_mode = original_mode & ~(
            _ENABLE_LINE_INPUT
            | _ENABLE_ECHO_INPUT
            | _ENABLE_PROCESSED_INPUT
            | _ENABLE_QUICK_EDIT_MODE
        )
        new_mode |= _ENABLE_EXTENDED_FLAGS | _ENABLE_VIRTUAL_TERMINAL_INPUT
        try:
            _set_windows_console_mode(handle, new_mode)
        except OSError:
            try:
                _set_windows_console_mode(
                    handle,
                    new_mode & ~_ENABLE_VIRTUAL_TERMINAL_INPUT,
                )
            except OSError:
                return
        self._windows_handle = handle
        self._windows_mode = original_mode


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
        self._resize_pending = False
        self._previous_resize_handler: object | None = None
        self._exit_emitted = False

    def run(self) -> int | None:
        failure: BaseException | None = None
        result: int | None = None
        terminal_state = self.terminal_state_factory()
        try:
            with terminal_state:  # type: ignore[union-attr]
                self._install_resize_handler()
                try:
                    self.backend.spawn(
                        self.command,
                        cwd=self.cwd,
                        env=self.env,
                        size=self.size,
                    )
                    result = self._run_loop()
                except BaseException as exc:
                    failure = exc
                finally:
                    try:
                        if not self._exit_emitted:
                            self._emit_exit(None if failure is not None else self.backend.exit_code)
                    except BaseException as exc:
                        if failure is None:
                            failure = exc
                    self._restore_resize_handler()
                    try:
                        self.backend.close()
                    except BaseException as exc:
                        if failure is None:
                            failure = exc
        except BaseException as exc:
            if failure is None:
                failure = exc
        if failure is not None:
            raise failure
        return result

    def notify_resize(self) -> None:
        self._resize_pending = True

    def _run_loop(self) -> int | None:
        input_closed = False
        while True:
            self._apply_pending_resize()
            if not input_closed:
                input_data = self.input_adapter.read()
                if input_data == b"":
                    self._handle_input_flush()
                    input_closed = True
                elif input_data is not None:
                    self._handle_input(input_data)

            output = self.backend.read()
            if output is None:
                if input_closed and not self.backend.is_alive():
                    output = b""
                else:
                    continue
            if output == b"":
                exit_code = self.backend.exit_code
                if exit_code is None:
                    exit_code = self.backend.wait(0.0)
                self._emit_exit(exit_code)
                return exit_code
            if not isinstance(output, bytes):
                raise TypeError("terminal backend read() must return bytes, None, or b''")
            self._write_output_all(output)
            self._emit(self.event_clock.next(TerminalEventType.OUTPUT, output))

    def _handle_input(self, data: bytes) -> None:
        match = self.matcher.feed(data)
        self._handle_match(match)

    def _handle_input_flush(self) -> None:
        self._handle_match(self.matcher.flush())

    def _handle_match(self, match: CaptureMatch) -> None:
        for kind, value in match.actions:
            if kind == "forward":
                self._emit(self.event_clock.next(TerminalEventType.INPUT, value))
                self._write_all(value)
                continue
            event = self.event_clock.next(TerminalEventType.CAPTURE, "capture")
            snapshot = replace(self.emulator.snapshot(), relative_time=event.relative_time)
            capture_id = (
                self.capture_handler(snapshot, event.relative_time)
                if self.capture_handler
                else event.payload
            )
            self._emit(replace(event, payload=_capture_id(capture_id)))

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
        self.emulator.apply(event)
        self.dispatcher.dispatch(event)

    def _apply_pending_resize(self) -> None:
        if not self._resize_pending:
            return
        self._resize_pending = False
        size = self.size_provider()
        self.backend.resize(size.columns, size.rows)
        self._emit(self.event_clock.next(TerminalEventType.RESIZE, size))

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
        self._resize_pending = True


def _capture_id(value: object) -> str:
    if value is None:
        return "capture"
    identifier = getattr(value, "capture_id", None)
    return str(identifier if identifier is not None else value)


_ENABLE_PROCESSED_INPUT = 0x0001
_ENABLE_LINE_INPUT = 0x0002
_ENABLE_ECHO_INPUT = 0x0004
_ENABLE_QUICK_EDIT_MODE = 0x0040
_ENABLE_EXTENDED_FLAGS = 0x0080
_ENABLE_VIRTUAL_TERMINAL_INPUT = 0x0200


def _is_windows_console(stream: object) -> bool:
    if os.name != "nt":
        return False
    try:
        return bool(stream.isatty())  # type: ignore[attr-defined]
    except (AttributeError, OSError, ValueError):
        return False


def _read_windows_console(max_bytes: int, timeout: float) -> bytes | None:
    import msvcrt

    deadline = time.monotonic() + max(0.0, timeout)
    while not msvcrt.kbhit():
        if time.monotonic() >= deadline:
            return None
        time.sleep(min(0.01, max(0.0, deadline - time.monotonic())))
    characters: list[str] = []
    while msvcrt.kbhit() and len("".join(characters).encode("utf-8")) < max_bytes:
        character = msvcrt.getwch()
        if character in {"\x00", "\xe0"} and msvcrt.kbhit():
            character += msvcrt.getwch()
        characters.append(character)
    value = "".join(characters)
    # When the host does not expose virtual-terminal input, msvcrt returns a
    # scan-code pair for function keys.  F12 is the default Capture binding.
    return _encode_windows_input(value)


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
        if not ctypes.windll.kernel32.PeekNamedPipe(
            handle,
            None,
            0,
            None,
            ctypes.byref(available),
            None,
        ):
            time.sleep(max(0.0, timeout))
            return None
        if not available.value:
            time.sleep(max(0.0, timeout))
            return None
        return os.read(stream.fileno(), min(max_bytes, available.value))  # type: ignore[attr-defined]
    except (AttributeError, OSError, ValueError):
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

    mode = ctypes.c_uint32()
    if not ctypes.windll.kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
        raise RuntimeError("input handle is not a Windows console")
    return int(mode.value)


def _set_windows_console_mode(handle: int, mode: int) -> None:
    import ctypes

    if not ctypes.windll.kernel32.SetConsoleMode(handle, mode):
        raise OSError("could not set Windows console mode")
