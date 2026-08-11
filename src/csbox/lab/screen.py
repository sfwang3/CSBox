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
class TerminalAttributes:
    foreground: str = "default"
    background: str = "default"
    bold: bool = False
    italic: bool = False
    underline: bool = False
    strikethrough: bool = False
    reverse: bool = False
    blink: bool = False


@dataclass(frozen=True, slots=True)
class TerminalSavepoint:
    row: int
    column: int
    visible: bool
    attributes: TerminalAttributes
    g0_charset: str
    g1_charset: str
    charset: int
    origin: bool
    wrap: bool


@dataclass(frozen=True, slots=True)
class TerminalEmulatorState:
    """Versioned, pyte-independent state needed to continue a snapshot."""

    version: int
    cursor_row: int
    cursor_column: int
    cursor_attributes: TerminalAttributes
    modes: tuple[int, ...]
    margins: tuple[int, int] | None
    tabstops: tuple[int, ...]
    charset: int
    g0_charset: str
    g1_charset: str
    savepoints: tuple[TerminalSavepoint, ...]
    saved_columns: int | None
    title: str
    icon_name: str
    use_utf8: bool
    pending_bytes: bytes = b""


@dataclass(frozen=True, slots=True)
class TerminalSnapshot:
    rows: int
    columns: int
    cells: tuple[tuple[TerminalCell, ...], ...]
    cursor: TerminalCursor
    relative_time: float = 0.0
    state: TerminalEmulatorState | None = None


class TerminalEmulator:
    """Keep terminal state while exposing only immutable CSBox values."""

    def __init__(self, *, columns: int, rows: int) -> None:
        if columns <= 0 or rows <= 0:
            raise ValueError("terminal dimensions must be positive")
        self._screen = _CSBoxScreen(columns, rows)
        self._stream = _CSBoxByteStream(self._screen)
        self._relative_time = 0.0

    @property
    def columns(self) -> int:
        return self._screen.columns

    @property
    def rows(self) -> int:
        return self._screen.lines

    @property
    def checkpoint_safe(self) -> bool:
        """Whether parser and UTF-8 decoder are between complete sequences."""

        return self._stream.checkpoint_safe

    def apply(self, event: TerminalEvent) -> None:
        if not isinstance(event, TerminalEvent):
            raise TypeError("event must be a TerminalEvent")
        if event.type is TerminalEventType.OUTPUT:
            assert isinstance(event.payload, bytes)
            self._stream.feed(event.payload)
        elif event.type is TerminalEventType.RESIZE:
            assert isinstance(event.payload, TerminalSize)
            self._screen.resize(lines=event.payload.rows, columns=event.payload.columns)
        self._relative_time = event.relative_time

    handle = apply
    __call__ = apply

    def snapshot(self) -> TerminalSnapshot:
        return _snapshot_from_pyte(
            self._screen,
            self._relative_time,
            _state_from_pyte(self._screen, self._stream),
        )

    def restore(self, snapshot: TerminalSnapshot) -> None:
        """Replace emulator state with an immutable domain snapshot."""

        _validate_snapshot(snapshot)
        screen = _CSBoxScreen(snapshot.columns, snapshot.rows)
        if snapshot.state is not None:
            _restore_screen_state(screen, snapshot.state)
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
        screen.cursor.y = (
            snapshot.state.cursor_row if snapshot.state is not None else snapshot.cursor.row
        )
        screen.cursor.x = (
            snapshot.state.cursor_column if snapshot.state is not None else snapshot.cursor.column
        )
        screen.cursor.hidden = not snapshot.cursor.visible
        screen.dirty = set(range(snapshot.rows))
        self._screen = screen
        self._stream = _CSBoxByteStream(screen)
        if snapshot.state is not None:
            self._stream.use_utf8 = snapshot.state.use_utf8
            self._stream.restore_pending(snapshot.state.pending_bytes)
        self._relative_time = snapshot.relative_time


def _snapshot_from_pyte(
    screen: pyte.Screen,
    relative_time: float,
    state: TerminalEmulatorState | None = None,
) -> TerminalSnapshot:
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
        state=state,
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
    if snapshot.state is not None:
        _validate_emulator_state(snapshot.state, snapshot.columns, snapshot.rows)
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


def _attributes_from_pyte(character: Any) -> TerminalAttributes:
    return TerminalAttributes(
        foreground=str(character.fg),
        background=str(character.bg),
        bold=bool(character.bold),
        italic=bool(character.italics),
        underline=bool(character.underscore),
        strikethrough=bool(character.strikethrough),
        reverse=bool(character.reverse),
        blink=bool(character.blink),
    )


def _attributes_to_pyte(attributes: TerminalAttributes, *, data: str = " ") -> Any:
    return pyte.screens.Char(
        data=data,
        fg=attributes.foreground,
        bg=attributes.background,
        bold=attributes.bold,
        italics=attributes.italic,
        underscore=attributes.underline,
        strikethrough=attributes.strikethrough,
        reverse=attributes.reverse,
        blink=attributes.blink,
    )


def _state_from_pyte(
    screen: pyte.Screen,
    stream: _CSBoxByteStream,
) -> TerminalEmulatorState:
    savepoints = tuple(
        TerminalSavepoint(
            row=savepoint.cursor.y,
            column=savepoint.cursor.x,
            visible=not savepoint.cursor.hidden,
            attributes=_attributes_from_pyte(savepoint.cursor.attrs),
            g0_charset=str(savepoint.g0_charset),
            g1_charset=str(savepoint.g1_charset),
            charset=int(savepoint.charset),
            origin=bool(savepoint.origin),
            wrap=bool(savepoint.wrap),
        )
        for savepoint in screen.savepoints
    )
    margins = None if screen.margins is None else tuple(screen.margins)
    return TerminalEmulatorState(
        version=1,
        cursor_row=screen.cursor.y,
        cursor_column=screen.cursor.x,
        cursor_attributes=_attributes_from_pyte(screen.cursor.attrs),
        modes=tuple(sorted(int(mode) for mode in screen.mode)),
        margins=margins,
        tabstops=tuple(sorted(int(tabstop) for tabstop in screen.tabstops)),
        charset=int(screen.charset),
        g0_charset=str(screen.g0_charset),
        g1_charset=str(screen.g1_charset),
        savepoints=savepoints,
        saved_columns=screen.saved_columns,
        title=str(screen.title),
        icon_name=str(screen.icon_name),
        use_utf8=bool(stream.use_utf8),
        pending_bytes=stream.pending_bytes,
    )


def _restore_screen_state(screen: pyte.Screen, state: TerminalEmulatorState) -> None:
    _validate_emulator_state(state, screen.columns, screen.lines)
    screen.mode = set(state.modes)
    screen.margins = None if state.margins is None else pyte.screens.Margins(*state.margins)
    screen.tabstops = set(state.tabstops)
    screen.charset = state.charset
    screen.g0_charset = state.g0_charset
    screen.g1_charset = state.g1_charset
    screen.saved_columns = state.saved_columns
    screen.title = state.title
    screen.icon_name = state.icon_name
    screen.cursor.attrs = _attributes_to_pyte(state.cursor_attributes)
    screen.savepoints = []
    for saved in state.savepoints:
        cursor = pyte.screens.Cursor(
            saved.column,
            saved.row,
            _attributes_to_pyte(saved.attributes),
        )
        cursor.hidden = not saved.visible
        screen.savepoints.append(
            pyte.screens.Savepoint(
                cursor,
                saved.g0_charset,
                saved.g1_charset,
                saved.charset,
                saved.origin,
                saved.wrap,
            )
        )


def _validate_emulator_state(
    state: TerminalEmulatorState,
    columns: int,
    rows: int,
) -> None:
    if not isinstance(state, TerminalEmulatorState) or state.version != 1:
        raise ValueError("unsupported terminal emulator state")
    if not 0 <= state.cursor_row < rows or not 0 <= state.cursor_column <= columns:
        raise ValueError("terminal state cursor is outside the terminal")
    if state.margins is not None and (
        len(state.margins) != 2 or not 0 <= state.margins[0] < state.margins[1] < rows
    ):
        raise ValueError("terminal state margins are invalid")
    if any(tabstop < 0 or tabstop > columns for tabstop in state.tabstops):
        raise ValueError("terminal state tab stop is outside the terminal")
    if state.charset not in {0, 1}:
        raise ValueError("terminal state charset is invalid")
    if state.saved_columns is not None and state.saved_columns <= 0:
        raise ValueError("terminal state saved columns are invalid")
    for savepoint in state.savepoints:
        if not 0 <= savepoint.row < rows or not 0 <= savepoint.column <= columns:
            raise ValueError("terminal savepoint is outside the terminal")
        if savepoint.charset not in {0, 1}:
            raise ValueError("terminal savepoint charset is invalid")
    if not isinstance(state.pending_bytes, bytes):
        raise ValueError("terminal parser pending bytes are invalid")


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


class _CSBoxByteStream(pyte.ByteStream):
    """Keep parser boundary checks inside the pyte adapter."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._pending_bytes = bytearray()

    def feed(self, data: bytes) -> None:
        for value in data:
            self._pending_bytes.append(value)
            super().feed(bytes((value,)))
            if self._is_parser_safe:
                self._pending_bytes.clear()

    @property
    def pending_bytes(self) -> bytes:
        return bytes(self._pending_bytes)

    def restore_pending(self, data: bytes) -> None:
        if not isinstance(data, bytes):
            raise TypeError("pending parser data must be bytes")
        self._pending_bytes.clear()
        self.feed(data)

    @property
    def _is_parser_safe(self) -> bool:
        pending_bytes, _ = self.utf8_decoder.getstate()
        return self._taking_plain_text is True and not pending_bytes

    @property
    def checkpoint_safe(self) -> bool:
        pending_bytes, _ = self.utf8_decoder.getstate()
        return self._is_parser_safe and not self._pending_bytes


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
