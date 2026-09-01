from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator


def validate_identifier(value: object, *, field_name: str = "identifier") -> str:
    """Validate a persisted ID that will be used as one filesystem component."""

    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be text")
    if (
        not value
        or value != value.strip()
        or value in {".", ".."}
        or "/" in value
        or "\\" in value
        or "\x00" in value
        or Path(value).name != value
    ):
        raise ValueError(f"{field_name} must be a safe path component")
    return value


def _required_text(value: object, *, field_name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be text")
    if not value.strip():
        raise ValueError(f"{field_name} must not be blank")
    return value


def _optional_text(value: object, *, field_name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be text")
    return value


def _utc_datetime(value: datetime, *, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware UTC")
    if value.utcoffset() != UTC.utcoffset(value):
        raise ValueError(f"{field_name} must be UTC")
    return value.astimezone(UTC)


class EvidenceSource(BaseModel):
    """A stable reference to an existing source artifact."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_type: str
    session_id: str
    capture_id: str

    @field_validator("source_type", mode="before")
    @classmethod
    def _validate_source_type(cls, value: object) -> str:
        return _required_text(value, field_name="source_type")

    @field_validator("session_id", "capture_id", mode="before")
    @classmethod
    def _validate_source_id(cls, value: object, info: object) -> str:
        field_name = getattr(info, "field_name", "source_id")
        return validate_identifier(value, field_name=field_name)

    @property
    def equality_key(self) -> tuple[str, str, str]:
        """Return the complete source reference used for duplicate detection."""

        return (self.source_type, self.session_id, self.capture_id)


class EvidenceItem(BaseModel):
    """Presentation metadata plus a stable source reference."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source: EvidenceSource
    title: str
    caption: str = ""
    note: str = ""

    @field_validator("title", mode="before")
    @classmethod
    def _validate_title(cls, value: object) -> str:
        return _required_text(value, field_name="title")

    @field_validator("caption", "note", mode="before")
    @classmethod
    def _validate_user_text(cls, value: object, info: object) -> str:
        field_name = getattr(info, "field_name", "text")
        return _optional_text(value, field_name=field_name)


class EvidenceSet(BaseModel):
    """An ordered, persisted collection of Evidence Items."""

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    evidence_set_id: str = Field(alias="id")
    title: str
    items: tuple[EvidenceItem, ...] = ()
    created_at: datetime
    updated_at: datetime

    @field_validator("evidence_set_id", mode="before")
    @classmethod
    def _validate_set_id(cls, value: object) -> str:
        return validate_identifier(value, field_name="evidence_set_id")

    @field_validator("title", mode="before")
    @classmethod
    def _validate_set_title(cls, value: object) -> str:
        return _required_text(value, field_name="title")

    @field_validator("created_at", "updated_at")
    @classmethod
    def _validate_timestamp(cls, value: datetime, info: object) -> datetime:
        field_name = getattr(info, "field_name", "timestamp")
        return _utc_datetime(value, field_name=field_name)


@dataclass(frozen=True, slots=True)
class EvidenceSetSummary:
    """Metadata needed to render the Evidence Set list without source access."""

    path: Path
    evidence_set_id: str
    title: str
    item_count: int | None
    updated_at: datetime | None
    readable: bool
    error: str | None = None


__all__ = [
    "EvidenceItem",
    "EvidenceSet",
    "EvidenceSetSummary",
    "EvidenceSource",
    "validate_identifier",
]
