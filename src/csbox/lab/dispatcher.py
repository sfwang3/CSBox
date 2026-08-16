from __future__ import annotations

from collections.abc import Iterable
from typing import Protocol

from csbox.core.events import TerminalEvent, TerminalEventType


class TerminalEventSink(Protocol):
    def handle(self, event: TerminalEvent) -> None: ...


class DispatchError(RuntimeError):
    def __init__(self, sink_index: int, sink: object) -> None:
        self.sink_index = sink_index
        self.sink = sink
        self.rollback_errors: tuple[BaseException, ...] = ()
        super().__init__(f"terminal event sink {sink_index} failed")


class TerminalEventDispatcher:
    """Synchronously fan out events in explicit registration order."""

    def __init__(self, sinks: Iterable[TerminalEventSink] = ()) -> None:
        self._sinks: list[TerminalEventSink] = []
        for sink in sinks:
            self.register(sink)

    @property
    def sinks(self) -> tuple[TerminalEventSink, ...]:
        return tuple(self._sinks)

    def register(self, sink: TerminalEventSink) -> None:
        if not callable(getattr(sink, "handle", None)):
            raise TypeError("sink must provide handle(event)")
        self._sinks.append(sink)

    def unregister(self, sink: TerminalEventSink) -> None:
        self._sinks.remove(sink)

    def dispatch(self, event: TerminalEvent) -> None:
        if not isinstance(event, TerminalEvent):
            raise TypeError("event must be a TerminalEvent")
        completed: list[TerminalEventSink] = []
        for index, sink in enumerate(tuple(self._sinks), start=1):
            try:
                sink.handle(event)
            except Exception as exc:
                error = DispatchError(index, sink)
                if event.type is TerminalEventType.CAPTURE:
                    rollback_errors: list[BaseException] = []
                    for completed_sink in (sink, *reversed(completed)):
                        rollback = getattr(completed_sink, "rollback", None)
                        if not callable(rollback):
                            continue
                        try:
                            rollback(event)
                        except BaseException as rollback_error:
                            rollback_errors.append(rollback_error)
                    error.rollback_errors = tuple(rollback_errors)
                raise error from exc
            completed.append(sink)

    __call__ = dispatch
