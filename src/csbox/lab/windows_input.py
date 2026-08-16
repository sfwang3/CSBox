from __future__ import annotations

import os
from collections import deque
from collections.abc import Callable, Iterable
from typing import Any

from csbox.core.events import TerminalSize

KEY_EVENT = 0x0001
WINDOW_BUFFER_SIZE_EVENT = 0x0004

VK_SPACE = 0x20
VK_PRIOR = 0x21
VK_NEXT = 0x22
VK_END = 0x23
VK_HOME = 0x24
VK_LEFT = 0x25
VK_UP = 0x26
VK_RIGHT = 0x27
VK_DOWN = 0x28
VK_INSERT = 0x2D
VK_DELETE = 0x2E
VK_F1 = 0x70
VK_F12 = 0x7B

LEFT_CTRL_PRESSED = 0x0008
RIGHT_CTRL_PRESSED = 0x0004

_CTRL_PRESSED = LEFT_CTRL_PRESSED | RIGHT_CTRL_PRESSED
_MAX_RECORDS_PER_READ = 128

_VIRTUAL_KEY_SEQUENCES = {
    VK_UP: b"\x1b[A",
    VK_DOWN: b"\x1b[B",
    VK_RIGHT: b"\x1b[C",
    VK_LEFT: b"\x1b[D",
    VK_HOME: b"\x1b[H",
    VK_END: b"\x1b[F",
    VK_INSERT: b"\x1b[2~",
    VK_DELETE: b"\x1b[3~",
    VK_PRIOR: b"\x1b[5~",
    VK_NEXT: b"\x1b[6~",
    VK_F1: b"\x1bOP",
    VK_F1 + 1: b"\x1bOQ",
    VK_F1 + 2: b"\x1bOR",
    VK_F1 + 3: b"\x1bOS",
    VK_F1 + 4: b"\x1b[15~",
    VK_F1 + 5: b"\x1b[17~",
    VK_F1 + 6: b"\x1b[18~",
    VK_F1 + 7: b"\x1b[19~",
    VK_F1 + 8: b"\x1b[20~",
    VK_F1 + 9: b"\x1b[21~",
    VK_F1 + 10: b"\x1b[23~",
    VK_F12: b"\x1b[24~",
}

_NATIVE_SCAN_SEQUENCES = {
    0x48: b"\x1b[A",
    0x50: b"\x1b[B",
    0x4D: b"\x1b[C",
    0x4B: b"\x1b[D",
    0x47: b"\x1b[H",
    0x4F: b"\x1b[F",
    0x52: b"\x1b[2~",
    0x53: b"\x1b[3~",
    0x49: b"\x1b[5~",
    0x51: b"\x1b[6~",
    0x3B: b"\x1bOP",
    0x3C: b"\x1bOQ",
    0x3D: b"\x1bOR",
    0x3E: b"\x1bOS",
    0x3F: b"\x1b[15~",
    0x40: b"\x1b[17~",
    0x41: b"\x1b[18~",
    0x42: b"\x1b[19~",
    0x43: b"\x1b[20~",
    0x44: b"\x1b[21~",
    0x57: b"\x1b[23~",
    0x58: b"\x1b[24~",
}


def decode_console_records(
    records: Iterable[object],
) -> tuple[bytes, tuple[TerminalSize, ...]]:
    """Translate one native console batch into key bytes and resize notices."""

    key_bytes = bytearray()
    resize_notices: list[TerminalSize] = []
    pending_high_surrogate: str | None = None
    for record in records:
        event_type = _field(record, "EventType", "event_type")
        event = _field(record, "Event", "event", default=record)
        if event_type == KEY_EVENT:
            key_event = _field(event, "KeyEvent", "key_event", default=event)
            encoded, pending_high_surrogate = _encode_key_event(
                key_event,
                pending_high_surrogate,
            )
            key_bytes.extend(encoded)
        elif event_type == WINDOW_BUFFER_SIZE_EVENT:
            resize_event = _field(
                event,
                "WindowBufferSizeEvent",
                "window_buffer_size_event",
                default=event,
            )
            coordinate = _field(resize_event, "dwSize", "size", default=resize_event)
            columns = _field(coordinate, "X", "columns")
            rows = _field(coordinate, "Y", "rows")
            if type(columns) is int and type(rows) is int and columns > 0 and rows > 0:
                resize_notices.append(TerminalSize(columns, rows))
    if pending_high_surrogate is not None:
        key_bytes.extend(_encode_surrogate(pending_high_surrogate))
    return bytes(key_bytes), tuple(resize_notices)


def _encode_key_event(
    event: object,
    pending_high_surrogate: str | None,
) -> tuple[bytes, str | None]:
    if not bool(_field(event, "bKeyDown", "key_down", default=False)):
        return b"", pending_high_surrogate
    character = _field(event, "uChar", "character", default="")
    if not isinstance(character, str):
        character = _field(character, "UnicodeChar", "unicode_char", default="")
    if isinstance(character, str) and character and character != "\x00":
        prefix = b""
        if pending_high_surrogate is not None:
            if _is_low_surrogate(character):
                return _encode_surrogate(pending_high_surrogate + character), None
            prefix = _encode_surrogate(pending_high_surrogate)
            pending_high_surrogate = None
        if _is_high_surrogate(character):
            return prefix, character
        return prefix + character.encode("utf-8", errors="surrogatepass"), None

    virtual_key = _field(event, "wVirtualKeyCode", "virtual_key", default=0)
    scan_code = _field(event, "wVirtualScanCode", "scan_code", default=0)
    control_state = _field(event, "dwControlKeyState", "control_key_state", default=0)
    prefix = (
        _encode_surrogate(pending_high_surrogate) if pending_high_surrogate is not None else b""
    )
    if (virtual_key == VK_SPACE or scan_code == 0x39) and control_state & _CTRL_PRESSED:
        return prefix + b"\x00", None
    sequence = _VIRTUAL_KEY_SEQUENCES.get(virtual_key)
    if sequence is None:
        sequence = _NATIVE_SCAN_SEQUENCES.get(scan_code, b"")
    return prefix + sequence, None


def _is_high_surrogate(value: str) -> bool:
    return len(value) == 1 and 0xD800 <= ord(value) <= 0xDBFF


def _is_low_surrogate(value: str) -> bool:
    return len(value) == 1 and 0xDC00 <= ord(value) <= 0xDFFF


def _encode_surrogate(value: str) -> bytes:
    return (
        value.encode("utf-16-le", errors="surrogatepass")
        .decode("utf-16-le", errors="surrogatepass")
        .encode("utf-8", errors="surrogatepass")
    )


def _field(value: object, *names: str, default: Any = None) -> Any:
    for name in names:
        if hasattr(value, name):
            return getattr(value, name)
    return default


RecordReader = Callable[[float, int], Iterable[object] | None]


class WindowsConsoleInputOwner:
    """Own native console reads for both key and window-size records."""

    def __init__(
        self,
        *,
        handle: int | None = None,
        record_reader: RecordReader | None = None,
    ) -> None:
        self._reader = record_reader or _native_record_reader(handle)
        self._resize_notices: deque[TerminalSize] = deque()
        self._pending_key_bytes = bytearray()

    def read(self, max_bytes: int = 4096, timeout: float = 0.05) -> bytes | None:
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        if self._pending_key_bytes:
            return self._take_key_bytes(max_bytes)
        records = self._reader(max(0.0, timeout), _MAX_RECORDS_PER_READ)
        if records is None:
            return None
        key_bytes, resize_notices = decode_console_records(records)
        self._pending_key_bytes.extend(key_bytes)
        self._resize_notices.extend(resize_notices)
        if not self._pending_key_bytes:
            return None
        return self._take_key_bytes(max_bytes)

    def drain_resize_notices(self) -> tuple[TerminalSize, ...]:
        notices = tuple(self._resize_notices)
        self._resize_notices.clear()
        return notices

    def _take_key_bytes(self, max_bytes: int) -> bytes:
        result = bytes(self._pending_key_bytes[:max_bytes])
        del self._pending_key_bytes[:max_bytes]
        return result


def _native_record_reader(handle: int | None) -> RecordReader:
    if os.name != "nt":
        raise OSError("Windows console input is available only on Windows")
    if handle is None:
        raise OSError("a Windows console input handle is required")
    return _Win32RecordReader(handle)


class _Win32RecordReader:
    def __init__(self, handle: int) -> None:
        import ctypes
        from ctypes import wintypes

        class Coordinate(ctypes.Structure):
            _fields_ = [("X", wintypes.SHORT), ("Y", wintypes.SHORT)]

        class CharacterUnion(ctypes.Union):
            _fields_ = [("UnicodeChar", wintypes.WCHAR), ("AsciiChar", ctypes.c_char)]

        class KeyEvent(ctypes.Structure):
            _fields_ = [
                ("bKeyDown", wintypes.BOOL),
                ("wRepeatCount", wintypes.WORD),
                ("wVirtualKeyCode", wintypes.WORD),
                ("wVirtualScanCode", wintypes.WORD),
                ("uChar", CharacterUnion),
                ("dwControlKeyState", wintypes.DWORD),
            ]

        class WindowBufferSizeEvent(ctypes.Structure):
            _fields_ = [("dwSize", Coordinate)]

        class EventUnion(ctypes.Union):
            _fields_ = [
                ("KeyEvent", KeyEvent),
                ("WindowBufferSizeEvent", WindowBufferSizeEvent),
            ]

        class InputRecord(ctypes.Structure):
            _fields_ = [("EventType", wintypes.WORD), ("Event", EventUnion)]

        self._ctypes = ctypes
        self._handle = handle
        self._record_type = InputRecord
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        self._kernel32.WaitForSingleObject.restype = wintypes.DWORD
        self._kernel32.ReadConsoleInputW.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(InputRecord),
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        ]
        self._kernel32.ReadConsoleInputW.restype = wintypes.BOOL

    def __call__(self, timeout: float, max_records: int) -> Iterable[object] | None:
        wait_result = self._kernel32.WaitForSingleObject(
            self._handle,
            int(max(0.0, timeout) * 1000),
        )
        if wait_result == 0x00000102:  # WAIT_TIMEOUT
            return None
        if wait_result != 0:
            error = self._ctypes.get_last_error()
            raise OSError(error, "WaitForSingleObject failed")
        records = (self._record_type * max_records)()
        count = self._ctypes.c_uint32()
        if not self._kernel32.ReadConsoleInputW(
            self._handle,
            records,
            max_records,
            self._ctypes.byref(count),
        ):
            error = self._ctypes.get_last_error()
            raise OSError(error, "ReadConsoleInputW failed")
        return records[: count.value]
