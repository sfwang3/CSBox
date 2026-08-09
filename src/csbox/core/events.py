from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum


class TerminalEventType(StrEnum):
    OUTPUT = "output"
    INPUT = "input"
    RESIZE = "resize"
    MARK = "mark"
    CAPTURE = "capture"
    EXIT = "exit"


@dataclass(frozen=True, slots=True)
class TerminalSize:
    columns: int
    rows: int


@dataclass(frozen=True, slots=True)
class TerminalEvent:
    sequence: int
    monotonic_time: float
    relative_time: float
    type: TerminalEventType
    payload: bytes | str | TerminalSize | int | None


class TerminalEventClock:
    """Create terminal events with a monotonic sequence and relative time."""

    def __init__(self, monotonic: Callable[[], float] = time.monotonic) -> None:
        self._monotonic = monotonic
        self._started_at = monotonic()
        self._sequence = 0

    def next(
        self,
        event_type: TerminalEventType,
        payload: bytes | str | TerminalSize | int | None = None,
    ) -> TerminalEvent:
        monotonic_time = self._monotonic()
        self._sequence += 1
        return TerminalEvent(
            sequence=self._sequence,
            monotonic_time=monotonic_time,
            relative_time=monotonic_time - self._started_at,
            type=event_type,
            payload=payload,
        )
