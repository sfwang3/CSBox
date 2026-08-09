from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from csbox.core.events import TerminalEvent, TerminalEventType, TerminalSize
from csbox.lab.recorder import AsciicastV3Reader, CastEvent, CastReadResult
from csbox.lab.screen import TerminalCell, TerminalCursor, TerminalEmulator, TerminalSnapshot

CHECKPOINT_VERSION: Final = 1
CHECKPOINT_SECONDS: Final = 5.0
CHECKPOINT_EVENTS: Final = 500

EmulatorFactory = Callable[..., TerminalEmulator]


class CheckpointStoreError(RuntimeError):
    """A derived replay checkpoint index could not be persisted."""


@dataclass(frozen=True, slots=True)
class Checkpoint:
    """State after ``event_index`` cast events have been applied."""

    event_index: int
    cast_offset: int
    relative_time: float
    snapshot: TerminalSnapshot


@dataclass(frozen=True, slots=True)
class _ReplayEvent:
    relative_time: float
    cast: CastEvent


class CheckpointStore:
    """Load and atomically replace a versioned, rebuildable replay index."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    def load(self, cast_path: Path | str) -> tuple[Checkpoint, ...] | None:
        source = Path(cast_path)
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(value, Mapping) or value.get("version") != CHECKPOINT_VERSION:
                return None
            if value.get("cast") != _cast_fingerprint(source):
                return None
            raw_checkpoints = value.get("checkpoints")
            if not isinstance(raw_checkpoints, list):
                return None
            checksum = value.get("checksum")
            if not isinstance(checksum, str) or checksum != _checkpoint_checksum(raw_checkpoints):
                return None
            checkpoints = tuple(_checkpoint_from_json(item) for item in raw_checkpoints)
            if not checkpoints:
                return None
            return checkpoints
        except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
            return None

    def save(self, cast_path: Path | str, checkpoints: tuple[Checkpoint, ...]) -> None:
        if not checkpoints:
            raise ValueError("at least one checkpoint is required")
        serialized_checkpoints = [_checkpoint_to_json(checkpoint) for checkpoint in checkpoints]
        document = {
            "version": CHECKPOINT_VERSION,
            "cast": _cast_fingerprint(Path(cast_path)),
            "checkpoints": serialized_checkpoints,
            "checksum": _checkpoint_checksum(serialized_checkpoints),
        }
        payload = (json.dumps(document, ensure_ascii=False, indent=2) + "\n").encode()
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write(self.path, payload)
        except OSError as exc:
            raise CheckpointStoreError(f"could not persist replay checkpoints: {exc}") from exc


class ReplayService:
    """Deterministically seek an asciicast using domain snapshot checkpoints."""

    def __init__(
        self,
        cast_path: Path | str,
        *,
        checkpoint_store: CheckpointStore | None = None,
        emulator_factory: EmulatorFactory = TerminalEmulator,
    ) -> None:
        self.cast_path = Path(cast_path)
        self.checkpoint_store = checkpoint_store or CheckpointStore(
            self.cast_path.with_name("checkpoints.json")
        )
        self._emulator_factory = emulator_factory
        read_result = AsciicastV3Reader(self.cast_path).read()
        self.warnings = read_result.warnings
        self._columns, self._rows = _header_dimensions(read_result.header)
        self._events = _timed_events(read_result.events)
        self.duration = self._events[-1].relative_time if self._events else 0.0
        checkpoints = self.checkpoint_store.load(self.cast_path)
        if not _checkpoints_match(checkpoints, read_result, self._events):
            checkpoints = self._build_checkpoints(read_result)
            self.checkpoint_store.save(self.cast_path, checkpoints)
        assert checkpoints is not None
        self.checkpoints = checkpoints

    def seek(self, relative_time: float) -> TerminalSnapshot:
        if isinstance(relative_time, bool) or not isinstance(relative_time, int | float):
            raise TypeError("relative_time must be a number")
        if not math.isfinite(relative_time):
            raise ValueError("relative_time must be finite")
        target = max(0.0, float(relative_time))
        checkpoint = max(
            (item for item in self.checkpoints if item.relative_time <= target),
            key=lambda item: (item.relative_time, item.event_index),
        )
        emulator = self._emulator_factory(
            columns=checkpoint.snapshot.columns,
            rows=checkpoint.snapshot.rows,
        )
        emulator.restore(checkpoint.snapshot)
        for event_index, replay_event in enumerate(
            self._events[checkpoint.event_index :],
            start=checkpoint.event_index,
        ):
            if replay_event.relative_time > target:
                break
            event = _terminal_event(event_index, replay_event)
            if event is not None:
                emulator.apply(event)
        return emulator.snapshot()

    def _build_checkpoints(self, read_result: CastReadResult) -> tuple[Checkpoint, ...]:
        emulator = self._emulator_factory(columns=self._columns, rows=self._rows)
        checkpoints = [
            Checkpoint(
                event_index=0,
                cast_offset=read_result.data_offset,
                relative_time=0.0,
                snapshot=emulator.snapshot(),
            )
        ]
        last_checkpoint_time = 0.0
        last_checkpoint_index = 0
        for event_index, replay_event in enumerate(self._events, start=1):
            event = _terminal_event(event_index - 1, replay_event)
            if event is not None:
                emulator.apply(event)
            if (
                replay_event.relative_time - last_checkpoint_time >= CHECKPOINT_SECONDS
                or event_index - last_checkpoint_index >= CHECKPOINT_EVENTS
            ):
                checkpoints.append(
                    Checkpoint(
                        event_index=event_index,
                        cast_offset=replay_event.cast.cast_offset,
                        relative_time=replay_event.relative_time,
                        snapshot=emulator.snapshot(),
                    )
                )
                last_checkpoint_time = replay_event.relative_time
                last_checkpoint_index = event_index
        return tuple(checkpoints)


def _timed_events(events: tuple[CastEvent, ...]) -> tuple[_ReplayEvent, ...]:
    relative_time = 0.0
    replay_events: list[_ReplayEvent] = []
    for event in events:
        relative_time += event.interval
        replay_events.append(_ReplayEvent(relative_time=relative_time, cast=event))
    return tuple(replay_events)


def _terminal_event(event_index: int, replay_event: _ReplayEvent) -> TerminalEvent | None:
    cast = replay_event.cast
    if cast.code == "o":
        event_type = TerminalEventType.OUTPUT
        payload: bytes | str | TerminalSize | int | None = cast.data.encode("utf-8")
    elif cast.code == "i":
        event_type = TerminalEventType.INPUT
        payload = cast.data.encode("utf-8")
    elif cast.code == "r":
        dimensions = cast.data.lower().split("x", maxsplit=1)
        if len(dimensions) != 2:
            return None
        try:
            payload = TerminalSize(columns=int(dimensions[0]), rows=int(dimensions[1]))
        except (TypeError, ValueError):
            return None
        event_type = TerminalEventType.RESIZE
    elif cast.code == "m":
        event_type = TerminalEventType.MARK
        payload = cast.data
    elif cast.code == "x":
        event_type = TerminalEventType.EXIT
        try:
            payload = int(cast.data)
        except ValueError:
            payload = None
    else:
        return None
    return TerminalEvent(
        sequence=event_index + 1,
        monotonic_time=replay_event.relative_time,
        relative_time=replay_event.relative_time,
        type=event_type,
        payload=payload,
    )


def _header_dimensions(header: Mapping[str, Any]) -> tuple[int, int]:
    term = header["term"]
    assert isinstance(term, Mapping)
    columns = term["cols"]
    rows = term["rows"]
    assert type(columns) is int and type(rows) is int
    return columns, rows


def _checkpoints_match(
    checkpoints: tuple[Checkpoint, ...] | None,
    read_result: CastReadResult,
    events: tuple[_ReplayEvent, ...],
) -> bool:
    if not checkpoints:
        return False
    if tuple(checkpoint.event_index for checkpoint in checkpoints) != _checkpoint_indices(events):
        return False
    previous_index = -1
    previous_time = -1.0
    for checkpoint in checkpoints:
        if checkpoint.event_index <= previous_index or checkpoint.relative_time < previous_time:
            return False
        if checkpoint.event_index < 0 or checkpoint.event_index > len(events):
            return False
        expected_time = (
            0.0 if checkpoint.event_index == 0 else events[checkpoint.event_index - 1].relative_time
        )
        expected_offset = (
            read_result.data_offset
            if checkpoint.event_index == 0
            else events[checkpoint.event_index - 1].cast.cast_offset
        )
        if checkpoint.relative_time != expected_time or checkpoint.cast_offset != expected_offset:
            return False
        previous_index = checkpoint.event_index
        previous_time = checkpoint.relative_time
    initial = checkpoints[0]
    columns, rows = _header_dimensions(read_result.header)
    return (
        initial.event_index == 0
        and initial.relative_time == 0.0
        and initial.snapshot.columns == columns
        and initial.snapshot.rows == rows
    )


def _checkpoint_indices(events: tuple[_ReplayEvent, ...]) -> tuple[int, ...]:
    indices = [0]
    last_checkpoint_time = 0.0
    last_checkpoint_index = 0
    for event_index, event in enumerate(events, start=1):
        if (
            event.relative_time - last_checkpoint_time >= CHECKPOINT_SECONDS
            or event_index - last_checkpoint_index >= CHECKPOINT_EVENTS
        ):
            indices.append(event_index)
            last_checkpoint_time = event.relative_time
            last_checkpoint_index = event_index
    return tuple(indices)


def _cast_fingerprint(path: Path) -> dict[str, int | str]:
    data = path.read_bytes()
    return {"size": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def _checkpoint_checksum(checkpoints: list[object]) -> str:
    canonical = json.dumps(
        checkpoints,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(canonical).hexdigest()


def _checkpoint_to_json(checkpoint: Checkpoint) -> dict[str, Any]:
    return {
        "eventIndex": checkpoint.event_index,
        "castOffset": checkpoint.cast_offset,
        "relativeTime": checkpoint.relative_time,
        "snapshot": _snapshot_to_json(checkpoint.snapshot),
    }


def _checkpoint_from_json(value: object) -> Checkpoint:
    if not isinstance(value, Mapping):
        raise ValueError("checkpoint must be an object")
    event_index = value.get("eventIndex")
    cast_offset = value.get("castOffset")
    relative_time = value.get("relativeTime")
    if type(event_index) is not int or event_index < 0:
        raise ValueError("invalid checkpoint event index")
    if type(cast_offset) is not int or cast_offset < 0:
        raise ValueError("invalid checkpoint cast offset")
    if (
        isinstance(relative_time, bool)
        or not isinstance(relative_time, int | float)
        or not math.isfinite(relative_time)
        or relative_time < 0
    ):
        raise ValueError("invalid checkpoint relative time")
    return Checkpoint(
        event_index=event_index,
        cast_offset=cast_offset,
        relative_time=float(relative_time),
        snapshot=_snapshot_from_json(value.get("snapshot")),
    )


def _snapshot_to_json(snapshot: TerminalSnapshot) -> dict[str, Any]:
    return {
        "rows": snapshot.rows,
        "columns": snapshot.columns,
        "cells": [
            [
                {
                    "character": cell.character,
                    "width": cell.width,
                    "foreground": cell.foreground,
                    "background": cell.background,
                    "bold": cell.bold,
                    "italic": cell.italic,
                    "underline": cell.underline,
                    "strikethrough": cell.strikethrough,
                    "reverse": cell.reverse,
                }
                for cell in row
            ]
            for row in snapshot.cells
        ],
        "cursor": {
            "row": snapshot.cursor.row,
            "column": snapshot.cursor.column,
            "visible": snapshot.cursor.visible,
        },
        "relativeTime": snapshot.relative_time,
    }


def _snapshot_from_json(value: object) -> TerminalSnapshot:
    if not isinstance(value, Mapping):
        raise ValueError("snapshot must be an object")
    rows = value.get("rows")
    columns = value.get("columns")
    raw_cells = value.get("cells")
    cursor = value.get("cursor")
    relative_time = value.get("relativeTime")
    if type(rows) is not int or rows <= 0 or type(columns) is not int or columns <= 0:
        raise ValueError("invalid snapshot dimensions")
    if not isinstance(raw_cells, list) or len(raw_cells) != rows:
        raise ValueError("invalid snapshot rows")
    cells = tuple(
        tuple(_cell_from_json(cell) for cell in row)
        for row in raw_cells
        if isinstance(row, list) and len(row) == columns
    )
    if len(cells) != rows:
        raise ValueError("invalid snapshot columns")
    if not isinstance(cursor, Mapping):
        raise ValueError("invalid snapshot cursor")
    cursor_row = cursor.get("row")
    cursor_column = cursor.get("column")
    cursor_visible = cursor.get("visible")
    if (
        type(cursor_row) is not int
        or not 0 <= cursor_row < rows
        or type(cursor_column) is not int
        or not 0 <= cursor_column < columns
        or type(cursor_visible) is not bool
    ):
        raise ValueError("invalid snapshot cursor")
    if (
        isinstance(relative_time, bool)
        or not isinstance(relative_time, int | float)
        or not math.isfinite(relative_time)
        or relative_time < 0
    ):
        raise ValueError("invalid snapshot relative time")
    snapshot = TerminalSnapshot(
        rows=rows,
        columns=columns,
        cells=cells,
        cursor=TerminalCursor(cursor_row, cursor_column, cursor_visible),
        relative_time=float(relative_time),
    )
    # Reuse the adapter's public validation boundary without importing a
    # private helper: restore rejects malformed wide-cell geometry.
    TerminalEmulator(columns=columns, rows=rows).restore(snapshot)
    return snapshot


def _cell_from_json(value: object) -> TerminalCell:
    if not isinstance(value, Mapping):
        raise ValueError("cell must be an object")
    character = value.get("character")
    width = value.get("width")
    foreground = value.get("foreground")
    background = value.get("background")
    flags = [
        value.get("bold"),
        value.get("italic"),
        value.get("underline"),
        value.get("strikethrough"),
        value.get("reverse"),
    ]
    if (
        not isinstance(character, str)
        or type(width) is not int
        or width not in {0, 1, 2}
        or not isinstance(foreground, str)
        or not isinstance(background, str)
        or any(type(flag) is not bool for flag in flags)
    ):
        raise ValueError("invalid terminal cell")
    return TerminalCell(
        character=character,
        width=width,
        foreground=foreground,
        background=background,
        bold=flags[0],
        italic=flags[1],
        underline=flags[2],
        strikethrough=flags[3],
        reverse=flags[4],
    )


def _atomic_write(path: Path, data: bytes) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    except BaseException:
        with suppress(OSError):
            temporary.unlink(missing_ok=True)
        raise


def _fsync_directory(directory: Path) -> None:
    try:
        descriptor = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)
