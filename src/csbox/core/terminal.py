from __future__ import annotations

import platform
from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from csbox.core.events import TerminalSize

if TYPE_CHECKING:
    from csbox.core.terminal_unix import UnixPTYBackend
    from csbox.core.terminal_windows import WindowsConPTYBackend


DEFAULT_TERMINAL_SIZE = TerminalSize(80, 24)


class TerminalBackendError(RuntimeError):
    """A terminal backend failure with stable Chinese user-facing guidance."""

    def __init__(self, user_message: str, cause: Exception) -> None:
        super().__init__(user_message)
        self.user_message = user_message
        self.cause = cause


class TerminalBackend(ABC):
    @abstractmethod
    def spawn(
        self,
        command: Sequence[str],
        *,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        size: TerminalSize = DEFAULT_TERMINAL_SIZE,
    ) -> None:
        """Start a process connected to a pseudoterminal."""

    @abstractmethod
    def read(self, max_bytes: int = 65536, timeout: float = 0.05) -> bytes | None:
        """Return output bytes, None when idle, or b"" after EOF."""

    @abstractmethod
    def write(self, data: bytes) -> int:
        """Write input bytes to the terminal."""

    @abstractmethod
    def resize(self, columns: int, rows: int) -> None:
        """Resize the terminal viewport."""

    @abstractmethod
    def is_alive(self) -> bool:
        """Return whether the terminal process is alive."""

    @abstractmethod
    def wait(self, timeout: float = 0.0) -> int | None:
        """Wait up to timeout seconds and return the exit code when available."""

    @property
    @abstractmethod
    def exit_code(self) -> int | None:
        """Return the process exit code when available."""

    @abstractmethod
    def close(self) -> None:
        """Close the terminal process and associated resources."""


def create_terminal_backend(system: str | None = None) -> TerminalBackend:
    os_name = platform.system() if system is None else system
    if os_name == "Windows":
        from csbox.core.terminal_windows import WindowsConPTYBackend

        return WindowsConPTYBackend()
    from csbox.core.terminal_unix import UnixPTYBackend

    return UnixPTYBackend()


def __getattr__(name: str) -> type[TerminalBackend]:
    if name == "UnixPTYBackend":
        from csbox.core.terminal_unix import UnixPTYBackend

        return UnixPTYBackend
    if name == "WindowsConPTYBackend":
        from csbox.core.terminal_windows import WindowsConPTYBackend

        return WindowsConPTYBackend
    raise AttributeError(name)


__all__ = [
    "TerminalBackend",
    "TerminalBackendError",
    "DEFAULT_TERMINAL_SIZE",
    "UnixPTYBackend",
    "WindowsConPTYBackend",
    "create_terminal_backend",
]
