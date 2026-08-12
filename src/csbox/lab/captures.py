from __future__ import annotations

import json
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
