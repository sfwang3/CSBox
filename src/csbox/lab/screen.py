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
        elif cell.width > columns - column:
            # A glyph without enough trailing cells cannot be represented by
            # the fixed domain grid. The private screen normally wraps first;
            # this replacement also protects snapshots restored by old pyte.
            cells[column] = replace(cell, character="\N{REPLACEMENT CHARACTER}", width=1)
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
