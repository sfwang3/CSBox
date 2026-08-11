from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class _ConfigModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class StudentConfig(_ConfigModel):
    id: str | None = None
    name: str | None = None


class CourseConfig(_ConfigModel):
    name: str | None = None


class LabConfig(_ConfigModel):
    shell: Literal["powershell", "pwsh", "bash", "zsh"] | None = None
    capture_key: Literal["f12", "ctrl-space"] = "f12"


class RenderConfig(_ConfigModel):
    theme: Literal["dark", "light"] = "dark"
    font: str | None = None


class PackConfig(_ConfigModel):
    filename: str = "{id}-{name}-{course}.zip"


class CheckConfig(_ConfigModel):
    large_file_threshold_mb: int = Field(default=50, ge=1, le=4096)


class ApiConfig(_ConfigModel):
    variables: dict[str, str] = Field(default_factory=dict)
    response_max_bytes: int = Field(default=262144, ge=1024, le=16 * 1024 * 1024)
    timeout_seconds: float = Field(default=10.0, ge=0.1, le=300.0)


class CSBoxConfig(_ConfigModel):
    locale: str = "zh_CN"
    student: StudentConfig = Field(default_factory=StudentConfig)
    course: CourseConfig = Field(default_factory=CourseConfig)
    lab: LabConfig = Field(default_factory=LabConfig)
    render: RenderConfig = Field(default_factory=RenderConfig)
    pack: PackConfig = Field(default_factory=PackConfig)
    check: CheckConfig = Field(default_factory=CheckConfig)
    api: ApiConfig = Field(default_factory=ApiConfig)
