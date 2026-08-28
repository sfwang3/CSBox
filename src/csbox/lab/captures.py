from __future__ import annotations

import json
import math
import re
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError

from csbox.core.safe_paths import (
    atomic_write_bytes,
    ensure_private_directory,
    read_regular_text,
)
from csbox.lab.models import CaptureRecord
from csbox.lab.screen import TerminalSnapshot

_MAX_CAPTURE_DOCUMENT_BYTES = 64 * 1024 * 1024
_CANONICAL_UTC_DATETIME = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]{6})?Z"
)


class CaptureStoreError(RuntimeError):
    """Capture metadata could not be read or persisted."""


@dataclass(frozen=True, slots=True)
class CaptureReadResult:
    captures: tuple[CaptureRecord, ...]
    warnings: tuple[str, ...] = ()


class _InvalidDocument(ValueError):
    pass


class CaptureStore:
    """Atomically persist recoverable capture sidecar data."""

    def __init__(
        self,
        path: Path | str,
        *,
        id_factory: Callable[[], str] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.path = Path(path)
        self.backup_path = self.path.with_suffix(self.path.suffix + ".bak")
        self._id_factory = id_factory or (lambda: str(uuid.uuid4()))
        self._clock = clock or (lambda: datetime.now(UTC))
        self._lock = threading.RLock()

    def load(self) -> CaptureReadResult:
        with self._lock:
            if not self.path.exists():
                if not self.backup_path.exists():
                    return CaptureReadResult(())
                return self._recover_backup(("primary captures file is missing",))

            try:
                captures, warnings = _read_document(self.path)
            except (OSError, UnicodeError, json.JSONDecodeError, ValueError, RecursionError):
                warning = "primary captures file is invalid"
                if self.backup_path.exists():
                    return self._recover_backup((warning,))
                return CaptureReadResult((), (warning,))
            return CaptureReadResult(_sort_captures(captures), tuple(warnings))

    def count(self) -> int:
        """Return a lightweight persisted Capture count without rebuilding snapshots."""

        with self._lock:
            if not self.path.exists():
                if not self.backup_path.exists():
                    return 0
                return _read_count_or_zero(self.backup_path)
            try:
                return _read_document_count(self.path)
            except (OSError, UnicodeError, json.JSONDecodeError, ValueError, RecursionError):
                if self.backup_path.exists():
                    return _read_count_or_zero(self.backup_path)
                return 0

    def create_capture(
        self,
        snapshot: TerminalSnapshot,
        timestamp: float | None = None,
        *,
        cwd: Path | str,
        command: str | None = None,
        title: str = "",
    ) -> CaptureRecord:
        if not isinstance(snapshot, TerminalSnapshot):
            raise TypeError("snapshot must be a TerminalSnapshot")
        captured_at = snapshot.relative_time if timestamp is None else timestamp
        capture = CaptureRecord(
            id=self._id_factory(),
            title=title,
            createdAt=self._clock(),
            timestamp=captured_at,
            rows=snapshot.rows,
            columns=snapshot.columns,
            cwd=Path(cwd),
            command=command,
            snapshot=snapshot,
        )
        with self._lock:
            previous = self.load().captures
            self._persist((*previous, capture), previous)
        return capture

    create = create_capture

    def edit_title(self, capture_id: str, title: str) -> CaptureRecord:
        with self._lock:
            previous = self.load().captures
            updated: list[CaptureRecord] = []
            edited: CaptureRecord | None = None
            for capture in previous:
                if capture.capture_id == capture_id:
                    edited = capture.model_copy(update={"title": title})
                    updated.append(edited)
                else:
                    updated.append(capture)
            if edited is None:
                raise KeyError(capture_id)
            self._persist(tuple(updated), previous)
            return edited

    def delete(self, capture_id: str) -> bool:
        with self._lock:
            previous = self.load().captures
            remaining = tuple(capture for capture in previous if capture.capture_id != capture_id)
            if len(remaining) == len(previous):
                return False
            self._persist(remaining, previous)
            return True

    def _recover_backup(self, preceding_warnings: tuple[str, ...]) -> CaptureReadResult:
        try:
            captures, backup_warnings = _read_document(self.backup_path)
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError, RecursionError):
            return CaptureReadResult((), (*preceding_warnings, "backup captures file is invalid"))
        warnings = (
            *preceding_warnings,
            "captures recovered from backup",
            *backup_warnings,
        )
        return CaptureReadResult(_sort_captures(captures), warnings)

    def _persist(
        self,
        captures: tuple[CaptureRecord, ...],
        previous: tuple[CaptureRecord, ...],
    ) -> None:
        sorted_captures = _sort_captures(captures)
        new_payload = _serialize_document(sorted_captures)
        had_document = self.path.exists() or self.backup_path.exists()
        backup_payload = (
            _serialize_document(_sort_captures(previous)) if had_document else new_payload
        )
        try:
            ensure_private_directory(self.path.parent)
            _atomic_write(self.backup_path, backup_payload)
            _atomic_write(self.path, new_payload)
        except (OSError, ValueError) as exc:
            raise CaptureStoreError("could not persist captures") from exc


def _read_document(path: Path) -> tuple[tuple[CaptureRecord, ...], list[str]]:
    value = json.loads(read_regular_text(path, max_bytes=_MAX_CAPTURE_DOCUMENT_BYTES))
    if not isinstance(value, dict) or value.get("version") != 1:
        raise _InvalidDocument("expected captures schema version 1")
    raw_captures = value.get("captures")
    if not isinstance(raw_captures, list):
        raise _InvalidDocument("captures must be a list")

    captures: list[CaptureRecord] = []
    warnings: list[str] = []
    for index, raw_capture in enumerate(raw_captures, start=1):
        try:
            captures.append(CaptureRecord.model_validate(raw_capture))
        except (ValidationError, TypeError, ValueError):
            warnings.append(f"invalid capture {index} ignored")
    return tuple(captures), warnings


def _read_document_count(path: Path) -> int:
    value = json.loads(read_regular_text(path, max_bytes=_MAX_CAPTURE_DOCUMENT_BYTES))
    if not isinstance(value, dict) or value.get("version") != 1:
        raise _InvalidDocument("expected captures schema version 1")
    raw_captures = value.get("captures")
    if not isinstance(raw_captures, list):
        raise _InvalidDocument("captures must be a list")
    return sum(_has_capture_summary_shape(capture) for capture in raw_captures)


def _has_capture_summary_shape(value: object) -> bool:
    """Validate canonical persisted Capture shape without constructing snapshots."""

    if not isinstance(value, dict):
        return False
    capture_id = value.get("id")
    created_at = value.get("createdAt")
    timestamp = value.get("timestamp")
    rows = value.get("rows")
    columns = value.get("columns")
    cwd = value.get("cwd")
    title = value.get("title", "")
    command = value.get("command")
    snapshot = value.get("snapshot")
    if not _is_text(capture_id) or not capture_id:
        return False
    if not _is_canonical_utc_datetime(created_at):
        return False
    if not _is_finite_number(timestamp) or timestamp < 0:
        return False
    if (
        not _is_int(rows)
        or rows <= 0
        or not _is_int(columns)
        or columns <= 0
        or not _is_text(cwd)
        or not _is_text(title)
        or (command is not None and not _is_text(command))
    ):
        return False
    return _has_snapshot_summary_shape(snapshot, rows=rows, columns=columns)


def _has_snapshot_summary_shape(value: object, *, rows: int, columns: int) -> bool:
    if not isinstance(value, dict):
        return False
    cells = value.get("cells")
    cursor = value.get("cursor")
    relative_time = value.get("relative_time", 0.0)
    state = value.get("state")
    return (
        value.get("rows") == rows
        and value.get("columns") == columns
        and isinstance(cells, list)
        and len(cells) == rows
        and all(
            isinstance(row, list)
            and len(row) == columns
            and all(_has_cell_summary_shape(cell) for cell in row)
            for row in cells
        )
        and _has_cursor_summary_shape(cursor)
        and _is_finite_number(relative_time)
        and (state is None or _has_state_summary_shape(state))
    )


def _has_cell_summary_shape(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    return (
        _is_text(value.get("character", " "))
        and _is_int(value.get("width", 1))
        and _is_text(value.get("foreground", "default"))
        and _is_text(value.get("background", "default"))
        and all(
            isinstance(value.get(attribute, False), bool)
            for attribute in ("bold", "italic", "underline", "strikethrough", "reverse")
        )
    )


def _has_cursor_summary_shape(value: object) -> bool:
    return (
        isinstance(value, dict)
        and _is_int(value.get("row", 0))
        and _is_int(value.get("column", 0))
        and isinstance(value.get("visible", True), bool)
    )


def _has_attributes_summary_shape(value: object, *, blink: bool) -> bool:
    if not isinstance(value, dict):
        return False
    boolean_fields = ["bold", "italic", "underline", "strikethrough", "reverse"]
    if blink:
        boolean_fields.append("blink")
    return (
        _is_text(value.get("foreground", "default"))
        and _is_text(value.get("background", "default"))
        and all(isinstance(value.get(field, False), bool) for field in boolean_fields)
    )


def _has_savepoint_summary_shape(value: object) -> bool:
    return (
        isinstance(value, dict)
        and _is_int(value.get("row"))
        and _is_int(value.get("column"))
        and isinstance(value.get("visible"), bool)
        and _has_attributes_summary_shape(value.get("attributes"), blink=True)
        and _is_text(value.get("g0_charset"))
        and _is_text(value.get("g1_charset"))
        and _is_int(value.get("charset"))
        and isinstance(value.get("origin"), bool)
        and isinstance(value.get("wrap"), bool)
    )


def _has_state_summary_shape(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    modes = value.get("modes")
    margins = value.get("margins")
    tabstops = value.get("tabstops")
    savepoints = value.get("savepoints")
    saved_columns = value.get("saved_columns")
    return (
        _is_int(value.get("version"))
        and _is_int(value.get("cursor_row"))
        and _is_int(value.get("cursor_column"))
        and _has_attributes_summary_shape(value.get("cursor_attributes"), blink=True)
        and isinstance(modes, list)
        and all(_is_int(mode) for mode in modes)
        and (
            margins is None
            or (
                isinstance(margins, list)
                and len(margins) == 2
                and all(_is_int(margin) for margin in margins)
            )
        )
        and isinstance(tabstops, list)
        and all(_is_int(tabstop) for tabstop in tabstops)
        and _is_int(value.get("charset"))
        and _is_text(value.get("g0_charset"))
        and _is_text(value.get("g1_charset"))
        and isinstance(savepoints, list)
        and all(_has_savepoint_summary_shape(savepoint) for savepoint in savepoints)
        and (saved_columns is None or _is_int(saved_columns))
        and _is_text(value.get("title"))
        and _is_text(value.get("icon_name"))
        and isinstance(value.get("use_utf8"), bool)
        and _is_text(value.get("pending_bytes", ""))
    )


def _is_int(value: object) -> bool:
    return type(value) is int


def _is_text(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        value.encode("utf-8")
    except UnicodeError:
        return False
    return True


def _is_canonical_utc_datetime(value: object) -> bool:
    if not _is_text(value) or _CANONICAL_UTC_DATETIME.fullmatch(value) is None:
        return False
    try:
        datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ" if "." in value else "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return False
    return True


def _is_finite_number(value: object) -> bool:
    if type(value) not in {int, float}:
        return False
    try:
        return math.isfinite(float(value))
    except OverflowError:
        return False


def _read_count_or_zero(path: Path) -> int:
    try:
        return _read_document_count(path)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError, RecursionError):
        return 0


def _sort_captures(captures: tuple[CaptureRecord, ...]) -> tuple[CaptureRecord, ...]:
    return tuple(sorted(captures, key=lambda capture: (capture.timestamp, capture.created_at)))


def _serialize_document(captures: tuple[CaptureRecord, ...]) -> bytes:
    value = {
        "version": 1,
        "captures": [capture.model_dump(mode="json", by_alias=True) for capture in captures],
    }
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode()


def _atomic_write(path: Path, data: bytes) -> None:
    atomic_write_bytes(path, data)
