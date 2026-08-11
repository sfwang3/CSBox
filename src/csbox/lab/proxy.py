from __future__ import annotations

import os
import select
import signal
import sys
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
        file_descriptor = self.stream.fileno()  # type: ignore[attr-defined]
        ready, _, _ = select.select([file_descriptor], [], [], timeout)
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

    def __enter__(self) -> RawTerminalState:
        if os.name != "nt":
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
            self.output_adapter.write(output)
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
