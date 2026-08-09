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

    def __post_init__(self) -> None:
        for name, value in (("columns", self.columns), ("rows", self.rows)):
            if type(value) is not int:
                raise TypeError(f"{name} must be an integer")
            if value <= 0:
                raise ValueError(f"{name} must be positive")


@dataclass(frozen=True, slots=True)
class TerminalEvent:
    sequence: int
    monotonic_time: float
    relative_time: float
    type: TerminalEventType
    payload: bytes | str | TerminalSize | int | None

    def __post_init__(self) -> None:
        if not isinstance(self.type, TerminalEventType):
            raise TypeError("type must be a TerminalEventType")
        if self.type in (TerminalEventType.OUTPUT, TerminalEventType.INPUT):
            valid_payload = isinstance(self.payload, bytes)
        elif self.type is TerminalEventType.RESIZE:
            valid_payload = isinstance(self.payload, TerminalSize)
        elif self.type in (TerminalEventType.MARK, TerminalEventType.CAPTURE):
            valid_payload = isinstance(self.payload, str)
        else:
            valid_payload = self.payload is None or type(self.payload) is int
        if not valid_payload:
            raise TypeError(f"invalid payload for {self.type.value} event")


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
        sequence = self._sequence + 1
        event = TerminalEvent(
            sequence=sequence,
            monotonic_time=monotonic_time,
            relative_time=monotonic_time - self._started_at,
            type=event_type,
            payload=payload,
        )
        self._sequence = sequence
        return event
