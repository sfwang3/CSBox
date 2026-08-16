from __future__ import annotations

from types import SimpleNamespace

import pytest

from csbox.core.events import TerminalSize
from csbox.lab import proxy as proxy_module
from csbox.lab.proxy import FileInputAdapter, RawTerminalState
from csbox.lab.windows_input import (
    KEY_EVENT,
    WINDOW_BUFFER_SIZE_EVENT,
    WindowsConsoleInputOwner,
    decode_console_records,
)

F12_VK = 0x7B
SPACE_VK = 0x20
LEFT_CTRL_PRESSED = 0x0008


def key_record(
    character: str = "",
    *,
    virtual_key: int = 0,
    scan_code: int = 0,
    control_key_state: int = 0,
    key_down: bool = True,
) -> object:
    return SimpleNamespace(
        EventType=KEY_EVENT,
        Event=SimpleNamespace(
            KeyEvent=SimpleNamespace(
                bKeyDown=key_down,
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
