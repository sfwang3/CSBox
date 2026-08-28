from __future__ import annotations

import sys
from typing import TYPE_CHECKING

from csbox.core.display_width import truncate_cells

if TYPE_CHECKING:
    from csbox.lab.proxy import OutputAdapter, TerminalStatus


class LabSurfacePresenter:
    """Expose Lab context through the terminal title, not the child screen.

    Windows Terminal and other VT consumers keep OSC title updates outside the
    child PTY screen.  The proxy refreshes the title after child output so a
    shell that writes its own title cannot permanently hide the Lab contract.
    """

    def __init__(
        self,
        output: OutputAdapter,
        *,
        experiment_name: str,
        capture_key: str = "f12",
    ) -> None:
        self.output = output
        self.experiment_name = _sanitize(str(experiment_name))
        self.capture_label = "F12 Capture"
        self._started = False
        self.capture_count = 0

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        self.refresh()

    def publish(self, status: TerminalStatus) -> None:
        if status.kind == "capture_succeeded":
            self.capture_count += 1
            message = _sanitize(status.message).rstrip("。")
            leading_status = f"✓ {message} · #{self.capture_count} | Capture {self.capture_count}"
        elif status.kind == "capture_failed":
            leading_status = f"✗ {_sanitize(status.message)} · Capture {self.capture_count}"
        else:
            leading_status = f"⚠ {_sanitize(status.message)} · Capture {self.capture_count}"
        self._render(leading_status)

    def __call__(self, status: TerminalStatus) -> None:
        self.publish(status)

    def refresh(self) -> None:
        if not self._started:
            return
        self._render(f"● 正在录制 · Capture {self.capture_count}")

    def _render(self, leading_status: str) -> None:
        title = (
            f"{leading_status} | CSBox Lab // {self.experiment_name} | "
            f"{self.capture_label} | 输入 exit 结束实验"
        )
        self._write(f"\x1b]0;{title}\x07".encode())

    def _write(self, data: bytes) -> None:
        offset = 0
        while offset < len(data):
            written = self.output.write(data[offset:])
            if type(written) is not int or written <= 0:
                raise BrokenPipeError("本地终端未能接收 Lab 状态。")
            offset += written


class LabCompletionPresenter:
    """Show the final Lab result after child recording has stopped."""

    def __init__(self, output: object | None = None, input_stream: object | None = None) -> None:
        self.output = sys.stdout if output is None else output
        self.input_stream = sys.stdin if input_stream is None else input_stream

    def present(
        self,
        *,
        experiment_name: str,
        session_id: str,
        status: str,
        capture_count: int,
        failure_reason: str | None = None,
    ) -> None:
        name = truncate_cells(_sanitize(str(experiment_name)), 48, ellipsis="…")
        session = truncate_cells(_sanitize(str(session_id)), 64, ellipsis="…")
        if status == "completed":
            title = f"✓ 实验已完成 · Capture {capture_count} | {name} | {session}"
            result_lines = ("✓ 实验已完成",)
        elif status == "failed":
            reason = truncate_cells(
                _sanitize(failure_reason or "实验运行异常，记录已标记为失败。"),
                72,
                ellipsis="…",
            )
            title = f"✗ 实验失败 · Capture {capture_count} | {name} | {session}"
            result_lines = (
                "✗ 实验失败",
                f"原因：{reason}",
                "下一步：重新运行 csbox，在 Lab 记录中确认状态后重试。",
            )
        elif status == "interrupted":
            reason = truncate_cells(
                _sanitize(failure_reason or "实验已中断，现有记录已保存。"),
                72,
                ellipsis="…",
            )
            title = f"! 实验已中断 · Capture {capture_count} | {name} | {session}"
            result_lines = (
                "! 实验已中断",
                f"原因：{reason}",
                "下一步：在 Lab 记录中确认已保存内容；需要时重新开始实验。",
            )
        else:
            raise ValueError("unsupported Lab completion status")
        lines = (
            "CSBox Lab",
            *result_lines,
            f"实验：{name}",
            f"Session：{session}",
            f"Capture：{capture_count}",
            "按 Enter 关闭此 Lab 窗口…",
        )
        message = f"\x1b]0;{title}\x07\r\n\r\n" + "\r\n".join(lines) + "\r\n"
        self.output.write(message)  # type: ignore[attr-defined]
        self.output.flush()  # type: ignore[attr-defined]
        isatty = getattr(self.input_stream, "isatty", None)
        if callable(isatty) and isatty():
            self.input_stream.readline()  # type: ignore[attr-defined]


def _sanitize(value: str) -> str:
    return "".join(
        character if ord(character) >= 0x20 and character != "\x7f" else " " for character in value
    )
