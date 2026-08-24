"""Bounded manual probe for the real Windows console input path.

Run only in an attached Windows Terminal tab:

    uv run python tests/manual_windows_input_probe.py

The probe prints mode bits and bounded event metadata. It never writes events to
a Lab session and never prints the literal text of ordinary character input.
"""

from __future__ import annotations

import ctypes
import json
import os
import sys
import time
from ctypes import wintypes

from csbox.lab.proxy import (
    RawTerminalState,
    _get_windows_console_mode,
    _windows_console_handle,
)
from csbox.lab.windows_input import decode_console_records

KEY_EVENT = 0x0001
MOUSE_EVENT = 0x0002
WINDOW_BUFFER_SIZE_EVENT = 0x0004

VK_RETURN = 0x0D
VK_F12 = 0x7B

MOUSE_WHEELED = 0x0004
MOUSE_HWHEELED = 0x0008

MAX_EVENTS = 64
PROBE_SECONDS = 30.0

MODE_FLAGS = (
    (0x0001, "PROCESSED_INPUT"),
    (0x0002, "LINE_INPUT"),
    (0x0004, "ECHO_INPUT"),
    (0x0008, "WINDOW_INPUT"),
    (0x0010, "MOUSE_INPUT"),
    (0x0020, "INSERT_MODE"),
    (0x0040, "QUICK_EDIT_MODE"),
    (0x0080, "EXTENDED_FLAGS"),
    (0x0100, "AUTO_POSITION"),
    (0x0200, "VIRTUAL_TERMINAL_INPUT"),
)


class Coordinate(ctypes.Structure):
    _fields_ = [("X", wintypes.SHORT), ("Y", wintypes.SHORT)]


class CharacterUnion(ctypes.Union):
    _fields_ = [("UnicodeChar", wintypes.WCHAR), ("AsciiChar", ctypes.c_char)]


class KeyEventRecord(ctypes.Structure):
    _fields_ = [
        ("bKeyDown", wintypes.BOOL),
        ("wRepeatCount", wintypes.WORD),
        ("wVirtualKeyCode", wintypes.WORD),
        ("wVirtualScanCode", wintypes.WORD),
        ("uChar", CharacterUnion),
        ("dwControlKeyState", wintypes.DWORD),
    ]


class MouseEventRecord(ctypes.Structure):
    _fields_ = [
        ("dwMousePosition", Coordinate),
        ("dwButtonState", wintypes.DWORD),
        ("dwControlKeyState", wintypes.DWORD),
        ("dwEventFlags", wintypes.DWORD),
    ]


class WindowBufferSizeRecord(ctypes.Structure):
    _fields_ = [("dwSize", Coordinate)]


class EventUnion(ctypes.Union):
    _fields_ = [
        ("KeyEvent", KeyEventRecord),
        ("MouseEvent", MouseEventRecord),
        ("WindowBufferSizeEvent", WindowBufferSizeRecord),
    ]


class InputRecord(ctypes.Structure):
    _fields_ = [("EventType", wintypes.WORD), ("Event", EventUnion)]


class NativeRecordReader:
    def __init__(self, handle: int) -> None:
        self.handle = handle
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        self.kernel32.WaitForSingleObject.restype = wintypes.DWORD
        self.kernel32.ReadConsoleInputW.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(InputRecord),
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        ]
        self.kernel32.ReadConsoleInputW.restype = wintypes.BOOL

    def read(self, timeout: float) -> list[InputRecord]:
        wait_result = self.kernel32.WaitForSingleObject(
            self.handle,
            int(max(0.0, timeout) * 1000),
        )
        if wait_result == 0x00000102:  # WAIT_TIMEOUT
            return []
        if wait_result != 0:
            raise ctypes.WinError(ctypes.get_last_error())
        records = (InputRecord * MAX_EVENTS)()
        count = wintypes.DWORD()
        if not self.kernel32.ReadConsoleInputW(
            self.handle,
            records,
            MAX_EVENTS,
            ctypes.byref(count),
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        return list(records[: count.value])


def format_mode(mode: int) -> dict[str, object]:
    return {
        "value": f"0x{mode:08x}",
        "flags": [name for bit, name in MODE_FLAGS if mode & bit],
    }


def summarize(record: InputRecord) -> dict[str, object] | None:
    event_type = int(record.EventType)
    if event_type == KEY_EVENT:
        event = record.Event.KeyEvent
        key_down = bool(event.bKeyDown)
        virtual_key = int(event.wVirtualKeyCode)
        scan_code = int(event.wVirtualScanCode)
        control_state = int(event.dwControlKeyState)
        character = event.uChar.UnicodeChar or "\x00"
        if virtual_key == VK_F12 or scan_code == 0x58:
            category = "F12"
        elif virtual_key == VK_RETURN:
            category = "Enter"
        elif key_down and character != "\x00":
            category = "ordinary-character"
        else:
            return None
        summary: dict[str, object] = {
            "eventType": "KEY_EVENT",
            "category": category,
            "keyDown": key_down,
            "repeatCount": int(event.wRepeatCount),
        }
        if category == "ordinary-character":
            return summary
        encoded, _ = decode_console_records([record])
        summary.update(
            {
                "virtualKeyCode": f"0x{virtual_key:04x}",
                "virtualScanCode": f"0x{scan_code:04x}",
                "controlKeyState": f"0x{control_state:08x}",
                "unicodeChar": f"U+{ord(character):04X}",
                "encodedHex": encoded.hex(),
            }
        )
        return summary
    if event_type == MOUSE_EVENT:
        event = record.Event.MouseEvent
        flags = int(event.dwEventFlags)
        if not flags & (MOUSE_WHEELED | MOUSE_HWHEELED):
            return None
        return {
            "eventType": "MOUSE_EVENT",
            "category": "wheel",
            "eventFlags": f"0x{flags:08x}",
            "controlKeyState": f"0x{int(event.dwControlKeyState):08x}",
        }
    if event_type == WINDOW_BUFFER_SIZE_EVENT:
        size = record.Event.WindowBufferSizeEvent.dwSize
        return {
            "eventType": "WINDOW_BUFFER_SIZE_EVENT",
            "category": "resize",
            "columns": int(size.X),
            "rows": int(size.Y),
        }
    return None


def main() -> int:
    if os.name != "nt":
        print("PHYSICAL INPUT: PENDING MANUAL (run in native Windows Terminal)")
        return 2
    if not sys.stdin.isatty():
        print("PHYSICAL INPUT: PENDING MANUAL (attached console input required)")
        return 2

    handle = _windows_console_handle(sys.stdin.fileno())
    reader = NativeRecordReader(handle)
    before = _get_windows_console_mode(handle)
    print(json.dumps({"modeBefore": format_mode(before)}, ensure_ascii=False), flush=True)
    print(
        "Press F12, one ordinary character, use the wheel, resize, then press Enter.",
        flush=True,
    )
    seen: set[str] = set()
    emitted = 0
    with RawTerminalState(sys.stdin):
        during = _get_windows_console_mode(handle)
        print(json.dumps({"modeDuring": format_mode(during)}, ensure_ascii=False), flush=True)
        deadline = time.monotonic() + PROBE_SECONDS
        finished = False
        while time.monotonic() < deadline and emitted < MAX_EVENTS and not finished:
            for record in reader.read(min(0.1, max(0.0, deadline - time.monotonic()))):
                summary = summarize(record)
                if summary is None:
                    continue
                category = str(summary["category"])
                if category == "ordinary-character" and category in seen:
                    continue
                print(json.dumps(summary, ensure_ascii=False), flush=True)
                emitted += 1
                if bool(summary.get("keyDown", True)):
                    seen.add(category)
                if category == "Enter" and bool(summary.get("keyDown")):
                    finished = True
                    break
    after = _get_windows_console_mode(handle)
    print(json.dumps({"modeAfter": format_mode(after)}, ensure_ascii=False), flush=True)
    print(json.dumps({"observed": sorted(seen)}, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
