from __future__ import annotations

from dataclasses import dataclass

from csbox.core.events import TerminalEvent, TerminalEventType
from csbox.lab.dispatcher import TerminalEventDispatcher
from csbox.lab.proxy import TerminalProxy, TerminalStatus
from csbox.lab.screen import TerminalEmulator
from csbox.lab.status import LabSurfacePresenter
from csbox.lab.surface import AlternateScreenSurface


class ScriptedBackend:
    def __init__(self) -> None:
        self.writes: list[bytes] = []
        self.closed = False
        self._reads = [b"child-output", b""]

    def spawn(self, command, *, cwd=None, env=None, size=None) -> None:
        del command, cwd, env, size

    def read(self, max_bytes: int = 65536, timeout: float = 0.05) -> bytes | None:
        del max_bytes, timeout
        return self._reads.pop(0)

    def write(self, data: bytes) -> int:
        self.writes.append(data)
        return len(data)

    def resize(self, columns: int, rows: int) -> None:
        del columns, rows

    def is_alive(self) -> bool:
        return bool(self._reads) and not self.closed

    @property
    def exit_code(self) -> int:
        return 0

    def wait(self, timeout: float = 0.0) -> int:
        del timeout
        return 0

    def close(self) -> None:
        self.closed = True


class MemoryInput:
    def __init__(self, chunks: list[bytes | None]) -> None:
        self.chunks = list(chunks)

    def read(self, max_bytes: int = 4096, timeout: float = 0.05) -> bytes | None:
        del max_bytes, timeout
        return self.chunks.pop(0) if self.chunks else b""


class MemoryOutput:
    def __init__(self) -> None:
        self.data = bytearray()

    def write(self, data: bytes) -> int:
        self.data.extend(data)
        return len(data)


@dataclass
class TerminalState:
    def __enter__(self) -> TerminalState:
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        del exc_type, exc, traceback


class EventSink:
    def __init__(self, events: list[TerminalEvent]) -> None:
        self.events = events

    def handle(self, event: TerminalEvent) -> None:
        self.events.append(event)


def test_alternate_surface_and_capture_status_never_enter_child_or_terminal_events() -> None:
    backend = ScriptedBackend()
    output = MemoryOutput()
    events: list[TerminalEvent] = []
    presenter = LabSurfacePresenter(output, experiment_name="中文实验")
    proxy = TerminalProxy(
        backend,
        command=("pwsh.exe",),
        input_adapter=MemoryInput([b"\x1b[24~", b""]),
        output_adapter=output,
        terminal_state_factory=TerminalState,
        dispatcher=TerminalEventDispatcher([EventSink(events)]),
        emulator=TerminalEmulator(columns=80, rows=24),
        capture_handler=lambda snapshot, timestamp: "capture-1",
        host_boundary=presenter.start,
        status_sink=presenter,
        terminal_surface_factory=lambda: AlternateScreenSurface(output),
    )

    assert proxy.run() == 0

    host_text = bytes(output.data).decode("utf-8")
    assert "CSBox Lab // 中文实验" in host_text
    assert "F12 / Ctrl-Space" in host_text
    assert "child-output" in host_text
    assert "Capture 已保存" in host_text
    assert "\nCSBox Lab" not in host_text
    assert "\x1b[?1049h" in host_text
    assert host_text.endswith("\x1b[?1049l")
    assert backend.writes == []
    assert [event.type for event in events] == [
        TerminalEventType.CAPTURE,
        TerminalEventType.OUTPUT,
        TerminalEventType.EXIT,
    ]
    snapshot_text = "".join(
        cell.character for row in proxy.emulator.snapshot().cells for cell in row
    )
    assert "CSBox Lab" not in snapshot_text
    assert "Capture 已保存" not in snapshot_text


def test_resize_warning_uses_the_same_title_status_channel() -> None:
    output = MemoryOutput()
    presenter = LabSurfacePresenter(output, experiment_name="实验")

    presenter.start()
    presenter.publish(
        TerminalStatus(
            kind="resize_failed",
            message="Resize 未确认；保留终端尺寸 80x24。",
            relative_time=0.1,
        )
    )

    host_text = bytes(output.data).decode("utf-8")
    assert "Resize 未确认" in host_text
    assert "\x1b]0;" in host_text
