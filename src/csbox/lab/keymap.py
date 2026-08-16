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
    """Filter the configured binding and its simple fallback."""

    def __init__(self, key: str = "f12") -> None:
        normalized_key = key.strip().lower()
        try:
            sequence = CAPTURE_SEQUENCES[normalized_key]
        except KeyError as exc:
            supported = ", ".join(sorted(CAPTURE_SEQUENCES))
            raise ValueError(f"不支持的 Capture 快捷键：{key}；可选 {supported}。") from exc
        fallback_key = "ctrl-space" if normalized_key == "f12" else "f12"
        self.key = normalized_key
        self.sequence = sequence
        self.fallback_key = fallback_key
        self.fallback_sequence = CAPTURE_SEQUENCES[fallback_key]
        self.bindings = (self.key, self.fallback_key)
        self.sequences = (self.sequence, self.fallback_sequence)
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
                if pending in self.sequences:
                    captures += 1
                    flush_forward()
                    actions.append(("capture", b""))
                    self._pending.clear()
                    break
                if any(sequence.startswith(pending) for sequence in self.sequences):
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
        self.fallback_key = matcher.fallback_key
        self.fallback_sequence = matcher.fallback_sequence
        self.environ = dict(os.environ if environ is None else environ)
        self.input_supported = input_supported

    def probe(self) -> CaptureBindingAdvisory:
        binding_label = f"{_display_binding(self.key)} / {_display_binding(self.fallback_key)}"
        if not self.input_supported:
            return CaptureBindingAdvisory(
                self.key,
                self.sequence,
                False,
                f"Capture 键 {binding_label} 当前输入适配器不可识别；可在 review 中补 Capture。",
            )
        terminal_program = self.environ.get("TERM_PROGRAM", "").lower()
        if terminal_program == "vscode":
            message = (
                f"检测到 VS Code 终端环境；CSBox 观察 Capture 输入为 {binding_label}。"
                "未观察到匹配输入时可在 review 中补 Capture。"
            )
        elif self.environ.get("WT_SESSION"):
            message = (
                f"检测到 Windows Terminal 环境；CSBox 观察 Capture 输入为 {binding_label}。"
                "未观察到匹配输入时可在 review 中补 Capture。"
            )
        else:
            message = (
                f"CSBox 观察 Capture 输入为 {binding_label}。"
                "未观察到匹配输入时可在 review 中补 Capture。"
            )
        return CaptureBindingAdvisory(self.key, self.sequence, True, message)


def _display_binding(key: str) -> str:
    return "F12" if key == "f12" else "Ctrl-Space"
