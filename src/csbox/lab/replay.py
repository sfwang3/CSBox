from __future__ import annotations

import base64
import binascii
import hashlib
import json
import math
from bisect import bisect_right
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path
from typing import Any, Final

from csbox.core.events import TerminalEvent, TerminalEventType, TerminalSize
from csbox.core.safe_paths import (
    atomic_write_bytes,
    ensure_private_directory,
    open_regular_binary,
    read_regular_text,
)
from csbox.lab.recorder import AsciicastV3Reader, CastEvent, CastReadResult
from csbox.lab.screen import (
    TerminalAttributes,
    TerminalCell,
    TerminalCursor,
    TerminalEmulator,
    TerminalEmulatorState,
    TerminalSavepoint,
    TerminalSnapshot,
)

CHECKPOINT_VERSION: Final = 2
CHECKPOINT_SECONDS: Final = 5.0
CHECKPOINT_EVENTS: Final = 500
MAX_CHECKPOINT_DOCUMENT_BYTES: Final = 32 * 1024 * 1024

EmulatorFactory = Callable[..., TerminalEmulator]


class CheckpointStoreError(RuntimeError):
    """A derived replay checkpoint index could not be persisted."""


class ReplayState(StrEnum):
    PLAYABLE = "playable"
    EMPTY = "empty"
    CORRUPT = "corrupt"


@dataclass(frozen=True, slots=True)
class ReplayDiagnostics:
    state: ReplayState
    duration: float
    event_count: int
    output_event_count: int
    warnings: tuple[str, ...]


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
        self._ensure_distinct_from_cast(source)
        try:
            value = json.loads(
                read_regular_text(self.path, max_bytes=MAX_CHECKPOINT_DOCUMENT_BYTES)
            )
            if not isinstance(value, Mapping) or value.get("version") != CHECKPOINT_VERSION:
                return None
            if value.get("cast") != _cast_fingerprint(source):
                return None
            raw_checkpoints = value.get("checkpoints")
            if not isinstance(raw_checkpoints, list):
                return None
            checksum = value.get("checksum")
            # This detects accidental corruption only.  It is not an
            # authentication boundary against a local writer who can also
            # recompute the checksum; structural/source checks remain below.
            if not isinstance(checksum, str) or checksum != _checkpoint_checksum(raw_checkpoints):
                return None
            checkpoints = tuple(_checkpoint_from_json(item) for item in raw_checkpoints)
            if not checkpoints:
                return None
            return checkpoints
        except (
            OSError,
            UnicodeError,
            json.JSONDecodeError,
            TypeError,
            ValueError,
            RecursionError,
        ):
            return None

    def save(self, cast_path: Path | str, checkpoints: tuple[Checkpoint, ...]) -> None:
        if not checkpoints:
            raise ValueError("at least one checkpoint is required")
        source = Path(cast_path)
        self._ensure_distinct_from_cast(source)
        payload = _bounded_checkpoint_payload(_cast_fingerprint(source), checkpoints)
        if payload is None:
            return
        try:
            ensure_private_directory(self.path.parent)
            _atomic_write(self.path, payload)
        except (OSError, ValueError) as exc:
            raise CheckpointStoreError("could not persist replay checkpoints") from exc

    def _ensure_distinct_from_cast(self, cast_path: Path) -> None:
        try:
            checkpoint_path = self.path.resolve()
            source_path = cast_path.resolve()
        except OSError as exc:
            raise CheckpointStoreError(
                "could not resolve replay checkpoint and cast paths"
            ) from exc
        if checkpoint_path == source_path:
            raise CheckpointStoreError("replay checkpoint path must differ from cast path")


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
            _default_checkpoint_path(self.cast_path)
        )
        self._emulator_factory = emulator_factory
        read_result = AsciicastV3Reader(self.cast_path).read()
        warnings = list(read_result.warnings)
        self._columns, self._rows = _header_dimensions(read_result.header)
        self._events = _timed_events(read_result.events)
        event_duration = self._events[-1].relative_time if self._events else 0.0
        self.duration = event_duration + read_result.trailing_interval
        self.event_count = len(read_result.events)
        self.output_event_count = sum(
            1 for event in read_result.events if event.code == "o" and event.data
        )
        self.state = (
            ReplayState.CORRUPT
            if _has_integrity_warning(tuple(warnings))
            else ReplayState.PLAYABLE
            if self.output_event_count
            else ReplayState.EMPTY
        )
        checkpoints = self.checkpoint_store.load(self.cast_path)
        if not _checkpoints_match(checkpoints, read_result, self._events):
            checkpoints = self._build_checkpoints(read_result)
            try:
                self.checkpoint_store.save(self.cast_path, checkpoints)
            except CheckpointStoreError:
                warnings.append("replay checkpoints could not be persisted")
        self.warnings = tuple(warnings)
        assert checkpoints is not None
        self.checkpoints = checkpoints
        self._checkpoint_keys = tuple(
            (checkpoint.relative_time, checkpoint.event_index) for checkpoint in checkpoints
        )

    @classmethod
    def unavailable(
        cls,
        cast_path: Path | str,
        *,
        columns: int,
        rows: int,
        warning: str,
        checkpoint_store: CheckpointStore | None = None,
        emulator_factory: EmulatorFactory = TerminalEmulator,
    ) -> ReplayService:
        """Create a controlled zero-event replay for an unreadable cast."""

        if columns <= 0 or rows <= 0:
            raise ValueError("fallback terminal dimensions must be positive")
        instance = cls.__new__(cls)
        instance.cast_path = Path(cast_path)
        instance.checkpoint_store = checkpoint_store or CheckpointStore(
            _default_checkpoint_path(instance.cast_path)
        )
        instance._emulator_factory = emulator_factory
        instance.warnings = (warning,)
        instance._columns = columns
        instance._rows = rows
        instance._events = ()
        instance.duration = 0.0
        instance.event_count = 0
        instance.output_event_count = 0
        instance.state = ReplayState.CORRUPT
        snapshot = emulator_factory(columns=columns, rows=rows).snapshot()
        instance.checkpoints = (Checkpoint(0, 0, 0.0, snapshot),)
        instance._checkpoint_keys = ((0.0, 0),)
        return instance

    @property
    def diagnostics(self) -> ReplayDiagnostics:
        return ReplayDiagnostics(
            state=self.state,
            duration=self.duration,
            event_count=self.event_count,
            output_event_count=self.output_event_count,
            warnings=self.warnings,
        )

    def seek(self, relative_time: float) -> TerminalSnapshot:
        if isinstance(relative_time, bool) or not isinstance(relative_time, int | float):
            raise TypeError("relative_time must be a number")
        if not math.isfinite(relative_time):
            raise ValueError("relative_time must be finite")
        target = min(max(0.0, float(relative_time)), self.duration)
        checkpoint_index = (
            bisect_right(
                self._checkpoint_keys,
                (target, len(self._events) + 1),
            )
            - 1
        )
        checkpoint = self.checkpoints[checkpoint_index]
        emulator = self._emulator_factory(
            columns=checkpoint.snapshot.columns,
            rows=checkpoint.snapshot.rows,
        )
        emulator.restore(checkpoint.snapshot)
        for event_index in range(checkpoint.event_index, len(self._events)):
            replay_event = self._events[event_index]
            if replay_event.relative_time > target:
                break
            event = _terminal_event(event_index, replay_event)
            if event is not None:
                emulator.apply(event)
        return replace(emulator.snapshot(), relative_time=target)

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
            if emulator.checkpoint_safe and (
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


def _has_integrity_warning(warnings: tuple[str, ...]) -> bool:
    markers = (
        "truncated cast line",
        "corrupt cast line",
        "oversized cast line",
        "invalid cast event",
        "invalid resize",
    )
    return any(any(marker in warning for marker in markers) for warning in warnings)


def _default_checkpoint_path(cast_path: Path) -> Path:
    if cast_path.name.casefold() == "checkpoints.json":
        return cast_path.with_name("checkpoints.checkpoints.json")
    return cast_path.with_name("checkpoints.json")


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
        if checkpoint.event_index > 0 and not (
            checkpoint.relative_time - previous_time >= CHECKPOINT_SECONDS
            or checkpoint.event_index - previous_index >= CHECKPOINT_EVENTS
        ):
            return False
        if checkpoint.snapshot.state is None:
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


def _cast_fingerprint(path: Path) -> dict[str, int | str]:
    digest = hashlib.sha256()
    size = 0
    with open_regular_binary(path) as stream:
        while chunk := stream.read(1024 * 1024):
            size += len(chunk)
            digest.update(chunk)
    return {"size": size, "sha256": digest.hexdigest()}


def _checkpoint_checksum(checkpoints: list[object]) -> str:
    canonical = json.dumps(
        checkpoints,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(canonical).hexdigest()


def _bounded_checkpoint_payload(
    cast_fingerprint: Mapping[str, int | str],
    checkpoints: tuple[Checkpoint, ...],
) -> bytes | None:
    largest = max(
        checkpoints,
        key=lambda checkpoint: checkpoint.snapshot.rows * checkpoint.snapshot.columns,
    )
    representative_size = len(
        json.dumps(_checkpoint_to_json(largest), ensure_ascii=False, indent=2).encode("utf-8")
    )
    available = max(1, MAX_CHECKPOINT_DOCUMENT_BYTES - 4096)
    maximum_count = max(1, min(len(checkpoints), available // max(1, representative_size)))

    while True:
        selected = _sample_checkpoints(checkpoints, maximum_count)
        serialized = [_checkpoint_to_json(checkpoint) for checkpoint in selected]
        document = {
            "version": CHECKPOINT_VERSION,
            "cast": dict(cast_fingerprint),
            "checkpoints": serialized,
            "checksum": _checkpoint_checksum(serialized),
        }
        payload = (json.dumps(document, ensure_ascii=False, indent=2) + "\n").encode()
        if len(payload) <= MAX_CHECKPOINT_DOCUMENT_BYTES:
            return payload
        if maximum_count == 1:
            return None
        maximum_count = max(1, maximum_count // 2)


def _sample_checkpoints(
    checkpoints: tuple[Checkpoint, ...], maximum_count: int
) -> tuple[Checkpoint, ...]:
    if maximum_count >= len(checkpoints):
        return checkpoints
    if maximum_count <= 1:
        return (checkpoints[0],)
    last_index = len(checkpoints) - 1
    indices = tuple(
        (sample_index * last_index) // (maximum_count - 1) for sample_index in range(maximum_count)
    )
    return tuple(checkpoints[index] for index in indices)


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
        "state": None if snapshot.state is None else _state_to_json(snapshot.state),
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
        state=_state_from_json(value.get("state"), columns=columns, rows=rows),
    )
    # Reuse the adapter's public validation boundary without importing a
    # private helper: restore rejects malformed wide-cell geometry.
    TerminalEmulator(columns=columns, rows=rows).restore(snapshot)
    return snapshot


def _state_to_json(state: TerminalEmulatorState) -> dict[str, Any]:
    return {
        "version": state.version,
        "cursorRow": state.cursor_row,
        "cursorColumn": state.cursor_column,
        "cursorAttributes": _attributes_to_json(state.cursor_attributes),
        "modes": list(state.modes),
        "margins": None if state.margins is None else list(state.margins),
        "tabstops": list(state.tabstops),
        "charset": state.charset,
        "g0Charset": state.g0_charset,
        "g1Charset": state.g1_charset,
        "savepoints": [_savepoint_to_json(savepoint) for savepoint in state.savepoints],
        "savedColumns": state.saved_columns,
        "title": state.title,
        "iconName": state.icon_name,
        "useUtf8": state.use_utf8,
        "pendingBytes": base64.b64encode(state.pending_bytes).decode("ascii"),
    }


def _state_from_json(
    value: object,
    *,
    columns: int,
    rows: int,
) -> TerminalEmulatorState | None:
    if value is None:
        return None
    if not isinstance(value, Mapping) or value.get("version") != 1:
        raise ValueError("invalid terminal emulator state version")
    cursor_row = _required_int(value, "cursorRow")
    cursor_column = _required_int(value, "cursorColumn")
    modes = _int_tuple(value.get("modes"), "terminal modes")
    tabstops = _int_tuple(value.get("tabstops"), "terminal tabstops")
    margins_value = value.get("margins")
    margins: tuple[int, int] | None
    if margins_value is None:
        margins = None
    elif (
        isinstance(margins_value, list)
        and len(margins_value) == 2
        and all(type(item) is int for item in margins_value)
    ):
        margins = (margins_value[0], margins_value[1])
    else:
        raise ValueError("invalid terminal margins")
    charset = _required_int(value, "charset")
    saved_columns = value.get("savedColumns")
    if saved_columns is not None and type(saved_columns) is not int:
        raise ValueError("invalid terminal saved columns")
    g0_charset = value.get("g0Charset")
    g1_charset = value.get("g1Charset")
    title = value.get("title")
    icon_name = value.get("iconName")
    use_utf8 = value.get("useUtf8")
    if not all(isinstance(item, str) for item in (g0_charset, g1_charset, title, icon_name)):
        raise ValueError("invalid terminal string state")
    if type(use_utf8) is not bool:
        raise ValueError("invalid terminal decoder state")
    pending_value = value.get("pendingBytes", "")
    if not isinstance(pending_value, str):
        raise ValueError("invalid terminal parser state")
    try:
        pending_bytes = base64.b64decode(pending_value.encode("ascii"), validate=True)
    except (UnicodeEncodeError, binascii.Error) as exc:
        raise ValueError("invalid terminal parser state") from exc
    raw_savepoints = value.get("savepoints")
    if not isinstance(raw_savepoints, list):
        raise ValueError("invalid terminal savepoints")
    state = TerminalEmulatorState(
        version=1,
        cursor_row=cursor_row,
        cursor_column=cursor_column,
        cursor_attributes=_attributes_from_json(value.get("cursorAttributes")),
        modes=modes,
        margins=margins,
        tabstops=tabstops,
        charset=charset,
        g0_charset=g0_charset,
        g1_charset=g1_charset,
        savepoints=tuple(_savepoint_from_json(item) for item in raw_savepoints),
        saved_columns=saved_columns,
        title=title,
        icon_name=icon_name,
        use_utf8=use_utf8,
        pending_bytes=pending_bytes,
    )
    # The adapter performs bounds and semantic validation against the grid.
    TerminalEmulator(columns=columns, rows=rows).restore(
        TerminalSnapshot(
            rows=rows,
            columns=columns,
            cells=tuple(tuple(TerminalCell() for _ in range(columns)) for _ in range(rows)),
            cursor=TerminalCursor(),
            state=state,
        )
    )
    return state


def _attributes_to_json(attributes: TerminalAttributes) -> dict[str, Any]:
    return {
        "foreground": attributes.foreground,
        "background": attributes.background,
        "bold": attributes.bold,
        "italic": attributes.italic,
        "underline": attributes.underline,
        "strikethrough": attributes.strikethrough,
        "reverse": attributes.reverse,
        "blink": attributes.blink,
    }


def _attributes_from_json(value: object) -> TerminalAttributes:
    if not isinstance(value, Mapping):
        raise ValueError("invalid terminal attributes")
    foreground = value.get("foreground")
    background = value.get("background")
    flags = [
        value.get("bold"),
        value.get("italic"),
        value.get("underline"),
        value.get("strikethrough"),
        value.get("reverse"),
        value.get("blink"),
    ]
    if (
        not isinstance(foreground, str)
        or not isinstance(background, str)
        or any(type(flag) is not bool for flag in flags)
    ):
        raise ValueError("invalid terminal attributes")
    return TerminalAttributes(
        foreground=foreground,
        background=background,
        bold=flags[0],
        italic=flags[1],
        underline=flags[2],
        strikethrough=flags[3],
        reverse=flags[4],
        blink=flags[5],
    )


def _savepoint_to_json(savepoint: TerminalSavepoint) -> dict[str, Any]:
    return {
        "row": savepoint.row,
        "column": savepoint.column,
        "visible": savepoint.visible,
        "attributes": _attributes_to_json(savepoint.attributes),
        "g0Charset": savepoint.g0_charset,
        "g1Charset": savepoint.g1_charset,
        "charset": savepoint.charset,
        "origin": savepoint.origin,
        "wrap": savepoint.wrap,
    }


def _savepoint_from_json(value: object) -> TerminalSavepoint:
    if not isinstance(value, Mapping):
        raise ValueError("invalid terminal savepoint")
    visible = value.get("visible")
    origin = value.get("origin")
    wrap = value.get("wrap")
    g0_charset = value.get("g0Charset")
    g1_charset = value.get("g1Charset")
    if (
        type(visible) is not bool
        or type(origin) is not bool
        or type(wrap) is not bool
        or not isinstance(g0_charset, str)
        or not isinstance(g1_charset, str)
    ):
        raise ValueError("invalid terminal savepoint")
    return TerminalSavepoint(
        row=_required_int(value, "row"),
        column=_required_int(value, "column"),
        visible=visible,
        attributes=_attributes_from_json(value.get("attributes")),
        g0_charset=g0_charset,
        g1_charset=g1_charset,
        charset=_required_int(value, "charset"),
        origin=origin,
        wrap=wrap,
    )


def _required_int(value: Mapping[str, object], key: str) -> int:
    item = value.get(key)
    if type(item) is not int:
        raise ValueError(f"invalid integer field {key}")
    return item


def _int_tuple(value: object, label: str) -> tuple[int, ...]:
    if not isinstance(value, list) or any(type(item) is not int for item in value):
        raise ValueError(f"invalid {label}")
    result = tuple(value)
    if result != tuple(sorted(set(result))):
        raise ValueError(f"invalid {label}")
    return result


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
    atomic_write_bytes(path, data)
