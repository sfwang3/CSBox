from __future__ import annotations

from dataclasses import dataclass
from io import StringIO
from pathlib import Path

import pytest

from csbox.core.events import TerminalEvent, TerminalEventType
from csbox.core.terminal import TerminalBackendError
from csbox.lab import status as status_module
from csbox.lab.dispatcher import TerminalEventDispatcher
from csbox.lab.proxy import TerminalProxy, TerminalStatus
from csbox.lab.recorder import AsciicastV3Recorder
from csbox.lab.screen import TerminalEmulator
from csbox.lab.status import LabSurfacePresenter
from csbox.lab.surface import AlternateScreenSurface


class MemoryOutput:
    def __init__(self) -> None:
        self.data = bytearray()

    def write(self, data: bytes) -> int:
        self.data.extend(data)
        return len(data)


class MemoryInput:
    def __init__(self, events: list[bytes | None], order: list[str]) -> None:
        self.events = list(events)
        self.order = order

    def read(self, max_bytes: int = 4096, timeout: float = 0.05) -> bytes | None:
        del max_bytes, timeout
        return self.events.pop(0) if self.events else b""

    def close(self) -> None:
        self.order.append("input.close")


class ScriptedBackend:
    def __init__(self, order: list[str], *, close_error: BaseException | None = None) -> None:
        self.order = order
        self.close_error = close_error
        self.closed = False

    def spawn(self, command, *, cwd=None, env=None, size=None) -> None:
        del command, cwd, env, size
        self.order.append("backend.spawn")

    def read(self, max_bytes: int = 65536, timeout: float = 0.05) -> bytes | None:
        del max_bytes, timeout
        return b"\x1b[6;19Hchild-output"

    def write(self, data: bytes) -> int:
        del data
        return 0

    def resize(self, columns: int, rows: int) -> None:
        del columns, rows

    def is_alive(self) -> bool:
        return False

    @property
    def exit_code(self) -> int:
        return 0

    def wait(self, timeout: float = 0.0) -> int:
        del timeout
        return 0

    def close(self) -> None:
        self.order.append("backend.close")
        self.closed = True
        if self.close_error is not None:
            raise self.close_error


@dataclass
class OrderedState:
    order: list[str]

    def __enter__(self) -> OrderedState:
        self.order.append("state.enter")
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        del exc_type, exc, traceback
        self.order.append("state.restore")


@dataclass
class OrderedSurface:
    order: list[str]

    def __enter__(self) -> OrderedSurface:
        self.order.append("surface.enter")
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        del exc_type, exc, traceback
        self.order.append("surface.restore")


class EventSink:
    def __init__(self, events: list[TerminalEvent]) -> None:
        self.events = events

    def handle(self, event: TerminalEvent) -> None:
        self.events.append(event)


class AcknowledgementInput:
    def __init__(self, *, attached: bool = True) -> None:
        self.attached = attached
        self.readline_calls = 0

    def isatty(self) -> bool:
        return self.attached

    def readline(self) -> str:
        self.readline_calls += 1
        return "\n"


def test_alternate_surface_is_entered_and_restored_without_inline_host_text() -> None:
    output = MemoryOutput()

    with AlternateScreenSurface(output):
        output.write(b"\x1b[6;19Hchild-output")

    assert bytes(output.data) == b"\x1b[?1049h\x1b[6;19Hchild-output\x1b[?1049l"
    assert b"CSBox Lab" not in output.data
    assert b"separator" not in output.data


def test_lab_status_is_visible_in_title_and_never_becomes_child_vt_or_cast_data() -> None:
    output = MemoryOutput()
    presenter = LabSurfacePresenter(output, experiment_name="中文实验")

    presenter.start()
    presenter.publish(
        TerminalStatus(
            kind="capture_succeeded",
            message="Capture 已保存。",
            relative_time=0.1,
            capture_id="capture-1",
        )
    )

    rendered = bytes(output.data)
    assert b"CSBox Lab // \xe4\xb8\xad\xe6\x96\x87\xe5\xae\x9e\xe9\xaa\x8c" in rendered
    assert "正在录制".encode() in rendered
    assert b"F12 Capture" in rendered
    assert b"ctrl" not in rendered.lower()
    assert b"Capture 1" in rendered
    assert "Capture 已保存".encode() in rendered
    assert b"\n" not in rendered
    assert b"\r" not in rendered


def test_capture_feedback_front_loads_result_and_count_until_child_output_resumes() -> None:
    output = MemoryOutput()
    presenter = LabSurfacePresenter(output, experiment_name="中文实验")
    presenter.start()
    output.data.clear()

    presenter.publish(
        TerminalStatus(
            kind="capture_succeeded",
            message="Capture 已保存。",
            relative_time=0.1,
            capture_id="capture-1",
        )
    )

    success = bytes(output.data)
    assert success.startswith("\x1b]0;✓ Capture 已保存 · #1 |".encode())
    assert success.endswith(b"\x07")
    assert b"\n" not in success and b"\r" not in success

    output.data.clear()
    presenter.publish(
        TerminalStatus(
            kind="capture_failed",
            message="Capture 保存失败；session 继续录制。",
            relative_time=0.2,
        )
    )

    failure = bytes(output.data)
    assert failure.startswith("\x1b]0;✗ Capture 保存失败".encode())
    assert "session 继续录制".encode() in failure
    assert b"Capture 1" in failure

    output.data.clear()
    presenter.refresh()

    resumed = bytes(output.data)
    assert resumed.startswith("\x1b]0;● 正在录制 · Capture 1 |".encode())


def test_completed_lab_feedback_is_explicit_and_waits_for_console_acknowledgement() -> None:
    presenter_type = getattr(status_module, "LabCompletionPresenter", None)
    assert presenter_type is not None
    output = StringIO()
    acknowledgement = AcknowledgementInput()
    presenter = presenter_type(output=output, input_stream=acknowledgement)

    presenter.present(
        experiment_name="中文实验",
        session_id="session-42",
        status="completed",
        capture_count=2,
    )

    rendered = output.getvalue()
    assert rendered.startswith("\x1b]0;✓ 实验已完成 · Capture 2 |")
    assert "✓ 实验已完成" in rendered
    assert "实验：中文实验" in rendered
    assert "Session：session-42" in rendered
    assert "Capture：2" in rendered
    assert "按 Enter 关闭" in rendered
    assert acknowledgement.readline_calls == 1


def test_failed_lab_feedback_gives_a_safe_reason_and_actionable_next_step() -> None:
    output = StringIO()
    acknowledgement = AcknowledgementInput()
    presenter = status_module.LabCompletionPresenter(
        output=output,
        input_stream=acknowledgement,
    )

    presenter.present(
        experiment_name="中文实验",
        session_id="session-failed",
        status="failed",
        capture_count=1,
        failure_reason="终端运行异常，实验记录已标记为失败。",
    )

    rendered = output.getvalue()
    assert rendered.startswith("\x1b]0;✗ 实验失败 · Capture 1 |")
    assert "✗ 实验失败" in rendered
    assert "原因：终端运行异常，实验记录已标记为失败。" in rendered
    assert "下一步：重新运行 csbox，在 Lab 记录中确认状态后重试。" in rendered
    assert acknowledgement.readline_calls == 1


def test_interrupted_lab_feedback_does_not_misreport_completion_or_failure() -> None:
    output = StringIO()
    presenter = status_module.LabCompletionPresenter(
        output=output,
        input_stream=AcknowledgementInput(attached=False),
    )

    presenter.present(
        experiment_name="中文实验",
        session_id="session-interrupted",
        status="interrupted",
        capture_count=3,
        failure_reason="实验已由用户中断，现有记录已保存。",
    )

    rendered = output.getvalue()
    assert rendered.startswith("\x1b]0;! 实验已中断 · Capture 3 |")
    assert "! 实验已中断" in rendered
    assert "实验已由用户中断，现有记录已保存。" in rendered
    assert "实验已完成" not in rendered
    assert "实验失败" not in rendered


def test_completion_feedback_stays_out_of_closed_session_recording(tmp_path: Path) -> None:
    cast_path = tmp_path / "session.cast"
    recorder = AsciicastV3Recorder(cast_path, columns=80, rows=24)
    recorder.record(
        TerminalEvent(
            sequence=1,
            monotonic_time=1.0,
            relative_time=0.1,
            type=TerminalEventType.OUTPUT,
            payload=b"child-result\r\n",
        )
    )
    recorder.record(
        TerminalEvent(
            sequence=2,
            monotonic_time=1.1,
            relative_time=0.2,
            type=TerminalEventType.EXIT,
            payload=0,
        )
    )
    recorder.close()
    output = StringIO()

    status_module.LabCompletionPresenter(
        output=output,
        input_stream=AcknowledgementInput(attached=False),
    ).present(
        experiment_name="中文实验",
        session_id="session-isolated",
        status="completed",
        capture_count=2,
    )

    cast_text = cast_path.read_text(encoding="utf-8")
    assert "child-result" in cast_text
    assert "实验已完成" not in cast_text
    assert "Capture：2" not in cast_text
    assert "实验已完成" in output.getvalue()


def test_normal_eof_restores_surface_before_closing_backend_and_input_owner() -> None:
    order: list[str] = []
    input_adapter = MemoryInput([b""], order)
    state = OrderedState(order)
    surface = OrderedSurface(order)

    class OneOutputBackend(ScriptedBackend):
        def __init__(self) -> None:
            super().__init__(order)
            self.reads = [b"child", b""]

        def read(self, max_bytes: int = 65536, timeout: float = 0.05) -> bytes | None:
            del max_bytes, timeout
            return self.reads.pop(0)

    backend = OneOutputBackend()
    proxy = TerminalProxy(
        backend,
        command=("pwsh.exe",),
        input_adapter=input_adapter,
        output_adapter=MemoryOutput(),
        terminal_state_factory=lambda: state,
        terminal_surface_factory=lambda: surface,
        dispatcher=TerminalEventDispatcher([EventSink([])]),
        emulator=TerminalEmulator(columns=80, rows=24),
    )

    assert proxy.run() == 0
    assert order == [
        "state.enter",
        "surface.enter",
        "backend.spawn",
        "input.close",
        "surface.restore",
        "state.restore",
        "backend.close",
    ]


def test_unknown_exit_while_child_is_alive_fails_before_surface_restore() -> None:
    order: list[str] = []
    input_adapter = MemoryInput([b""], order)

    class UnknownLiveBackend(ScriptedBackend):
        def __init__(self) -> None:
            super().__init__(order)
            self.reads = [b""]

        @property
        def exit_code(self) -> None:
            return None

        def wait(self, timeout: float = 0.0) -> None:
            del timeout
            return None

        def is_alive(self) -> bool:
            return True

        def read(self, max_bytes: int = 65536, timeout: float = 0.05) -> bytes:
            del max_bytes, timeout
            return self.reads.pop(0)

    backend = UnknownLiveBackend()
    proxy = TerminalProxy(
        backend,
        command=("pwsh.exe",),
        input_adapter=input_adapter,
        output_adapter=MemoryOutput(),
        terminal_state_factory=lambda: OrderedState(order),
        terminal_surface_factory=lambda: OrderedSurface(order),
        dispatcher=TerminalEventDispatcher(),
        emulator=TerminalEmulator(columns=80, rows=24),
    )

    with pytest.raises(TerminalBackendError, match="仍在运行"):
        proxy.run()

    assert order == [
        "state.enter",
        "surface.enter",
        "backend.spawn",
        "input.close",
        "backend.close",
        "surface.restore",
        "state.restore",
    ]


def test_live_child_close_is_retried_before_surface_restore() -> None:
    order: list[str] = []
    input_adapter = MemoryInput([b""], order)

    class RetryCloseBackend(ScriptedBackend):
        def __init__(self) -> None:
            super().__init__(order)
            self.close_attempts = 0

        def read(self, max_bytes: int = 65536, timeout: float = 0.05) -> bytes:
            del max_bytes, timeout
            raise RuntimeError("child read failed")

        def close(self) -> None:
            self.order.append("backend.close")
            self.close_attempts += 1
            if self.close_attempts == 1:
                raise OSError("close busy")
            self.closed = True

    backend = RetryCloseBackend()
    proxy = TerminalProxy(
        backend,
        command=("pwsh.exe",),
        input_adapter=input_adapter,
        output_adapter=MemoryOutput(),
        terminal_state_factory=lambda: OrderedState(order),
        terminal_surface_factory=lambda: OrderedSurface(order),
        dispatcher=TerminalEventDispatcher(),
        emulator=TerminalEmulator(columns=80, rows=24),
    )

    with pytest.raises(RuntimeError, match="child read failed"):
        proxy.run()

    assert order == [
        "state.enter",
        "surface.enter",
        "backend.spawn",
        "input.close",
        "backend.close",
        "backend.close",
        "surface.restore",
        "state.restore",
    ]


def test_surface_stays_owned_if_live_child_cannot_be_closed() -> None:
    order: list[str] = []
    input_adapter = MemoryInput([b""], order)

    class UncloseableBackend(ScriptedBackend):
        def read(self, max_bytes: int = 65536, timeout: float = 0.05) -> bytes:
            del max_bytes, timeout
            raise RuntimeError("child read failed")

        def close(self) -> None:
            self.order.append("backend.close")
            raise OSError("close permanently busy")

    proxy = TerminalProxy(
        UncloseableBackend(order),
        command=("pwsh.exe",),
        input_adapter=input_adapter,
        output_adapter=MemoryOutput(),
        terminal_state_factory=lambda: OrderedState(order),
        terminal_surface_factory=lambda: OrderedSurface(order),
        dispatcher=TerminalEventDispatcher(),
        emulator=TerminalEmulator(columns=80, rows=24),
    )

    with pytest.raises(RuntimeError, match="child read failed"):
        proxy.run()

    assert order == [
        "state.enter",
        "surface.enter",
        "backend.spawn",
        "input.close",
        "backend.close",
        "backend.close",
        "state.restore",
    ]


def test_cleanup_failure_is_reported_as_cleanup_failure_after_child_exit() -> None:
    order: list[str] = []
    input_adapter = MemoryInput([b""], order)

    class OneOutputBackend(ScriptedBackend):
        def __init__(self) -> None:
            super().__init__(order, close_error=OSError("closed handle"))
            self.reads = [b"child", b""]

        def read(self, max_bytes: int = 65536, timeout: float = 0.05) -> bytes | None:
            del max_bytes, timeout
            return self.reads.pop(0)

    backend = OneOutputBackend()
    proxy = TerminalProxy(
        backend,
        command=("pwsh.exe",),
        input_adapter=input_adapter,
        output_adapter=MemoryOutput(),
        terminal_state_factory=lambda: OrderedState(order),
        terminal_surface_factory=lambda: OrderedSurface(order),
        dispatcher=TerminalEventDispatcher(),
        emulator=TerminalEmulator(columns=80, rows=24),
    )

    try:
        proxy.run()
    except Exception as error:
        assert "终端清理失败" in str(error)
        assert getattr(error, "exit_code", None) == 0
    else:
        raise AssertionError("cleanup failure must not be reported as a startup failure")


def test_new_recordings_never_persist_raw_input(tmp_path: Path) -> None:
    cast_path = tmp_path / "session.cast"
    recorder = AsciicastV3Recorder(cast_path, columns=80, rows=24)
    recorder.record(
        TerminalEvent(
            sequence=1,
            monotonic_time=1.0,
            relative_time=0.1,
            type=TerminalEventType.INPUT,
            payload=b"password=secret\r",
        )
    )
    recorder.record(
        TerminalEvent(
            sequence=2,
            monotonic_time=1.1,
            relative_time=0.2,
            type=TerminalEventType.EXIT,
            payload=0,
        )
    )
    recorder.close()

    cast_text = cast_path.read_text(encoding="utf-8")
    assert "password=secret" not in cast_text
    assert '"i"' not in cast_text
