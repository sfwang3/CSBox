from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from csbox.lab.screen import TerminalSnapshot


class SessionMetadata(BaseModel):
    """Version-independent metadata stored next to a terminal recording."""

    model_config = ConfigDict(populate_by_name=True)

    session_id: str = Field(alias="id", min_length=1)
    experiment_name: str = Field(alias="name", min_length=1)
    status: Literal["starting", "running", "completed", "interrupted", "failed"] = "starting"
    status_reason: str | None = Field(default=None, alias="statusReason")
    exit_code: int | None = Field(default=None, alias="exitCode")
    owner_pid: int | None = Field(default=None, alias="ownerPid")
    owner_token: str | None = Field(default=None, alias="ownerToken")
    started_at: datetime = Field(alias="startedAt")
    ended_at: datetime | None = Field(default=None, alias="endedAt")
    platform: str = Field(min_length=1)
    shell: str = Field(min_length=1)
    shell_version: str | None = Field(alias="shellVersion")
    initial_rows: int = Field(alias="initialRows", gt=0)
    initial_columns: int = Field(alias="initialColumns", gt=0)
    cwd: Path
    csbox_version: str = Field(alias="csboxVersion", min_length=1)

    @field_validator("started_at", "ended_at")
    @classmethod
    def require_utc_datetime(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("session timestamps must be timezone-aware")
        return value.astimezone(UTC)


class SessionPaths:
    """Canonical paths belonging to one session directory."""

    __slots__ = ("root",)

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)

    @property
    def cast(self) -> Path:
        return self.root / "session.cast"

    @property
    def metadata(self) -> Path:
        return self.root / "metadata.json"

    @property
    def captures(self) -> Path:
        return self.root / "captures.json"

    @property
    def checkpoints(self) -> Path:
        return self.root / "checkpoints.json"

    @property
    def owner_lock(self) -> Path:
        return self.root / "owner.lock"


class CaptureRecord(BaseModel):
    """A persisted immutable terminal snapshot and its user-facing metadata."""

    model_config = ConfigDict(frozen=True, populate_by_name=True)

    capture_id: str = Field(alias="id", min_length=1)
    title: str = ""
    created_at: datetime = Field(alias="createdAt")
    timestamp: float = Field(ge=0)
    rows: int = Field(gt=0)
    columns: int = Field(gt=0)
    cwd: Path
    command: str | None = None
    snapshot: TerminalSnapshot

    @field_validator("created_at")
    @classmethod
    def require_utc_datetime(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("capture timestamp must be timezone-aware")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def dimensions_match_snapshot(self) -> CaptureRecord:
        if self.rows != self.snapshot.rows or self.columns != self.snapshot.columns:
            raise ValueError("capture dimensions must match snapshot")
        if len(self.snapshot.cells) != self.rows or any(
            len(row) != self.columns for row in self.snapshot.cells
        ):
            raise ValueError("snapshot cell geometry does not match its dimensions")
        return self
