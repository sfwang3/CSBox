from __future__ import annotations

from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, Field


class EnvironmentSnapshot(BaseModel):
    os_name: str
    os_version: str
    python_version: str
    shell: str
    shell_executable: str | None
    powershell_51_available: bool
    powershell_7_available: bool
    is_wsl: bool
    terminal_columns: int = Field(gt=0)
    terminal_rows: int = Field(gt=0)


class RecentExperiment(BaseModel):
    name: str
    status: str
    duration: str
    demo: bool = True
    capture_count: int = Field(default=0, ge=0)
    platform: str | None = None
    cwd: Path | None = None
    session_id: str | None = None


class RecentApiRun(BaseModel):
    """A metadata-only API run summary safe for Home presentation."""

    id: str
    scenario_name: str
    status: str
    started_at: datetime | None = None
    elapsed_ms: float = Field(default=0.0, ge=0.0)

    @property
    def run_id(self) -> str:
        """Compatibility name for UI adapters that call the identifier a run ID."""

        return self.id


class HomeSnapshot(BaseModel):
    environment: EnvironmentSnapshot
    recent_experiments: list[RecentExperiment] = Field(default_factory=list)
    recent_api_runs: list[RecentApiRun] = Field(default_factory=list)
    check_status: str | None = None
    project_dir: Path | None = None


class Evidence(BaseModel):
    kind: str
    content: str


class RenderedEvidence(BaseModel):
    format_id: str
    content: str


class CheckContext(BaseModel):
    project_dir: Path


class CheckResult(BaseModel):
    rule_id: str
    passed: bool
    summary: str


class ProjectInfo(BaseModel):
    kind: str
    root: Path


class BuildResult(BaseModel):
    adapter_id: str
    success: bool
    artifacts: tuple[str, ...] = ()


class ExportResult(BaseModel):
    format_id: str
    destination: Path
