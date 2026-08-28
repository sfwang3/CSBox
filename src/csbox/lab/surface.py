from __future__ import annotations

import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from csbox.lab.proxy import OutputAdapter


ALTERNATE_SCREEN_ENTER = b"\x1b[?1049h"
ALTERNATE_SCREEN_EXIT = b"\x1b[?1049l"


class AlternateScreenSurface:
    """Own the terminal screen used by one Lab session.

    The child PTY output is forwarded unchanged after the alternate buffer is
    selected.  Lab chrome is deliberately sent through a separate status
    channel; this object never writes a header, separator, or cursor overlay.
    """

    def __init__(self, output: OutputAdapter) -> None:
        self.output = output
        self._entered = False
        self._original_windows_title: str | None = None

    def __enter__(self) -> AlternateScreenSurface:
        self._original_windows_title = _read_windows_title()
        self._write(ALTERNATE_SCREEN_ENTER)
        self._entered = True
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        del exc_type, exc, traceback
        if not self._entered:
            return
        try:
            self._write(ALTERNATE_SCREEN_EXIT)
        finally:
            _restore_windows_title(self._original_windows_title)
            self._entered = False

    def _write(self, data: bytes) -> None:
        offset = 0
        while offset < len(data):
            written = self.output.write(data[offset:])
            if type(written) is not int or written <= 0:
                raise BrokenPipeError("本地终端未能切换 Lab screen。")
            offset += written


def _read_windows_title() -> str | None:
    if os.name != "nt":
        return None
    try:
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        buffer = ctypes.create_unicode_buffer(32768)
        length = kernel32.GetConsoleTitleW(buffer, len(buffer))
        if not length:
            return ""
        return buffer.value[:length]
    except (AttributeError, OSError, TypeError, ValueError):
        return None


def _restore_windows_title(title: str | None) -> None:
    if os.name != "nt" or title is None:
        return
    try:
        import ctypes

        ctypes.WinDLL("kernel32", use_last_error=True).SetConsoleTitleW(title)
    except (AttributeError, OSError, TypeError, ValueError):
        return


__all__ = ["ALTERNATE_SCREEN_ENTER", "ALTERNATE_SCREEN_EXIT", "AlternateScreenSurface"]
