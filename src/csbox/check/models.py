from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from csbox.check.detectors import FileInventory


class CheckStatus(StrEnum):
    PASS = "PASS"
    WARN = "WARN"
    FAIL = "FAIL"
    SKIP = "SKIP"


class CheckFinding(BaseModel):
    """One deterministic check result without sensitive matched content."""

    model_config = ConfigDict(extra="forbid")

    rule_id: str = Field(min_length=1)
    status: CheckStatus
    message: str
    path: Path | None = None
    line: int | None = Field(default=None, ge=1)
    category: str | None = None


class DetectedProject(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: str = Field(min_length=1)
    root: Path
    marker: str = Field(min_length=1)
    package_manager: str | None = None


class BuildOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid")

    adapter_id: str
    project: DetectedProject
    status: CheckStatus
    message: str
    command: tuple[str, ...] = ()
    exit_code: int | None = None


@dataclass(frozen=True, slots=True)
class CheckContext:
    root: Path
    inventory: FileInventory
    large_file_threshold_bytes: int = 50 * 1024 * 1024


class CheckReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    root: Path
    projects: tuple[DetectedProject, ...] = ()
    findings: tuple[CheckFinding, ...] = ()
    builds: tuple[BuildOutcome, ...] = ()
    deep_scan: CheckFinding | None = None
    text_scan_stats: dict[str, int] = Field(default_factory=dict)

    @property
    def status(self) -> CheckStatus:
        additional = (self.deep_scan,) if self.deep_scan is not None else ()
        statuses = [finding.status for finding in (*self.findings, *self.builds, *additional)]
        if CheckStatus.FAIL in statuses:
            return CheckStatus.FAIL
        if CheckStatus.WARN in statuses:
            return CheckStatus.WARN
        if statuses and all(status is CheckStatus.SKIP for status in statuses):
            return CheckStatus.SKIP
        return CheckStatus.PASS

    @property
    def exit_code(self) -> int:
        return 1 if self.status is CheckStatus.FAIL else 0


__all__ = [
    "BuildOutcome",
    "CheckContext",
    "CheckFinding",
    "CheckReport",
    "CheckStatus",
    "DetectedProject",
]
