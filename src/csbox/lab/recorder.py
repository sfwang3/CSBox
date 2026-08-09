from __future__ import annotations

import codecs
import json
import math
import queue
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from csbox.core.events import TerminalEvent, TerminalEventType, TerminalSize


class RecorderError(RuntimeError):
    """A recording could not accept or durably write an event."""


@dataclass(frozen=True, slots=True)
class CastEvent:
    interval: float
    code: str
    data: str


@dataclass(frozen=True, slots=True)
class CastReadResult:
    header: dict[str, Any]
    events: tuple[CastEvent, ...]
    warnings: tuple[str, ...]


_STOP: Final = object()
_CAST_CODES: Final = frozenset({"o", "i", "r", "m", "x"})


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
        with self._state_lock:
            if not self._accepting:
                raise RecorderError("recorder is closed")
            self._raise_writer_error()
            try:
                self._queue.put(event, timeout=self._enqueue_timeout)
            except queue.Full as exc:
                self._raise_writer_error()
                raise RecorderError("recorder queue remained full") from exc
            if event.type is TerminalEventType.EXIT:
                self._accepting = False
            self._raise_writer_error()

    handle = record
    write = record
    __call__ = record

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
            self.path.parent.mkdir(parents=True, exist_ok=True)
            is_empty = not self.path.exists() or self.path.stat().st_size == 0
            with self.path.open("a", encoding="utf-8", newline="\n") as stream:
                if is_empty:
                    self._write_json_line(stream, self._header)
                self._ready.set()
                self._write_events(stream)
        except BaseException as exc:
            self._writer_error = exc
            self._ready.set()

    def _write_events(self, stream: Any) -> None:
        decoders = {
            TerminalEventType.OUTPUT: codecs.getincrementaldecoder("utf-8")(errors="replace"),
            TerminalEventType.INPUT: codecs.getincrementaldecoder("utf-8")(errors="replace"),
        }
        previous_time = 0.0
        rounding_error = 0.0
        decoders_finalized = False

        while True:
            item = self._queue.get()
            try:
                if item is _STOP:
                    if not decoders_finalized:
                        self._flush_decoder_tails(stream, decoders)
                    return
                assert isinstance(item, TerminalEvent)
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
            finally:
                self._queue.task_done()

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
    return "x", str(event.payload if event.payload is not None else 0)


class AsciicastV3Reader:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    def read(self) -> CastReadResult:
        raw_lines = self.path.read_bytes().splitlines(keepends=True)
        warnings: list[str] = []
        header: dict[str, Any] | None = None
        events: list[CastEvent] = []
        pending_unknown_interval = 0.0

        for line_number, raw_line in enumerate(raw_lines, start=1):
            complete_line = raw_line.endswith((b"\n", b"\r"))
            stripped = raw_line.strip()
            if not stripped or stripped.startswith(b"#"):
                continue
            try:
                value = json.loads(stripped)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                if header is None:
                    raise RecorderError(
                        f"invalid asciicast v3 header on line {line_number}"
                    ) from exc
                if line_number == len(raw_lines) and not complete_line:
                    warnings.append(f"truncated cast line {line_number} ignored")
                else:
                    warnings.append(f"corrupt cast line {line_number} ignored")
                continue

            if header is None:
                if not _is_valid_v3_header(value):
                    raise RecorderError(f"invalid asciicast v3 header on line {line_number}")
                assert isinstance(value, dict)
                header = value
                continue

            parsed = _parse_cast_event(value)
            if parsed is None:
                warnings.append(f"invalid cast event on line {line_number} ignored")
                continue
            if parsed.code not in _CAST_CODES:
                warnings.append(f"unknown event code {parsed.code!r} on line {line_number} ignored")
                pending_unknown_interval += parsed.interval
                continue
            events.append(
                CastEvent(
                    interval=parsed.interval + pending_unknown_interval,
                    code=parsed.code,
                    data=parsed.data,
                )
            )
            pending_unknown_interval = 0.0

        if header is None:
            raise RecorderError("asciicast v3 header is missing")
        return CastReadResult(header=header, events=tuple(events), warnings=tuple(warnings))


def _parse_cast_event(value: object) -> CastEvent | None:
    if not isinstance(value, list) or len(value) != 3:
        return None
    interval, code, data = value
    if (
        isinstance(interval, bool)
        or not isinstance(interval, int | float)
        or not math.isfinite(interval)
        or interval < 0
    ):
        return None
    if not isinstance(code, str) or not isinstance(data, str):
        return None
    return CastEvent(interval=float(interval), code=code, data=data)


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
    return "type" not in term or isinstance(term["type"], str)
