from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from pathlib import Path


class TerminalBackend(ABC):
    @abstractmethod
    def spawn(
        self,
        command: Sequence[str],
        *,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
    ) -> None:
        """Start a terminal process in a future backend implementation."""

    @abstractmethod
    def read(self, max_bytes: int = 4096) -> bytes:
        """Read terminal output."""

    @abstractmethod
    def write(self, data: str | bytes) -> int:
        """Write input to the terminal."""

    @abstractmethod
    def resize(self, columns: int, rows: int) -> None:
        """Resize the terminal viewport."""

    @abstractmethod
    def is_alive(self) -> bool:
        """Return whether the terminal process is alive."""

    @abstractmethod
    def close(self) -> None:
        """Close the terminal process and associated resources."""


class _ReservedTerminalBackend(TerminalBackend):
    def _not_implemented(self) -> None:
        raise NotImplementedError(f"{type(self).__name__} is not implemented")

    def spawn(
        self,
        command: Sequence[str],
        *,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
    ) -> None:
        del command, cwd, env
        self._not_implemented()

    def read(self, max_bytes: int = 4096) -> bytes:
        del max_bytes
        self._not_implemented()
        return b""

    def write(self, data: str | bytes) -> int:
        del data
        self._not_implemented()
        return 0

    def resize(self, columns: int, rows: int) -> None:
        del columns, rows
        self._not_implemented()

    def is_alive(self) -> bool:
        self._not_implemented()
        return False

    def close(self) -> None:
        self._not_implemented()


class WindowsConPTYBackend(_ReservedTerminalBackend):
    """Reserved adapter for a future Windows ConPTY implementation."""


class UnixPTYBackend(_ReservedTerminalBackend):
    """Reserved adapter for a future Unix PTY implementation."""
