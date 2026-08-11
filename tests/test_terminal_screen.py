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


def assert_valid_grid(snapshot: TerminalSnapshot) -> None:
    assert len(snapshot.cells) == snapshot.rows
    for row in snapshot.cells:
        assert len(row) == snapshot.columns
        for column, cell in enumerate(row):
            assert cell.width in {0, 1, 2}
            if cell.width == 2:
                assert column + 1 < snapshot.columns
                assert row[column + 1].width == 0
                assert row[column + 1].character == ""
            elif cell.width == 0:
                assert column > 0
                assert row[column - 1].width == 2


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
    assert_valid_grid(snapshot)


def test_overwriting_either_half_of_a_wide_cell_clears_only_the_orphan() -> None:
    overwritten_lead = TerminalEmulator(columns=4, rows=2)
    overwritten_lead.apply(event(1, 0.1, TerminalEventType.OUTPUT, "中".encode()))
    overwritten_lead.apply(event(2, 0.2, TerminalEventType.OUTPUT, b"\x1b[1;1HA"))
    lead_snapshot = overwritten_lead.snapshot()

    assert lead_snapshot.cells[0][0].character == "A"
    assert lead_snapshot.cells[0][1] == TerminalCell()
    assert_valid_grid(lead_snapshot)

    overwritten_continuation = TerminalEmulator(columns=4, rows=2)
    overwritten_continuation.apply(event(1, 0.1, TerminalEventType.OUTPUT, "中".encode()))
    overwritten_continuation.apply(event(2, 0.2, TerminalEventType.OUTPUT, b"\x1b[1;2HA"))
    continuation_snapshot = overwritten_continuation.snapshot()

    assert continuation_snapshot.cells[0][0] == TerminalCell()
    assert continuation_snapshot.cells[0][1].character == "A"
    assert_valid_grid(continuation_snapshot)


def test_resize_shrink_then_grow_does_not_revive_a_truncated_wide_pair() -> None:
    emulator = TerminalEmulator(columns=4, rows=2)
    emulator.apply(event(1, 0.1, TerminalEventType.OUTPUT, b"ab" + "中".encode()))

    emulator.apply(event(2, 0.2, TerminalEventType.RESIZE, TerminalSize(3, 2)))
    shrunk = emulator.snapshot()
    assert shrunk.cells[0][2] == TerminalCell()
    assert_valid_grid(shrunk)

    emulator.apply(event(3, 0.3, TerminalEventType.RESIZE, TerminalSize(4, 2)))
    grown = emulator.snapshot()
    assert grown.cells[0][2:] == (TerminalCell(), TerminalCell())
    assert_valid_grid(grown)


def test_emulator_advances_timeline_for_non_screen_events_without_changing_grid() -> None:
    emulator = TerminalEmulator(columns=8, rows=2)
    before = emulator.snapshot()

    emulator.apply(event(1, 1.0, TerminalEventType.INPUT, b"secret"))
    after_input = emulator.snapshot()
    emulator.apply(event(2, 2.0, TerminalEventType.MARK, "mark"))
    after_mark = emulator.snapshot()
    emulator.apply(event(3, 3.0, TerminalEventType.EXIT, 0))
    after_exit = emulator.snapshot()

    assert after_input.cells == after_mark.cells == after_exit.cells == before.cells
    assert after_input.relative_time == 1.0
    assert after_mark.relative_time == 2.0
    assert after_exit.relative_time == 3.0


@pytest.mark.parametrize(
    ("prefix", "tail"),
    [
        (b"\x1b[31", b"mX"),
        (b"\xe4", b"\xb8\xad"),
    ],
    ids=("split-csi", "split-utf8"),
)
def test_snapshot_restore_preserves_incomplete_parser_and_utf8_state(
    prefix: bytes, tail: bytes
) -> None:
    continuous = TerminalEmulator(columns=8, rows=2)
    continuous.apply(event(1, 1.0, TerminalEventType.OUTPUT, prefix))
    checkpoint = continuous.snapshot()
    continuous.apply(event(2, 2.0, TerminalEventType.OUTPUT, tail))

    restored = TerminalEmulator(columns=1, rows=1)
    restored.restore(checkpoint)
    restored.apply(event(2, 2.0, TerminalEventType.OUTPUT, tail))

    assert restored.snapshot() == continuous.snapshot()
