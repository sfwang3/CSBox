from __future__ import annotations

from dataclasses import dataclass

from csbox.core.events import TerminalEvent, TerminalEventType
from csbox.lab.dispatcher import TerminalEventDispatcher
from csbox.lab.proxy import TerminalProxy
from csbox.lab.screen import TerminalEmulator


class ScriptedBackend:
    def __init__(self, reads: list[bytes | None], *, exit_code: int | None = 0) -> None:
        self.reads = list(reads)
        self.exit_code = exit_code
        self.writes: list[bytes] = []
        self.spawned = False
        self.closed = False
        self.resizes: list[tuple[int, int]] = []

    def spawn(self, command: tuple[str, ...], *, cwd=None, env=None, size=None) -> None:
        self.spawned = True

    def read(self, max_bytes: int = 65536, timeout: float = 0.05) -> bytes | None:
        del max_bytes, timeout
        if self.reads:
            return self.reads.pop(0)
        return b""

    def write(self, data: bytes) -> int:
        self.writes.append(data)
        return len(data)

    def resize(self, columns: int, rows: int) -> None:
        self.resizes.append((columns, rows))

    def is_alive(self) -> bool:
        return not self.closed and bool(self.reads)

    def wait(self, timeout: float = 0.0) -> int | None:
        del timeout
        return self.exit_code

    def close(self) -> None:
        self.closed = True


class MemoryInput:
    def __init__(self, chunks: list[bytes | None]) -> None:
        self.chunks = list(chunks)

    def read(self, max_bytes: int = 4096, timeout: float = 0.05) -> bytes | None:
        del max_bytes, timeout
        if self.chunks:
            return self.chunks.pop(0)
        return b""


class MemoryOutput:
    def __init__(self) -> None:
        self.data = bytearray()

    def write(self, data: bytes) -> int:
        self.data.extend(data)
        return len(data)


class ShortMemoryOutput(MemoryOutput):
    def write(self, data: bytes) -> int:
        self.data.extend(data[:1])
        return min(1, len(data))


class EventSink:
    def __init__(self, events: list[TerminalEvent]) -> None:
        self.events = events

    def handle(self, event: TerminalEvent) -> None:
        self.events.append(event)


@dataclass
class TerminalState:
    entered: bool = False
    restored: bool = False

    def __enter__(self) -> TerminalState:
        self.entered = True
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.restored = True


def test_proxy_filters_capture_input_preserves_output_and_orders_events() -> None:
    backend = ScriptedBackend([b"out", b"", None])
    input_adapter = MemoryInput([b"in\x1b[24~", b"\r", b""])
    output_adapter = MemoryOutput()
    state = TerminalState()
    events: list[TerminalEvent] = []

    dispatcher = TerminalEventDispatcher([EventSink(events)])
    emulator = TerminalEmulator(columns=8, rows=2)

    proxy = TerminalProxy(
        backend,
        command=("bash",),
        input_adapter=input_adapter,
        output_adapter=output_adapter,
        terminal_state_factory=lambda: state,
        dispatcher=dispatcher,
        emulator=emulator,
        capture_handler=lambda snapshot, timestamp: "capture-1",
    )

    exit_code = proxy.run()

    assert exit_code == 0
    assert state.entered is True
    assert state.restored is True
    assert backend.writes == [b"in", b"\r"]
    assert bytes(output_adapter.data) == b"out"
    assert [event.type for event in events] == [
        TerminalEventType.INPUT,
        TerminalEventType.CAPTURE,
        TerminalEventType.OUTPUT,
        TerminalEventType.INPUT,
        TerminalEventType.EXIT,
    ]
    assert events[1].payload == "capture-1"
    assert events[-1].payload == 0


def test_proxy_restores_terminal_state_when_backend_crashes_after_output() -> None:
    class CrashingBackend(ScriptedBackend):
        def read(self, max_bytes: int = 65536, timeout: float = 0.05) -> bytes | None:
            if self.reads:
                return self.reads.pop(0)
            raise RuntimeError("shell crashed")

    backend = CrashingBackend([b"before-crash"])
    state = TerminalState()
    events: list[TerminalEvent] = []
    output_adapter = MemoryOutput()
    proxy = TerminalProxy(
        backend,
        command=("bash",),
        input_adapter=MemoryInput([None]),
        output_adapter=output_adapter,
        terminal_state_factory=lambda: state,
        dispatcher=TerminalEventDispatcher([EventSink(events)]),
        emulator=TerminalEmulator(columns=8, rows=2),
    )

    try:
        proxy.run()
    except RuntimeError as error:
        assert str(error) == "shell crashed"
    else:
        raise AssertionError("proxy must propagate backend failures")

    assert state.restored is True
    assert bytes(output_adapter.data) == b"before-crash"
    assert events[-1].type is TerminalEventType.EXIT
    assert events[-1].payload is None


def test_proxy_keeps_capture_position_when_capture_precedes_later_input() -> None:
    backend = ScriptedBackend([b"", None])
    events: list[TerminalEvent] = []
    proxy = TerminalProxy(
        backend,
        command=("bash",),
        input_adapter=MemoryInput([b"\x1b[24~later", b""]),
        output_adapter=MemoryOutput(),
        terminal_state_factory=TerminalState,
        dispatcher=TerminalEventDispatcher([EventSink(events)]),
        emulator=TerminalEmulator(columns=8, rows=2),
        capture_handler=lambda snapshot, timestamp: "capture-1",
    )

    proxy.run()

    assert [event.type for event in events] == [
        TerminalEventType.CAPTURE,
        TerminalEventType.INPUT,
        TerminalEventType.EXIT,
    ]
    assert backend.writes == [b"later"]


def test_proxy_retries_short_output_writes_without_dropping_bytes() -> None:
    backend = ScriptedBackend(["完整输出".encode(), b""])
    output = ShortMemoryOutput()
    proxy = TerminalProxy(
        backend,
        command=("bash",),
        input_adapter=MemoryInput([b""]),
        output_adapter=output,
        terminal_state_factory=TerminalState,
        dispatcher=TerminalEventDispatcher([]),
        emulator=TerminalEmulator(columns=8, rows=2),
    )

    proxy.run()

    assert bytes(output.data) == "完整输出".encode()
