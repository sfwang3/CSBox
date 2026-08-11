from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Final

CAPTURE_SEQUENCES: Final[dict[str, bytes]] = {
    "f12": b"\x1b[24~",
    "ctrl-space": b"\x00",
}


@dataclass(frozen=True, slots=True)
class CaptureMatch:
    forwarded: bytes = b""
    captures: int = 0
    actions: tuple[tuple[str, bytes], ...] = ()


class CaptureKeyMatcher:
    """Filter one configured byte sequence without losing chunk boundaries."""

    def __init__(self, key: str = "f12") -> None:
        try:
            sequence = CAPTURE_SEQUENCES[key.strip().lower()]
        except KeyError as exc:
            supported = ", ".join(sorted(CAPTURE_SEQUENCES))
            raise ValueError(f"不支持的 Capture 快捷键：{key}；可选 {supported}。") from exc
        self.key = key.strip().lower()
        self.sequence = sequence
        self._pending = bytearray()

    def feed(self, data: bytes) -> CaptureMatch:
        if not isinstance(data, bytes):
            raise TypeError("Capture 快捷键输入必须是 bytes。")
        forwarded = bytearray()
        captures = 0
        actions: list[tuple[str, bytes]] = []
        pending_forward = bytearray()

        def flush_forward() -> None:
            if pending_forward:
                actions.append(("forward", bytes(pending_forward)))
                pending_forward.clear()

        for byte in data:
            self._pending.append(byte)
            while self._pending:
                pending = bytes(self._pending)
                if pending == self.sequence:
                    captures += 1
                    flush_forward()
                    actions.append(("capture", b""))
                    self._pending.clear()
                    break
                if self.sequence.startswith(pending):
                    break
                forwarded_byte = bytes((self._pending.pop(0),))
                forwarded.extend(forwarded_byte)
                pending_forward.extend(forwarded_byte)
        flush_forward()
        return CaptureMatch(bytes(forwarded), captures, tuple(actions))

    def flush(self) -> CaptureMatch:
        forwarded = bytes(self._pending)
        self._pending.clear()
        actions = (("forward", forwarded),) if forwarded else ()
        return CaptureMatch(forwarded=forwarded, actions=actions)


@dataclass(frozen=True, slots=True)
class CaptureBindingAdvisory:
    key: str
    sequence: bytes
    supported: bool
    message: str


class CaptureBindingProbe:
    """Describe likely host-terminal conflicts without changing host settings."""

    def __init__(
        self,
        key: str = "f12",
        *,
        environ: dict[str, str] | None = None,
        input_supported: bool = True,
    ) -> None:
        matcher = CaptureKeyMatcher(key)
        self.key = matcher.key
        self.sequence = matcher.sequence
        self.environ = dict(os.environ if environ is None else environ)
        self.input_supported = input_supported

    def probe(self) -> CaptureBindingAdvisory:
        if not self.input_supported:
            return CaptureBindingAdvisory(
                self.key,
                self.sequence,
                False,
                f"Capture 键 {self.key} 当前输入适配器不可识别；可在 review 中补 Capture。",
            )
        terminal_program = self.environ.get("TERM_PROGRAM", "").lower()
        if terminal_program == "vscode":
            message = (
                f"提示：Capture 键 {self.key} 可用；VS Code 终端可能抢占该按键，"
                "无法收到时可在 review 中补 Capture。"
            )
        elif self.environ.get("WT_SESSION"):
            message = (
                f"提示：Capture 键 {self.key} 可用；Windows Terminal 可能改写该按键，"
                "无法收到时可在 review 中补 Capture。"
            )
        else:
            message = f"提示：Capture 键 {self.key} 可用；无法收到时可在 review 中补 Capture。"
        return CaptureBindingAdvisory(self.key, self.sequence, True, message)
