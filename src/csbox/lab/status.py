from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from csbox.lab.proxy import OutputAdapter, TerminalStatus


class HostOnlyLabPresenter:
    """Render Lab context/status outside the child terminal event stream."""

    def __init__(
        self,
        output: OutputAdapter,
        *,
        experiment_name: str,
        capture_key: str = "f12",
    ) -> None:
        self.output = output
        self.experiment_name = _sanitize(str(experiment_name))
        self.capture_label = "F12 / Ctrl-Space" if capture_key == "f12" else "Ctrl-Space / F12"
        self._started = False

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        text = (
            f"\nCSBox Lab // {self.experiment_name}\n"
            "● 正在录制\n"
            f"Capture: {self.capture_label}\n"
            "输入 exit 结束实验\n"
            "────────────────────────\n"
        )
        self._write(text.encode("utf-8"))

    def publish(self, status: TerminalStatus) -> None:
        """Update host title without moving the child/emulator cursor."""

        message = _sanitize(status.message)
        title = f"CSBox Lab // {self.experiment_name} | {message}"
        self._write(f"\x1b]0;{title}\x07".encode())

    def _write(self, data: bytes) -> None:
        offset = 0
        while offset < len(data):
            written = self.output.write(data[offset:])
            if type(written) is not int or written <= 0:
                raise BrokenPipeError("本地终端未能接收 host-only 状态。")
            offset += written


def _sanitize(value: str) -> str:
    return "".join(
        character if ord(character) >= 0x20 and character != "\x7f" else " " for character in value
    )
