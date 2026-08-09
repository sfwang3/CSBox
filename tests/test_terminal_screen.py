from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from csbox.core.events import TerminalEvent, TerminalEventType, TerminalSize
from csbox.lab.screen import TerminalCell, TerminalCursor, TerminalEmulator, TerminalSnapshot


def event(
    sequence: int,
    relative_time: float,
    event_type: TerminalEventType,
    payload: bytes | str | TerminalSize | int | None,
) -> TerminalEvent:
    return TerminalEvent(
        sequence=sequence,
        monotonic_time=100.0 + relative_time,
        relative_time=relative_time,
        type=event_type,
        payload=payload,
    )


def row_text(snapshot: TerminalSnapshot, row: int) -> str:
    return "".join(cell.character for cell in snapshot.cells[row] if cell.width != 0)


def test_snapshot_converts_ascii_cjk_ansi_colors_and_attributes_to_domain_types() -> None:
    emulator = TerminalEmulator(columns=16, rows=3)
    emulator.apply(
        event(
            1,
            0.25,
            TerminalEventType.OUTPUT,
            (b"A\x1b[31;1;4;7mB\x1b[0m\x1b[38;5;196mC\x1b[38;2;1;2;3mD\x1b[0m" + "中文".encode()),
        )
    )

    snapshot = emulator.snapshot()

    assert isinstance(snapshot, TerminalSnapshot)
    assert all(isinstance(cell, TerminalCell) for row in snapshot.cells for cell in row)
    assert isinstance(snapshot.cursor, TerminalCursor)
    assert type(snapshot).__module__ == "csbox.lab.screen"
    assert all(
        type(cell).__module__ == "csbox.lab.screen" for row in snapshot.cells for cell in row
    )
    assert snapshot.cells[0][1] == TerminalCell(
        character="B", foreground="red", bold=True, underline=True, reverse=True
    )
    assert snapshot.cells[0][2].foreground == "ff0000"
    assert snapshot.cells[0][3].foreground == "010203"
    assert snapshot.cells[0][4].character == "中"
    assert snapshot.cells[0][4].width == 2
    assert snapshot.cells[0][5].width == 0
    assert snapshot.relative_time == 0.25
    with pytest.raises(FrozenInstanceError):
        snapshot.cursor.row = 2  # type: ignore[misc]


def test_cursor_movement_erase_line_erase_screen_and_resize() -> None:
    emulator = TerminalEmulator(columns=8, rows=3)
    emulator.apply(event(1, 0.1, TerminalEventType.OUTPUT, b"hello\x1b[2;3HX"))
    moved = emulator.snapshot()

    assert moved.cells[1][2].character == "X"
    assert moved.cursor == TerminalCursor(row=1, column=3, visible=True)

    emulator.apply(event(2, 0.2, TerminalEventType.OUTPUT, b"\x1b[1;1H\x1b[2K"))
    assert row_text(emulator.snapshot(), 0).strip() == ""
    assert emulator.snapshot().cells[1][2].character == "X"

    emulator.apply(event(3, 0.3, TerminalEventType.OUTPUT, b"\x1b[2J"))
    assert all(not row_text(emulator.snapshot(), row).strip() for row in range(3))

    emulator.apply(event(4, 0.4, TerminalEventType.RESIZE, TerminalSize(columns=12, rows=4)))
    resized = emulator.snapshot()
    assert (resized.rows, resized.columns, resized.relative_time) == (4, 12, 0.4)
    assert len(resized.cells) == 4
    assert all(len(row) == 12 for row in resized.cells)


def test_carriage_return_progress_powershell_table_and_chinese_paths() -> None:
    emulator = TerminalEmulator(columns=44, rows=6)
    output = (
        b"10%\r20%\r100%\r\n"
        + "Name        Length\r\n----        ------\r\n报告.txt    128\r\n".encode()
        + "C:\\课程\\计算机网络\\实验一\r\n/home/student/课程/实验一".encode()
    )
    emulator.apply(event(1, 1.0, TerminalEventType.OUTPUT, output))

    snapshot = emulator.snapshot()

    assert row_text(snapshot, 0).startswith("100%")
    assert "Name        Length" in row_text(snapshot, 1)
    assert "报告.txt    128" in row_text(snapshot, 3)
    assert "C:\\课程\\计算机网络\\实验一" in row_text(snapshot, 4)
    assert "/home/student/课程/实验一" in row_text(snapshot, 5)


def test_combining_mark_after_cjk_stays_on_the_wide_lead_cell() -> None:
    emulator = TerminalEmulator(columns=4, rows=2)
    emulator.apply(event(1, 0.1, TerminalEventType.OUTPUT, "中\u0301".encode()))

    snapshot = emulator.snapshot()

    assert snapshot.cells[0][0].character == "中\u0301"
    assert snapshot.cells[0][0].width == 2
    assert snapshot.cells[0][1].character == ""
    assert snapshot.cells[0][1].width == 0


def test_wide_character_at_last_column_wraps_without_invalid_cell_geometry() -> None:
    emulator = TerminalEmulator(columns=4, rows=2)
    emulator.apply(event(1, 0.1, TerminalEventType.OUTPUT, "abc中".encode()))

    snapshot = emulator.snapshot()

    assert snapshot.cells[0][3] == TerminalCell()
    assert snapshot.cells[1][0].character == "中"
    assert snapshot.cells[1][0].width == 2
    assert snapshot.cells[1][1].width == 0


def test_emulator_ignores_non_screen_events() -> None:
    emulator = TerminalEmulator(columns=8, rows=2)
    before = emulator.snapshot()

    emulator.apply(event(1, 1.0, TerminalEventType.INPUT, b"secret"))
    emulator.apply(event(2, 2.0, TerminalEventType.MARK, "mark"))
    emulator.apply(event(3, 3.0, TerminalEventType.EXIT, 0))

    assert emulator.snapshot() == before
