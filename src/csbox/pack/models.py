from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, model_serializer


class PackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_root: Path
    destination: Path | None = None
    dry_run: bool = False
    verify: bool = False
    force: bool = False
    include_manifest: bool = False
    include: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()


class PackPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_root: Path
    destination: Path
    output_filename: str
    project_type: str
    package_manager: str | None = None
    included: tuple[str, ...] = ()
    excluded: tuple[str, ...] = ()
    rejected: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    source_bytes: int = Field(ge=0)
    include_manifest: bool = False
    verification_requested: bool = False
    force: bool = False
    output_exists: bool = False
    include: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()
    _source_fingerprint: str = PrivateAttr(default="")
    _source_file_fingerprints: tuple[tuple[str, str], ...] = PrivateAttr(default=())

    @model_serializer(mode="wrap")
    def _safe_dump(self, handler):
        data = handler(self)
        data.pop("source_fingerprint", None)
        data["source_root"] = "."
        data["destination"] = self.output_filename
        return data

    @property
    def blockers(self) -> tuple[str, ...]:
        blockers = list(self.rejected)
        if self.output_exists and not self.force:
            blockers.append(f"{self.output_filename}:destination-exists")
        return tuple(blockers)

    @property
    def can_publish(self) -> bool:
        return not self.blockers


class PackReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_root: Path
    destination: Path
    entries: tuple[str, ...] = ()
    excluded: tuple[str, ...] = ()
    rejected: tuple[str, ...] = ()
    source_bytes: int = Field(ge=0)
    archive_bytes: int = Field(ge=0)
    verified: bool = False
    verification_status: str = "not_requested"
    project_type: str = "unknown"
    package_manager: str | None = None
    include_manifest: bool = False

    @model_serializer(mode="wrap")
    def _safe_dump(self, handler):
        data = handler(self)
        data["source_root"] = "."
        data["destination"] = self.destination.name
        return data


__all__ = ["PackPlan", "PackReport", "PackRequest"]
