from __future__ import annotations

import json
import re
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError

from csbox.core.safe_paths import (
    atomic_create_text,
    atomic_write_text,
    ensure_private_directory,
    read_regular_text,
)
from csbox.evidence.models import EvidenceSet, EvidenceSetSummary, validate_identifier

EVIDENCE_SET_VERSION = 1
_MAX_EVIDENCE_SET_BYTES = 4 * 1024 * 1024
_CANONICAL_UTC_DATETIME = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]{6})?Z"
)
_DOCUMENT_KEYS = frozenset({"version", "id", "title", "items", "created_at", "updated_at"})


class EvidencePersistenceError(RuntimeError):
    """An Evidence Set could not be read or atomically persisted."""


class EvidenceSetRepository:
    """Persist one independently readable JSON document per Evidence Set."""

    def __init__(
        self,
        root: Path | str,
        *,
        id_factory: Callable[[], str] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.root = Path(root)
        self._id_factory = id_factory or (lambda: uuid.uuid4().hex)
        self._clock = clock or (lambda: datetime.now(UTC))

    @classmethod
    def from_cwd(cls, cwd: Path | str) -> EvidenceSetRepository:
        return cls(Path(cwd) / ".csbox" / "evidence")

    def path_for(self, evidence_set_id: str) -> Path:
        identifier = validate_identifier(evidence_set_id, field_name="evidence_set_id")
        return self.root / f"{identifier}.json"

    def create(self, title: str) -> EvidenceSet:
        now = self._now()
        try:
            identifier = validate_identifier(self._id_factory(), field_name="evidence_set_id")
            evidence_set = EvidenceSet(
                id=identifier,
                title=title,
                items=(),
                created_at=now,
                updated_at=now,
            )
        except (TypeError, ValueError, ValidationError) as exc:
            raise EvidencePersistenceError("证据集信息无效，无法创建。") from exc

        path = self.path_for(evidence_set.evidence_set_id)
        try:
            ensure_private_directory(self.root)
            if path.exists() or path.is_symlink():
                raise EvidencePersistenceError("证据集 ID 已存在，无法覆盖已有文件。")
            # The precheck gives a clearer error for the usual collision. The
            # no-replace publisher below is still authoritative if the path
            # appears between this check and publication.
            self._write(path, evidence_set, replace_existing=False)
        except EvidencePersistenceError:
            raise
        except (OSError, UnicodeError, ValueError, TypeError, RecursionError) as exc:
            raise EvidencePersistenceError("证据集保存失败，请检查项目目录后重试。") from exc
        return evidence_set

    def load(self, evidence_set_id: str) -> EvidenceSet:
        path = self.path_for(evidence_set_id)
        try:
            return self._read(path, expected_id=evidence_set_id)
        except EvidencePersistenceError:
            raise
        except (
            OSError,
            UnicodeError,
            json.JSONDecodeError,
            TypeError,
            ValueError,
            ValidationError,
            RecursionError,
        ) as exc:
            raise EvidencePersistenceError("证据集不可读取。") from exc

    def save(self, evidence_set: EvidenceSet) -> EvidenceSet:
        if not isinstance(evidence_set, EvidenceSet):
            raise TypeError("evidence_set must be an EvidenceSet")
        path = self.path_for(evidence_set.evidence_set_id)
        if not path.exists() or path.is_symlink() or not path.is_file():
            raise EvidencePersistenceError("证据集文件不存在或不可写。")
        try:
            persisted = self._read(path, expected_id=evidence_set.evidence_set_id)
            updated = evidence_set.model_copy(
                update={
                    "created_at": persisted.created_at,
                    "updated_at": self._now(),
                }
            )
            self._write(path, updated)
        except EvidencePersistenceError:
            raise
        except (
            OSError,
            UnicodeError,
            ValueError,
            TypeError,
            ValidationError,
            RecursionError,
        ) as exc:
            raise EvidencePersistenceError("证据集保存失败，请检查项目目录后重试。") from exc
        return updated

    def list_summaries(self) -> tuple[EvidenceSetSummary, ...]:
        if not self.root.exists():
            return ()
        if self.root.is_symlink() or not self.root.is_dir():
            raise EvidencePersistenceError("证据集目录不可读取。")

        try:
            candidates = sorted(self.root.glob("*.json"), key=lambda path: path.name)
        except OSError as exc:
            raise EvidencePersistenceError("证据集目录不可读取。") from exc

        summaries: list[EvidenceSetSummary] = []
        for path in candidates:
            if path.is_symlink() or not path.is_file():
                continue
            identifier = path.stem or path.name
            try:
                evidence_set = self._read(path, expected_id=identifier)
            except (
                OSError,
                UnicodeError,
                json.JSONDecodeError,
                TypeError,
                ValueError,
                ValidationError,
                RecursionError,
            ):
                summaries.append(
                    EvidenceSetSummary(
                        path=path,
                        evidence_set_id=identifier,
                        title="不可读取",
                        item_count=None,
                        updated_at=None,
                        readable=False,
                        error="invalid_document",
                    )
                )
                continue
            summaries.append(
                EvidenceSetSummary(
                    path=path,
                    evidence_set_id=evidence_set.evidence_set_id,
                    title=evidence_set.title,
                    item_count=len(evidence_set.items),
                    updated_at=evidence_set.updated_at,
                    readable=True,
                )
            )

        summaries.sort(key=_summary_sort_key)
        return tuple(summaries)

    def _now(self) -> datetime:
        value = self._clock()
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise EvidencePersistenceError("证据集时间必须使用 UTC。")
        if value.utcoffset() != UTC.utcoffset(value):
            raise EvidencePersistenceError("证据集时间必须使用 UTC。")
        return value.astimezone(UTC)

    def _write(
        self,
        path: Path,
        evidence_set: EvidenceSet,
        *,
        replace_existing: bool = True,
    ) -> None:
        payload = _serialize(evidence_set)
        if len(payload.encode("utf-8")) > _MAX_EVIDENCE_SET_BYTES:
            raise ValueError("Evidence Set document exceeds the size limit")
        if replace_existing:
            atomic_write_text(path, payload)
        else:
            atomic_create_text(path, payload)

    def _read(self, path: Path, *, expected_id: str) -> EvidenceSet:
        raw = json.loads(read_regular_text(path, max_bytes=_MAX_EVIDENCE_SET_BYTES))
        if not isinstance(raw, dict) or set(raw) != _DOCUMENT_KEYS:
            raise ValueError("invalid Evidence Set document keys")
        if raw.get("version") != EVIDENCE_SET_VERSION:
            raise ValueError("unsupported Evidence Set version")
        if raw.get("id") != expected_id:
            raise ValueError("Evidence Set ID does not match its filename")
        for field_name in ("created_at", "updated_at"):
            raw[field_name] = _parse_timestamp(raw[field_name], field_name=field_name)
        model_payload = {key: value for key, value in raw.items() if key != "version"}
        evidence_set = EvidenceSet.model_validate(model_payload)
        if evidence_set.evidence_set_id != expected_id:
            raise ValueError("Evidence Set ID does not match its filename")
        return evidence_set


def _serialize(evidence_set: EvidenceSet) -> str:
    payload = {
        "version": EVIDENCE_SET_VERSION,
        "id": evidence_set.evidence_set_id,
        "title": evidence_set.title,
        "items": [item.model_dump(mode="json") for item in evidence_set.items],
        "created_at": _utc_iso(evidence_set.created_at),
        "updated_at": _utc_iso(evidence_set.updated_at),
    }
    return json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n"


def _parse_timestamp(value: object, *, field_name: str) -> datetime:
    if not isinstance(value, str) or _CANONICAL_UTC_DATETIME.fullmatch(value) is None:
        raise ValueError(f"{field_name} is not a canonical UTC timestamp")
    try:
        parsed = datetime.strptime(
            value,
            "%Y-%m-%dT%H:%M:%S.%fZ" if "." in value else "%Y-%m-%dT%H:%M:%SZ",
        )
    except ValueError as exc:
        raise ValueError(f"{field_name} is not a canonical UTC timestamp") from exc
    return parsed.replace(tzinfo=UTC)


def _utc_iso(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    if value.utcoffset() != UTC.utcoffset(value):
        raise ValueError("timestamp must be UTC")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _summary_sort_key(summary: EvidenceSetSummary) -> tuple[int, float, str]:
    if not summary.readable or summary.updated_at is None:
        return (1, 0.0, summary.evidence_set_id)
    return (0, -summary.updated_at.timestamp(), summary.evidence_set_id)


__all__ = ["EVIDENCE_SET_VERSION", "EvidencePersistenceError", "EvidenceSetRepository"]
