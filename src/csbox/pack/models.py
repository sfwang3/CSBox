from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field


class PackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_root: Path
    destination: Path | None = None
    verify: bool = False
    force: bool = False
    include: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()


class PackReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_root: Path
    destination: Path
    entries: tuple[str, ...] = ()
    excluded: tuple[str, ...] = ()
    source_bytes: int = Field(ge=0)
    archive_bytes: int = Field(ge=0)
    verified: bool = False


__all__ = ["PackReport", "PackRequest"]
