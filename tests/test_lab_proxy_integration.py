from __future__ import annotations

from dataclasses import dataclass

from csbox.core.events import TerminalEvent, TerminalEventType, TerminalSize
from csbox.lab.dispatcher import TerminalEventDispatcher
from csbox.lab.proxy import TerminalProxy, _encode_windows_input
from csbox.lab.screen import TerminalEmulator


class ScriptedBackend:
    def __init__(self, reads: list[bytes | None], *, exit_code: int | None = 0) -> None:
        self.reads = list(reads)
        self.exit_code = exit_code
        self.writes: list[bytes] = []
        self.spawned = False
        self.closed = False
        self.resizes: list[tuple[int, int]] = []
        self.calls: list[tuple[object, ...]] = []
        self.read_timeouts: list[float] = []
        self.resize_error: BaseException | None = None
        self.on_spawn = None

    def spawn(self, command: tuple[str, ...], *, cwd=None, env=None, size=None) -> None:
        del command, cwd, env, size
        self.spawned = True
        self.calls.append(("spawn",))
        if self.on_spawn is not None:
            self.on_spawn()

    def read(self, max_bytes: int = 65536, timeout: float = 0.05) -> bytes | None:
        del max_bytes
        self.read_timeouts.append(timeout)
        if self.reads:
            value = self.reads.pop(0)
            self.calls.append(("read", value))
            return value
        self.calls.append(("read", b""))
        return b""

    def write(self, data: bytes) -> int:
        self.writes.append(data)
        return len(data)

    def resize(self, columns: int, rows: int) -> None:
        self.calls.append(("resize", columns, rows))
        if self.resize_error is not None:
            raise self.resize_error
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


class ResizeNoticeInput(MemoryInput):
    def __init__(self, chunks: list[bytes | None], notices: list[TerminalSize]) -> None:
        super().__init__(chunks)
        self.notices = list(notices)

    def drain_resize_notices(self) -> tuple[TerminalSize, ...]:
        notices = tuple(self.notices)
        self.notices.clear()
        return notices


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
    assert [event.type for event in events] == [TerminalEventType.OUTPUT]


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


def test_windows_scan_codes_are_translated_to_terminal_sequences() -> None:
    assert _encode_windows_input("\x00\x86") == b"\x1b[24~"
    assert _encode_windows_input("\x00H") == b"\x1b[A"


def test_pending_resize_waits_for_already_readable_output_before_barrier() -> None:
    backend = ScriptedBackend([b"before-resize", None, b"after-resize", b""])
    events: list[TerminalEvent] = []
    proxy = TerminalProxy(
        backend,
        command=("bash",),
        input_adapter=MemoryInput([None]),
        output_adapter=MemoryOutput(),
        terminal_state_factory=TerminalState,
        dispatcher=TerminalEventDispatcher([EventSink(events)]),
        emulator=TerminalEmulator(columns=80, rows=24),
        size_provider=lambda: TerminalSize(120, 35),
    )
    backend.on_spawn = proxy.notify_resize

    assert proxy.run() == 0

    assert [event.type for event in events] == [
        TerminalEventType.OUTPUT,
        TerminalEventType.RESIZE,
        TerminalEventType.OUTPUT,
        TerminalEventType.EXIT,
    ]
    assert backend.calls.index(("resize", 120, 35)) > backend.calls.index(
        ("read", b"before-resize")
    )
    assert backend.read_timeouts[0] == 0.0


def test_uncommitted_resizes_are_latest_wins_and_emit_one_event() -> None:
    backend = ScriptedBackend([None, b""])
    events: list[TerminalEvent] = []
    proxy = TerminalProxy(
        backend,
        command=("bash",),
        input_adapter=MemoryInput([None]),
        output_adapter=MemoryOutput(),
        terminal_state_factory=TerminalState,
        dispatcher=TerminalEventDispatcher([EventSink(events)]),
        emulator=TerminalEmulator(columns=80, rows=24),
    )

    def request_latest_sizes() -> None:
        proxy.notify_resize(TerminalSize(100, 30))
        proxy.notify_resize(TerminalSize(160, 45))

    backend.on_spawn = request_latest_sizes

    assert proxy.run() == 0

    assert backend.resizes == [(160, 45)]
    resize_events = [event for event in events if event.type is TerminalEventType.RESIZE]
    assert len(resize_events) == 1
    assert resize_events[0].payload == TerminalSize(160, 45)
    assert proxy.size == TerminalSize(160, 45)


def test_resize_failure_keeps_confirmed_size_warns_and_does_not_fake_event() -> None:
    backend = ScriptedBackend([None, b"after-failed-resize", b""])
    backend.resize_error = OSError("native resize unavailable")
    events: list[TerminalEvent] = []
    statuses = []
    proxy = TerminalProxy(
        backend,
        command=("bash",),
        input_adapter=MemoryInput([None]),
        output_adapter=MemoryOutput(),
        terminal_state_factory=TerminalState,
        dispatcher=TerminalEventDispatcher([EventSink(events)]),
        emulator=TerminalEmulator(columns=80, rows=24),
        status_sink=statuses.append,
    )
    backend.on_spawn = lambda: proxy.notify_resize(TerminalSize(120, 35))

    assert proxy.run() == 0

    assert proxy.size == TerminalSize(80, 24)
    assert not [event for event in events if event.type is TerminalEventType.RESIZE]
    assert [event.type for event in events] == [TerminalEventType.OUTPUT, TerminalEventType.EXIT]
    assert statuses[0].kind == "resize_failed"
    assert "native resize unavailable" in statuses[0].message


def test_proxy_commits_resize_notices_from_the_same_input_owner() -> None:
    backend = ScriptedBackend([None, b""])
    events: list[TerminalEvent] = []
    proxy = TerminalProxy(
        backend,
        command=("bash",),
        input_adapter=ResizeNoticeInput([None, b""], [TerminalSize(100, 30)]),
        output_adapter=MemoryOutput(),
        terminal_state_factory=TerminalState,
        dispatcher=TerminalEventDispatcher([EventSink(events)]),
        emulator=TerminalEmulator(columns=80, rows=24),
    )

    assert proxy.run() == 0

    assert backend.resizes == [(100, 30)]
    assert [event.type for event in events] == [TerminalEventType.RESIZE, TerminalEventType.EXIT]
