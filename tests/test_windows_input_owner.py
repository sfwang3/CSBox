from __future__ import annotations

import os
import runpy
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from csbox.core.events import TerminalSize
from csbox.lab import proxy as proxy_module
from csbox.lab.keymap import CaptureKeyMatcher
from csbox.lab.proxy import FileInputAdapter, RawTerminalState
from csbox.lab.windows_input import (
    KEY_EVENT,
    WINDOW_BUFFER_SIZE_EVENT,
    WindowsConsoleInputOwner,
    decode_console_records,
)

F12_VK = 0x7B
SPACE_VK = 0x20
NUL_VK = 0x32
LEFT_CTRL_PRESSED = 0x0008
SHIFT_PRESSED = 0x0010


def test_manual_probe_redacts_ordinary_character_content() -> None:
    probe = runpy.run_path(str(Path(__file__).with_name("manual_windows_input_probe.py")))
    record = probe["InputRecord"]()
    record.EventType = probe["KEY_EVENT"]
    record.Event.KeyEvent.bKeyDown = True
    record.Event.KeyEvent.wRepeatCount = 1
    record.Event.KeyEvent.wVirtualKeyCode = 0x41
    record.Event.KeyEvent.wVirtualScanCode = 0x1E
    record.Event.KeyEvent.uChar.UnicodeChar = "a"

    summary = probe["summarize"](record)

    assert summary is not None
    assert summary["category"] == "ordinary-character"
    assert set(summary) == {"eventType", "category", "keyDown", "repeatCount"}


def key_record(
    character: str = "",
    *,
    virtual_key: int = 0,
    scan_code: int = 0,
    control_key_state: int = 0,
    key_down: bool = True,
    repeat_count: int = 1,
) -> object:
    return SimpleNamespace(
        EventType=KEY_EVENT,
        Event=SimpleNamespace(
            KeyEvent=SimpleNamespace(
                bKeyDown=key_down,
                wRepeatCount=repeat_count,
                wVirtualKeyCode=virtual_key,
                wVirtualScanCode=scan_code,
                uChar=SimpleNamespace(UnicodeChar=character),
                dwControlKeyState=control_key_state,
            )
        ),
    )


def resize_record(columns: int, rows: int) -> object:
    return SimpleNamespace(
        EventType=WINDOW_BUFFER_SIZE_EVENT,
        Event=SimpleNamespace(
            WindowBufferSizeEvent=SimpleNamespace(
                dwSize=SimpleNamespace(X=columns, Y=rows),
            )
        ),
    )


def test_file_input_adapter_exposes_one_resize_notice_channel() -> None:
    adapter = FileInputAdapter(stream=object())

    assert adapter.drain_resize_notices() == ()


def test_non_windows_resize_drain_never_loads_win32_console_api(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FileLike:
        def fileno(self) -> int:
            return 0

    def fail_if_called(file_descriptor: int) -> int:
        del file_descriptor
        raise AssertionError("Win32 console API must not load on Linux")

    monkeypatch.setattr(proxy_module, "_windows_console_handle", fail_if_called)
    adapter = FileInputAdapter(stream=FileLike())

    assert adapter.drain_resize_notices() == ()


def test_decode_console_records_combines_key_and_resize_notices_in_input_order() -> None:
    records = [
        key_record("中"),
        resize_record(120, 35),
        key_record(virtual_key=F12_VK),
        key_record(virtual_key=SPACE_VK, control_key_state=LEFT_CTRL_PRESSED),
        key_record("ignored", key_down=False),
    ]

    key_bytes, resize_notices = decode_console_records(records)

    assert key_bytes == "中".encode() + b"\x1b[24~" + b"\x00"
    assert resize_notices == (TerminalSize(120, 35),)


def test_decode_console_records_reassembles_utf16_surrogate_key_pair() -> None:
    key_bytes, resize_notices = decode_console_records([key_record("\ud83d"), key_record("\ude00")])

    assert key_bytes == "😀".encode()
    assert resize_notices == ()


def test_decode_console_records_maps_native_f12_scan_code_without_virtual_key() -> None:
    key_bytes, resize_notices = decode_console_records([key_record(scan_code=0x58, virtual_key=0)])

    assert key_bytes == b"\x1b[24~"
    assert resize_notices == ()


def test_decode_console_records_maps_ctrl_space_scan_code_fallback() -> None:
    key_bytes, resize_notices = decode_console_records(
        [key_record(scan_code=0x39, virtual_key=0, control_key_state=LEFT_CTRL_PRESSED)]
    )

    assert key_bytes == b"\x00"
    assert resize_notices == ()


def test_decode_console_records_prefers_physical_ctrl_space_over_translated_space() -> None:
    key_bytes, resize_notices = decode_console_records(
        [
            key_record(
                " ",
                virtual_key=SPACE_VK,
                scan_code=0x39,
                control_key_state=LEFT_CTRL_PRESSED,
            )
        ]
    )

    assert key_bytes == b"\x00"
    assert resize_notices == ()


def test_decode_console_records_maps_windows_terminal_synthesized_nul_transport() -> None:
    key_bytes, resize_notices = decode_console_records(
        [
            key_record(
                "\x00",
                virtual_key=NUL_VK,
                scan_code=0,
                control_key_state=LEFT_CTRL_PRESSED | SHIFT_PRESSED,
            )
        ]
    )

    assert key_bytes == b"\x00"
    assert resize_notices == ()


def test_windows_terminal_f12_and_ctrl_space_transport_reach_matcher_without_forwarding() -> None:
    # WT 1.24 sends F12 as VT text, while its console host reconstructs NUL
    # using VkKeyScanW(0): VK_2/scan 0/Left Ctrl+Shift on the native gate machine.
    records = [key_record(character) for character in "\x1b[24~"]
    records.extend(
        [
            key_record(
                "\x00",
                virtual_key=NUL_VK,
                scan_code=0,
                control_key_state=LEFT_CTRL_PRESSED | SHIFT_PRESSED,
            ),
        ]
    )

    key_bytes, resize_notices = decode_console_records(records)
    match = CaptureKeyMatcher("f12").feed(key_bytes)

    assert key_bytes == b"\x1b[24~\x00"
    assert resize_notices == ()
    assert match.forwarded == b""
    assert match.captures == 2
    assert match.actions == (("capture", b""), ("capture", b""))


def test_coalesced_repeat_count_and_key_up_do_not_duplicate_ctrl_space_capture() -> None:
    key_bytes, resize_notices = decode_console_records(
        [
            key_record(
                " ",
                virtual_key=SPACE_VK,
                scan_code=0x39,
                control_key_state=LEFT_CTRL_PRESSED,
                repeat_count=5,
            ),
            key_record(
                " ",
                virtual_key=SPACE_VK,
                scan_code=0x39,
                control_key_state=LEFT_CTRL_PRESSED,
                key_down=False,
            ),
        ]
    )
    match = CaptureKeyMatcher("f12").feed(key_bytes)

    assert key_bytes == b"\x00"
    assert resize_notices == ()
    assert match.forwarded == b""
    assert match.captures == 1
    assert match.actions == (("capture", b""),)


def test_physical_ctrl_number_nul_is_not_mistaken_for_terminal_synthesized_nul() -> None:
    key_bytes, resize_notices = decode_console_records(
        [
            key_record(
                "\x00",
                virtual_key=NUL_VK,
                scan_code=0x03,
                control_key_state=LEFT_CTRL_PRESSED | SHIFT_PRESSED,
            )
        ]
    )

    assert key_bytes == b""
    assert resize_notices == ()


def test_native_key_down_sequences_reach_capture_matcher_and_key_up_is_consumed() -> None:
    records = [
        key_record(virtual_key=F12_VK, scan_code=0x58),
        key_record(virtual_key=F12_VK, scan_code=0x58, key_down=False),
        key_record(
            " ",
            virtual_key=SPACE_VK,
            scan_code=0x39,
            control_key_state=LEFT_CTRL_PRESSED,
        ),
        key_record(
            " ",
            virtual_key=SPACE_VK,
            scan_code=0x39,
            control_key_state=LEFT_CTRL_PRESSED,
            key_down=False,
        ),
    ]

    key_bytes, resize_notices = decode_console_records(records)
    match = CaptureKeyMatcher("f12").feed(key_bytes)

    assert key_bytes == b"\x1b[24~\x00"
    assert resize_notices == ()
    assert match.forwarded == b""
    assert match.captures == 2
    assert match.actions == (("capture", b""), ("capture", b""))


def test_windows_console_owner_drains_resize_notices_without_a_second_reader() -> None:
    batches = iter(
        [
            [key_record("a"), resize_record(100, 30)],
            [key_record("b")],
            None,
        ]
    )
    reader_calls: list[tuple[float, int]] = []

    def read_records(timeout: float, max_records: int) -> object:
        reader_calls.append((timeout, max_records))
        return next(batches)

    owner = WindowsConsoleInputOwner(record_reader=read_records)

    assert owner.read(timeout=0.05, max_bytes=32) == b"a"
    assert owner.drain_resize_notices() == (TerminalSize(100, 30),)
    assert owner.read(timeout=0.05, max_bytes=32) == b"b"
    assert owner.drain_resize_notices() == ()
    assert owner.read(timeout=0.05, max_bytes=32) is None
    assert len(reader_calls) == 3


def test_windows_console_owner_shutdown_is_bounded_and_stops_future_native_reads() -> None:
    reader_calls: list[tuple[float, int]] = []

    def read_records(timeout: float, max_records: int) -> None:
        reader_calls.append((timeout, max_records))
        return None

    owner = WindowsConsoleInputOwner(record_reader=read_records)

    assert owner.read(timeout=0.05) is None
    owner.close()
    assert owner.read(timeout=30.0) == b""
    assert reader_calls == [(0.05, 128)]


def test_file_input_adapter_routes_keys_and_resize_to_the_same_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = WindowsConsoleInputOwner(
        record_reader=lambda timeout, max_records: [
            key_record("a"),
            resize_record(100, 30),
        ]
    )
    adapter = FileInputAdapter(stream=object(), windows_owner=owner)
    monkeypatch.setattr(proxy_module.os, "name", "nt")
    monkeypatch.setattr(proxy_module, "_is_windows_console", lambda stream: True)

    assert adapter.read(timeout=0.05) == b"a"
    assert adapter.drain_resize_notices() == (TerminalSize(100, 30),)


def test_windows_console_owner_failure_is_explicit_instead_of_silent_msvcrt_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(proxy_module.os, "name", "nt")
    monkeypatch.setattr(proxy_module, "_is_windows_console", lambda stream: True)
    monkeypatch.setattr(
        proxy_module,
        "_windows_console_handle",
        lambda file_descriptor: (_ for _ in ()).throw(OSError("invalid console")),
    )
    adapter = FileInputAdapter(stream=object())

    with pytest.raises(RuntimeError, match="authoritative Windows console input owner"):
        adapter.read(timeout=0.0)


def test_raw_windows_state_enables_window_buffer_size_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FileLike:
        def fileno(self) -> int:
            return 0

    mode_writes: list[int] = []
    monkeypatch.setattr(proxy_module.os, "name", "nt")
    monkeypatch.setattr(proxy_module, "_windows_console_handle", lambda fd: 99)
    monkeypatch.setattr(proxy_module, "_get_windows_console_mode", lambda handle: 0)
    monkeypatch.setattr(
        proxy_module,
        "_set_windows_console_mode",
        lambda handle, mode: mode_writes.append(mode),
    )

    RawTerminalState(FileLike())._enter_windows_console()

    assert mode_writes
    assert mode_writes[0] & 0x0008


def test_raw_windows_state_uses_native_key_records_and_returns_mouse_to_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FileLike:
        def fileno(self) -> int:
            return 0

    original_mode = 0x03FF
    mode_writes: list[int] = []
    state = RawTerminalState(FileLike())
    monkeypatch.setattr(proxy_module.os, "name", "nt")
    monkeypatch.setattr(proxy_module, "_windows_console_handle", lambda fd: 99)
    monkeypatch.setattr(proxy_module, "_get_windows_console_mode", lambda handle: original_mode)
    monkeypatch.setattr(
        proxy_module,
        "_set_windows_console_mode",
        lambda handle, mode: mode_writes.append(mode),
    )
    monkeypatch.setattr(state, "_enter_windows_output", lambda: None)

    state.__enter__()
    state.__exit__(None, None, None)

    expected_during = original_mode & ~(0x0001 | 0x0002 | 0x0004 | 0x0010 | 0x0040 | 0x0200)
    expected_during |= 0x0008 | 0x0080
    assert mode_writes == [expected_during, original_mode]


def test_raw_windows_state_restores_exact_input_mode_after_session_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FileLike:
        def fileno(self) -> int:
            return 0

    original_mode = 0x01F7
    mode_writes: list[int] = []
    state = RawTerminalState(FileLike())
    monkeypatch.setattr(proxy_module.os, "name", "nt")
    monkeypatch.setattr(proxy_module, "_windows_console_handle", lambda fd: 99)
    monkeypatch.setattr(proxy_module, "_get_windows_console_mode", lambda handle: original_mode)
    monkeypatch.setattr(
        proxy_module,
        "_set_windows_console_mode",
        lambda handle, mode: mode_writes.append(mode),
    )
    monkeypatch.setattr(state, "_enter_windows_output", lambda: None)

    with pytest.raises(RuntimeError, match="session failed"), state:
        raise RuntimeError("session failed")

    assert mode_writes[-1] == original_mode


@pytest.mark.windows
def test_native_windows_console_mode_is_restored_exactly_when_console_is_attached() -> None:
    if os.name != "nt":
        pytest.skip("native Windows console mode regression only runs on Windows")
    if not sys.stdin.isatty():
        pytest.skip("native Windows console mode regression requires attached console input")

    handle = proxy_module._windows_console_handle(sys.stdin.fileno())
    before = proxy_module._get_windows_console_mode(handle)
    with RawTerminalState(sys.stdin):
        during = proxy_module._get_windows_console_mode(handle)
    after = proxy_module._get_windows_console_mode(handle)

    expected_during = before & ~(0x0001 | 0x0002 | 0x0004 | 0x0010 | 0x0040 | 0x0200)
    expected_during |= 0x0008 | 0x0080
    assert during == expected_during
    assert after == before


def test_raw_windows_state_enables_virtual_terminal_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FileLike:
        def fileno(self) -> int:
            return 0

    mode_writes: list[int] = []
    monkeypatch.setattr(proxy_module.os, "name", "nt")
    monkeypatch.setattr(
        proxy_module,
        "_windows_console_handle",
        lambda file_descriptor: 99 if file_descriptor == 0 else 100,
    )
    monkeypatch.setattr(proxy_module, "_get_windows_console_mode", lambda handle: 0)
    monkeypatch.setattr(
        proxy_module,
        "_set_windows_console_mode",
        lambda handle, mode: mode_writes.append(mode),
    )

    RawTerminalState(FileLike())._enter_windows_console()

    assert any(mode & 0x0004 for mode in mode_writes)
