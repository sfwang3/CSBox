from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Literal, TypeAlias

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


class _SourceModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class LabCaptureSource(_SourceModel):
    """A stable reference to one canonical Lab Capture."""

    source_type: Literal["lab_capture"] = "lab_capture"
    session_id: str
    capture_id: str

    @field_validator("session_id", "capture_id", mode="before")
    @classmethod
    def _validate_source_id(cls, value: object, info: object) -> str:
        field_name = getattr(info, "field_name", "source_id")
        return validate_identifier(value, field_name=field_name)

    @property
    def equality_key(self) -> tuple[str, str, str]:
        """Return the complete source reference used for duplicate detection."""

        return (self.source_type, self.session_id, self.capture_id)


class ApiStepSource(_SourceModel):
    """A stable reference to one already-persisted API run step."""

    source_type: Literal["api_step"] = "api_step"
    run_id: str
    step_index: int = Field(ge=1)

    @field_validator("run_id", mode="before")
    @classmethod
    def _validate_run_id(cls, value: object) -> str:
        return validate_identifier(value, field_name="run_id")

    @field_validator("step_index", mode="before")
    @classmethod
    def _validate_step_index(cls, value: object) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError("step_index must be a positive integer")
        return value

    @property
    def equality_key(self) -> tuple[str, str, int]:
        """Return the complete source reference used for duplicate detection."""

        return (self.source_type, self.run_id, self.step_index)


EvidenceSourceValue: TypeAlias = Annotated[
    LabCaptureSource | ApiStepSource,
    Field(discriminator="source_type"),
]


def EvidenceSource(**data: object) -> EvidenceSourceValue:
    """Construct a typed source while retaining the v0.5 call syntax.

    Nested Pydantic validation uses ``EvidenceSourceValue``. The factory keeps
    the v0.5 call syntax for the two supported variants while rejecting every
    other discriminator.
    """

    source_type = data.get("source_type")
    if source_type == "lab_capture":
        return LabCaptureSource.model_validate(data)
    if source_type == "api_step":
        return ApiStepSource.model_validate(data)
    raise ValueError("unsupported Evidence source type")


class EvidenceItem(BaseModel):
    """Presentation metadata plus a stable source reference."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source: EvidenceSourceValue
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
    "ApiStepSource",
    "EvidenceSource",
    "EvidenceSourceValue",
    "LabCaptureSource",
    "validate_identifier",
]
