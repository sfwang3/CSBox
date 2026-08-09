from __future__ import annotations

import unicodedata
from dataclasses import dataclass, replace
from typing import Any

import pyte
from pyte import modes
from wcwidth import wcwidth

from csbox.core.display_width import display_width
from csbox.core.events import TerminalEvent, TerminalEventType, TerminalSize


@dataclass(frozen=True, slots=True)
class TerminalCell:
    character: str = " "
    width: int = 1
    foreground: str = "default"
    background: str = "default"
    bold: bool = False
    italic: bool = False
    underline: bool = False
    strikethrough: bool = False
    reverse: bool = False


@dataclass(frozen=True, slots=True)
class TerminalCursor:
    row: int = 0
    column: int = 0
    visible: bool = True


@dataclass(frozen=True, slots=True)
class TerminalSnapshot:
    rows: int
    columns: int
    cells: tuple[tuple[TerminalCell, ...], ...]
    cursor: TerminalCursor
    relative_time: float = 0.0


class TerminalEmulator:
    """Keep terminal state while exposing only immutable CSBox values."""

    def __init__(self, *, columns: int, rows: int) -> None:
        if columns <= 0 or rows <= 0:
            raise ValueError("terminal dimensions must be positive")
        self._screen = _CSBoxScreen(columns, rows)
        self._stream = pyte.ByteStream(self._screen)
        self._relative_time = 0.0

    @property
    def columns(self) -> int:
        return self._screen.columns

    @property
    def rows(self) -> int:
        return self._screen.lines

    def apply(self, event: TerminalEvent) -> None:
        if not isinstance(event, TerminalEvent):
            raise TypeError("event must be a TerminalEvent")
        if event.type is TerminalEventType.OUTPUT:
            assert isinstance(event.payload, bytes)
            self._stream.feed(event.payload)
            self._relative_time = event.relative_time
        elif event.type is TerminalEventType.RESIZE:
            assert isinstance(event.payload, TerminalSize)
            self._screen.resize(lines=event.payload.rows, columns=event.payload.columns)
            self._relative_time = event.relative_time

    handle = apply
    __call__ = apply

    def snapshot(self) -> TerminalSnapshot:
        return _snapshot_from_pyte(self._screen, self._relative_time)

    def restore(self, snapshot: TerminalSnapshot) -> None:
        """Replace emulator state with an immutable domain snapshot."""

        _validate_snapshot(snapshot)
        screen = _CSBoxScreen(snapshot.columns, snapshot.rows)
        for row_index, row in enumerate(snapshot.cells):
            for column_index, cell in enumerate(row):
                screen.buffer[row_index][column_index] = screen.default_char._replace(
                    data=cell.character,
                    fg=cell.foreground,
                    bg=cell.background,
                    bold=cell.bold,
                    italics=cell.italic,
                    underscore=cell.underline,
                    strikethrough=cell.strikethrough,
                    reverse=cell.reverse,
                )
        screen.cursor.y = snapshot.cursor.row
        screen.cursor.x = snapshot.cursor.column
        screen.cursor.hidden = not snapshot.cursor.visible
        screen.dirty = set(range(snapshot.rows))
        self._screen = screen
        self._stream = pyte.ByteStream(screen)
        self._relative_time = snapshot.relative_time


def _snapshot_from_pyte(screen: pyte.Screen, relative_time: float) -> TerminalSnapshot:
    """The one boundary where pyte quirks become stable domain values."""

    rows = screen.lines
    columns = screen.columns
    cells = tuple(_row_from_pyte(screen, row, columns) for row in range(rows))
    # pyte can temporarily place x one cell beyond the edge after drawing a
    # double-width glyph in the final column. Do not leak that internal state.
    cursor = TerminalCursor(
        row=min(max(screen.cursor.y, 0), rows - 1),
        column=min(max(screen.cursor.x, 0), columns - 1),
        visible=not screen.cursor.hidden,
    )
    return TerminalSnapshot(
        rows=rows,
        columns=columns,
        cells=cells,
        cursor=cursor,
        relative_time=relative_time,
    )


def _validate_snapshot(snapshot: TerminalSnapshot) -> None:
    if not isinstance(snapshot, TerminalSnapshot):
        raise TypeError("snapshot must be a TerminalSnapshot")
    if snapshot.rows <= 0 or snapshot.columns <= 0:
        raise ValueError("snapshot dimensions must be positive")
    if len(snapshot.cells) != snapshot.rows or any(
        len(row) != snapshot.columns for row in snapshot.cells
    ):
        raise ValueError("snapshot cell geometry does not match its dimensions")
    if not 0 <= snapshot.cursor.row < snapshot.rows:
        raise ValueError("snapshot cursor row is outside the terminal")
    if not 0 <= snapshot.cursor.column < snapshot.columns:
        raise ValueError("snapshot cursor column is outside the terminal")
    for row in snapshot.cells:
        for column, cell in enumerate(row):
            if cell.width not in {0, 1, 2}:
                raise ValueError("snapshot contains an invalid cell width")
            if cell.width == 2 and (
                column + 1 >= snapshot.columns
                or row[column + 1].width != 0
                or row[column + 1].character != ""
            ):
                raise ValueError("snapshot contains an incomplete wide cell")
            if cell.width == 0 and (column == 0 or row[column - 1].width != 2):
                raise ValueError("snapshot contains an orphan continuation cell")


def _cell_from_pyte(character: Any) -> TerminalCell:
    # pyte uses an empty character for the continuation cell of a wide glyph.
    data = str(character.data)
    width = 0 if data == "" else display_width(data)
    return TerminalCell(
        character=data,
        width=width,
        foreground=str(character.fg),
        background=str(character.bg),
        bold=bool(character.bold),
        italic=bool(character.italics),
        underline=bool(character.underscore),
        strikethrough=bool(character.strikethrough),
        reverse=bool(character.reverse),
    )


def _row_from_pyte(screen: pyte.Screen, row: int, columns: int) -> tuple[TerminalCell, ...]:
    cells = [_cell_from_pyte(screen.buffer[row][column]) for column in range(columns)]
    for column, cell in enumerate(cells):
        if cell.character and cell.width == 0:
            lead_column = column - 1
            while lead_column >= 0 and cells[lead_column].width == 0:
                lead_column -= 1
            if lead_column >= 0:
                lead = cells[lead_column]
                combined = unicodedata.normalize("NFC", lead.character + cell.character)
                cells[lead_column] = replace(lead, character=combined)
                cells[column] = replace(cell, character="")
    for column, cell in enumerate(cells):
        if cell.width == 2:
            if column + 1 >= columns or cells[column + 1].width != 0:
                cells[column] = TerminalCell()
        elif cell.width == 0 and (column == 0 or cells[column - 1].width != 2):
            cells[column] = TerminalCell()
    return tuple(cells)


class _CSBoxScreen(pyte.Screen):
    """Contain pyte width edge-case patches behind the domain adapter."""

    def draw(self, data: str) -> None:
        for character in data:
            character_width = wcwidth(character)
            if (
                character_width == 2
                and self.cursor.x == self.columns - 1
                and modes.DECAWM in self.mode
            ):
                self.carriage_return()
                self.linefeed()
            if (
                character_width == 0
                and unicodedata.combining(character)
                and self._combine_with_wide_lead(character)
            ):
                continue
            super().draw(character)
            self._repair_wide_row(self.cursor.y)

    def resize(self, lines: int | None = None, columns: int | None = None) -> None:
        super().resize(lines=lines, columns=columns)
        for row in range(self.lines):
            self._repair_wide_row(row)

    def _combine_with_wide_lead(self, combining_character: str) -> bool:
        row = self.cursor.y
        column = self.cursor.x - 1
        if column < 0:
            row -= 1
            column = self.columns - 1
        if row < 0 or column < 1:
            return False
        line = self.buffer[row]
        if line[column].data != "" or display_width(line[column - 1].data) != 2:
            return False
        lead = line[column - 1]
        combined = unicodedata.normalize("NFC", lead.data + combining_character)
        line[column - 1] = lead._replace(data=combined)
        self.dirty.add(row)
        return True

    def _repair_wide_row(self, row: int) -> None:
        line = self.buffer[row]
        repaired = False
        for column in range(self.columns):
            data = line[column].data
            width = display_width(data)
            if width == 2:
                if column + 1 >= self.columns or line[column + 1].data != "":
                    line[column] = self.default_char
                    repaired = True
            elif data == "":
                if column == 0 or display_width(line[column - 1].data) != 2:
                    line[column] = self.default_char
                    repaired = True
            elif width == 0:
                line[column] = self.default_char
                repaired = True
        if repaired:
            self.dirty.add(row)
