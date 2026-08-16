from __future__ import annotations

import codecs
import json
import math
import os
import queue
import threading
import time
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from csbox.core.events import TerminalEvent, TerminalEventType, TerminalSize
from csbox.core.safe_paths import open_private_text_append, open_regular_binary


class RecorderError(RuntimeError):
    """A recording could not accept or durably write an event."""


@dataclass(frozen=True, slots=True)
class CastEvent:
    interval: float
    code: str
    data: str
    # Byte position immediately after this event's source line.  A replay
    # checkpoint can resume reading at this position without decoding the
    # already-applied prefix again.
    cast_offset: int = 0


@dataclass(frozen=True, slots=True)
class CastReadResult:
    header: dict[str, Any]
    events: tuple[CastEvent, ...]
    warnings: tuple[str, ...]
    data_offset: int = 0
    trailing_interval: float = 0.0


_STOP: Final = object()
_CAST_CODES: Final = frozenset({"o", "i", "r", "m", "x"})
MAX_CAST_LINE_BYTES: Final = 1024 * 1024
# Bound the screen allocation as a whole so normal wide terminal geometries are
# not rejected solely because one axis exceeds an arbitrary threshold.
MAX_CAST_SCREEN_CELLS: Final = 1_000_000


@dataclass
class _RecorderAck:
    done: threading.Event = field(default_factory=threading.Event)
    error: BaseException | None = None


@dataclass
class _RollbackRequest:
    sequence: int
    ack: _RecorderAck


class AsciicastV3Recorder:
    """Append asciicast v3 lines on a bounded background writer."""

    def __init__(
        self,
        path: Path | str,
        *,
        columns: int,
        rows: int,
        term_type: str = "xterm-256color",
        env: dict[str, str] | None = None,
        queue_size: int = 1024,
        enqueue_timeout: float = 1.0,
        close_timeout: float = 5.0,
    ) -> None:
        if columns <= 0 or rows <= 0:
            raise ValueError("terminal dimensions must be positive")
        if queue_size <= 0:
            raise ValueError("queue_size must be positive")
        if enqueue_timeout <= 0 or close_timeout <= 0:
            raise ValueError("recorder timeouts must be positive")

        self.path = Path(path)
        self._header: dict[str, Any] = {
            "version": 3,
            "term": {"cols": columns, "rows": rows, "type": term_type},
        }
        safe_env = {name: env[name] for name in ("SHELL", "TERM") if env and name in env}
        if safe_env:
            self._header["env"] = safe_env

        self._queue: queue.Queue[TerminalEvent | object] = queue.Queue(maxsize=queue_size)
        self._enqueue_timeout = enqueue_timeout
        self._close_timeout = close_timeout
        self._state_lock = threading.Lock()
        self._close_lock = threading.Lock()
        self._accepting = True
        self._stop_enqueued = False
        self._fully_closed = False
        self._writer_error: BaseException | None = None
        self._capture_ack_lock = threading.Lock()
        self._capture_waiters: dict[int, _RecorderAck] = {}
        self._ready = threading.Event()
        self._thread = threading.Thread(
            target=self._writer_main,
            name=f"csbox-recorder-{self.path.name}",
            daemon=True,
        )
        self._thread.start()
        if not self._ready.wait(timeout=self._close_timeout):
            raise RecorderError("recorder writer did not start in time")
        self._raise_writer_error()

    @property
    def is_alive(self) -> bool:
        return self._thread.is_alive()

    def record(self, event: TerminalEvent) -> None:
        if not isinstance(event, TerminalEvent):
            raise TypeError("event must be a TerminalEvent")
        capture_ack: _RecorderAck | None = None
        with self._state_lock:
            if not self._accepting:
                raise RecorderError("recorder is closed")
            self._raise_writer_error()
            if event.type is TerminalEventType.CAPTURE:
                capture_ack = _RecorderAck()
                with self._capture_ack_lock:
                    self._capture_waiters[event.sequence] = capture_ack
            try:
                self._queue.put(event, timeout=self._enqueue_timeout)
            except queue.Full as exc:
                if capture_ack is not None:
                    with self._capture_ack_lock:
                        self._capture_waiters.pop(event.sequence, None)
                self._raise_writer_error()
                raise RecorderError("recorder queue remained full") from exc
            if event.type is TerminalEventType.EXIT:
                self._accepting = False
            self._raise_writer_error()
        if capture_ack is not None:
            if not capture_ack.done.wait(timeout=self._close_timeout):
                with self._capture_ack_lock:
                    self._capture_waiters.pop(event.sequence, None)
                raise RecorderError("recorder Capture write did not finish in time")
            if capture_ack.error is not None:
                raise RecorderError("recorder Capture write failed") from capture_ack.error
            self._raise_writer_error()

    handle = record
    write = record
    __call__ = record

    def rollback(self, event: TerminalEvent) -> None:
        if not isinstance(event, TerminalEvent):
            raise TypeError("event must be a TerminalEvent")
        if event.type is not TerminalEventType.CAPTURE:
            return
        ack = _RecorderAck()
        with self._state_lock:
            self._raise_writer_error()
            try:
                self._queue.put(
                    _RollbackRequest(sequence=event.sequence, ack=ack),
                    timeout=self._enqueue_timeout,
                )
            except queue.Full as exc:
                raise RecorderError("recorder rollback queue remained full") from exc
        if not ack.done.wait(timeout=self._close_timeout):
            raise RecorderError("recorder Capture rollback did not finish in time")
        if ack.error is not None:
            raise RecorderError("recorder Capture rollback failed") from ack.error

    def close(self) -> None:
        if not self._close_lock.acquire(timeout=self._close_timeout):
            raise RecorderError("another recorder close did not finish in time")
        try:
            deadline = time.monotonic() + self._close_timeout
            with self._state_lock:
                if self._fully_closed:
                    self._raise_writer_error()
                    return
                self._accepting = False
                stop_enqueued = self._stop_enqueued

            if not stop_enqueued:
                self._enqueue_stop(deadline)
            self._thread.join(timeout=max(0.0, deadline - time.monotonic()))
            if self._thread.is_alive():
                raise RecorderError("recorder writer did not stop in time")
            with self._state_lock:
                self._fully_closed = True
            self._raise_writer_error()
        finally:
            self._close_lock.release()

    def _enqueue_stop(self, deadline: float) -> None:
        while True:
            if not self._thread.is_alive():
                with self._state_lock:
                    self._fully_closed = True
                self._raise_writer_error()
                raise RecorderError("recorder writer stopped before shutdown")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RecorderError("recorder shutdown timed out while queueing stop")
            try:
                self._queue.put(_STOP, timeout=min(self._enqueue_timeout, remaining))
            except queue.Full:
                continue
            with self._state_lock:
                self._stop_enqueued = True
            return

    def _raise_writer_error(self) -> None:
        if self._writer_error is not None:
            raise RecorderError("recorder writer failed") from self._writer_error

    def _writer_main(self) -> None:
        try:
            with open_private_text_append(self.path) as stream:
                is_empty = os.fstat(stream.fileno()).st_size == 0
                if is_empty:
                    self._write_json_line(stream, self._header)
                self._ready.set()
                self._write_events(stream)
        except BaseException as exc:
            self._writer_error = exc
            self._fail_capture_waiters(exc)
            self._ready.set()

    def _write_events(self, stream: Any) -> None:
        decoders = {
            TerminalEventType.OUTPUT: codecs.getincrementaldecoder("utf-8")(errors="replace"),
            TerminalEventType.INPUT: codecs.getincrementaldecoder("utf-8")(errors="replace"),
        }
        previous_time = 0.0
        rounding_error = 0.0
        decoders_finalized = False
        capture_offsets: dict[int, tuple[int, int, float, float]] = {}

        while True:
            item = self._queue.get()
            try:
                if item is _STOP:
                    if not decoders_finalized:
                        self._flush_decoder_tails(stream, decoders)
                    return
                if isinstance(item, _RollbackRequest):
                    restored_timeline = self._rollback_capture(stream, item, capture_offsets)
                    if restored_timeline is not None:
                        previous_time, rounding_error = restored_timeline
                    continue
                assert isinstance(item, TerminalEvent)
                capture_offset = stream.tell() if item.type is TerminalEventType.CAPTURE else None
                previous_time_before_event = previous_time
                rounding_error_before_event = rounding_error
                delta = item.relative_time - previous_time
                if delta < 0:
                    raise RecorderError("event relative time moved backwards")
                previous_time = item.relative_time
                adjusted = delta + rounding_error
                milliseconds = math.floor(adjusted * 1000.0 + 0.5)
                interval = milliseconds / 1000.0
                rounding_error = adjusted - interval
                if item.type is TerminalEventType.EXIT:
                    self._flush_decoder_tails(stream, decoders)
                    decoders_finalized = True
                code, data = _encode_event(item, decoders)
                self._write_json_line(stream, [interval, code, data])
                if capture_offset is not None:
                    capture_offsets[item.sequence] = (
                        capture_offset,
                        stream.tell(),
                        previous_time_before_event,
                        rounding_error_before_event,
                    )
                    self._complete_capture_waiter(item.sequence)
            finally:
                self._queue.task_done()

    def _rollback_capture(
        self,
        stream: Any,
        request: _RollbackRequest,
        capture_offsets: dict[int, tuple[int, int, float, float]],
    ) -> tuple[float, float] | None:
        offsets = capture_offsets.pop(request.sequence, None)
        if offsets is None or stream.tell() != offsets[1]:
            request.ack.error = RecorderError("recorder Capture rollback is no longer contiguous")
            request.ack.done.set()
            return None
        try:
            stream.seek(offsets[0])
            stream.truncate()
            stream.seek(0, os.SEEK_END)
            stream.flush()
        except BaseException as error:
            request.ack.error = error
        finally:
            request.ack.done.set()
        return offsets[2], offsets[3]

    def _complete_capture_waiter(
        self,
        sequence: int,
        error: BaseException | None = None,
    ) -> None:
        with self._capture_ack_lock:
            waiter = self._capture_waiters.pop(sequence, None)
        if waiter is not None:
            waiter.error = error
            waiter.done.set()

    def _fail_capture_waiters(self, error: BaseException) -> None:
        with self._capture_ack_lock:
            waiters = tuple(self._capture_waiters.values())
            self._capture_waiters.clear()
        for waiter in waiters:
            waiter.error = error
            waiter.done.set()

    @staticmethod
    def _write_json_line(stream: Any, value: object) -> None:
        stream.write(json.dumps(value, ensure_ascii=False, separators=(",", ":")))
        stream.write("\n")
        stream.flush()

    @classmethod
    def _flush_decoder_tails(cls, stream: Any, decoders: dict[TerminalEventType, Any]) -> None:
        for event_type, decoder in decoders.items():
            tail = decoder.decode(b"", final=True)
            if tail:
                code = "o" if event_type is TerminalEventType.OUTPUT else "i"
                cls._write_json_line(stream, [0.0, code, tail])


def _encode_event(event: TerminalEvent, decoders: dict[TerminalEventType, Any]) -> tuple[str, str]:
    if event.type in (TerminalEventType.OUTPUT, TerminalEventType.INPUT):
        assert isinstance(event.payload, bytes)
        code = "o" if event.type is TerminalEventType.OUTPUT else "i"
        return code, decoders[event.type].decode(event.payload, final=False)
    if event.type is TerminalEventType.RESIZE:
        assert isinstance(event.payload, TerminalSize)
        return "r", f"{event.payload.columns}x{event.payload.rows}"
    if event.type in (TerminalEventType.MARK, TerminalEventType.CAPTURE):
        assert isinstance(event.payload, str)
        return "m", event.payload
    assert event.type is TerminalEventType.EXIT
    return "x", str(event.payload) if event.payload is not None else "unknown"


class AsciicastV3Reader:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    def read_header(self) -> dict[str, Any]:
        """Validate and return only the header without scanning event data."""

        with open_regular_binary(self.path) as stream:
            for line_number, raw_line, _line_size, _complete_line in _iter_bounded_lines(stream):
                if raw_line is None:
                    raise RecorderError(f"invalid asciicast v3 header on line {line_number}")
                stripped = raw_line.strip()
                if not stripped or stripped.startswith(b"#"):
                    continue
                try:
                    value = json.loads(stripped)
                except (UnicodeDecodeError, ValueError, RecursionError) as exc:
                    raise RecorderError(
                        f"invalid asciicast v3 header on line {line_number}"
                    ) from exc
                if not _is_valid_v3_header(value):
                    raise RecorderError(f"invalid asciicast v3 header on line {line_number}")
                assert isinstance(value, dict)
                return value
        raise RecorderError("asciicast v3 header is missing")

    def read(self) -> CastReadResult:
        warnings: list[str] = []
        header: dict[str, Any] | None = None
        events: list[CastEvent] = []
        pending_unknown_interval = 0.0
        cast_offset = 0
        data_offset = 0

        with open_regular_binary(self.path) as stream:
            for line_number, raw_line, line_size, complete_line in _iter_bounded_lines(stream):
                cast_offset += line_size
                if raw_line is None:
                    if header is None:
                        raise RecorderError(f"invalid asciicast v3 header on line {line_number}")
                    warnings.append(f"oversized cast line {line_number} ignored")
                    continue

                stripped = raw_line.strip()
                if not stripped or stripped.startswith(b"#"):
                    continue
                try:
                    value = json.loads(stripped)
                except (UnicodeDecodeError, ValueError, RecursionError) as exc:
                    if header is None:
                        raise RecorderError(
                            f"invalid asciicast v3 header on line {line_number}"
                        ) from exc
                    if not complete_line:
                        warnings.append(f"truncated cast line {line_number} ignored")
                    else:
                        warnings.append(f"corrupt cast line {line_number} ignored")
                    continue

                if header is None:
                    if not _is_valid_v3_header(value):
                        raise RecorderError(f"invalid asciicast v3 header on line {line_number}")
                    assert isinstance(value, dict)
                    header = value
                    data_offset = cast_offset
                    continue

                parsed = _parse_cast_event(value)
                if parsed is None:
                    warnings.append(f"invalid cast event on line {line_number} ignored")
                    continue
                if parsed.code == "r" and _parse_resize_data(parsed.data) is None:
                    warnings.append(f"invalid resize on line {line_number} ignored")
                    pending_unknown_interval += parsed.interval
                    continue
                if parsed.code not in _CAST_CODES:
                    warnings.append(
                        f"unknown event code {parsed.code!r} on line {line_number} ignored"
                    )
                    pending_unknown_interval += parsed.interval
                    continue
                events.append(
                    CastEvent(
                        interval=parsed.interval + pending_unknown_interval,
                        code=parsed.code,
                        data=parsed.data,
                        cast_offset=cast_offset,
                    )
                )
                pending_unknown_interval = 0.0

        if header is None:
            raise RecorderError("asciicast v3 header is missing")
        return CastReadResult(
            header=header,
            events=tuple(events),
            warnings=tuple(warnings),
            data_offset=data_offset,
            trailing_interval=pending_unknown_interval,
        )


def _iter_bounded_lines(stream: Any) -> Iterator[tuple[int, bytes | None, int, bool]]:
    """Yield complete cast lines without retaining an unbounded input line."""

    line_number = 0
    line = bytearray()
    line_size = 0
    oversized = False
    pending_cr = False

    def append_byte(value: int) -> None:
        nonlocal line_size, oversized
        line_size += 1
        if oversized:
            return
        if len(line) < MAX_CAST_LINE_BYTES:
            line.append(value)
        else:
            oversized = True
            line.clear()

    def emit(complete_line: bool) -> tuple[int, bytes | None, int, bool]:
        nonlocal line_number
        line_number += 1
        return line_number, None if oversized else bytes(line), line_size, complete_line

    def reset() -> None:
        nonlocal line_size, oversized, pending_cr
        line.clear()
        line_size = 0
        oversized = False
        pending_cr = False

    while chunk := stream.read(64 * 1024):
        for value in chunk:
            if pending_cr:
                if value == 0x0A:
                    append_byte(value)
                    yield emit(True)
                    reset()
                    continue
                yield emit(True)
                reset()
            append_byte(value)
            if value == 0x0D:
                pending_cr = True
            elif value == 0x0A:
                yield emit(True)
                reset()

    if line_size:
        yield emit(pending_cr)


def _parse_cast_event(value: object) -> CastEvent | None:
    if not isinstance(value, list) or len(value) != 3:
        return None
    interval, code, data = value
    if isinstance(interval, bool) or not isinstance(interval, int | float):
        return None
    if isinstance(interval, int):
        if interval < 0:
            return None
        try:
            normalized_interval = float(interval)
        except OverflowError:
            return None
    else:
        if not math.isfinite(interval) or interval < 0:
            return None
        normalized_interval = interval
    if not isinstance(code, str) or not isinstance(data, str):
        return None
    return CastEvent(interval=normalized_interval, code=code, data=data)


def _is_valid_v3_header(value: object) -> bool:
    if not isinstance(value, dict) or type(value.get("version")) is not int:
        return False
    if value["version"] != 3:
        return False
    term = value.get("term")
    if not isinstance(term, Mapping):
        return False
    for dimension in ("cols", "rows"):
        size = term.get(dimension)
        if type(size) is not int or size <= 0:
            return False
    if not _valid_dimensions(term["cols"], term["rows"]):
        return False
    return "type" not in term or isinstance(term["type"], str)


def _parse_resize_data(data: str) -> tuple[int, int] | None:
    dimensions = data.lower().split("x", maxsplit=1)
    if len(dimensions) != 2:
        return None
    try:
        columns = int(dimensions[0])
        rows = int(dimensions[1])
    except ValueError:
        return None
    if not _valid_dimensions(columns, rows):
        return None
    return columns, rows


def _valid_dimensions(columns: object, rows: object) -> bool:
    if type(columns) is not int or type(rows) is not int:
        return False
    if columns <= 0 or rows <= 0:
        return False
    return columns <= MAX_CAST_SCREEN_CELLS // rows
