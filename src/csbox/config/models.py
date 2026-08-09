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
    capture_key: Literal["f12"] = "f12"


class RenderConfig(_ConfigModel):
    theme: Literal["dark", "light"] = "dark"
    font: str | None = None


class PackConfig(_ConfigModel):
    filename: str = "{id}-{name}-{course}.zip"


class CheckConfig(_ConfigModel):
    large_file_threshold_mb: int = Field(default=50, ge=1, le=4096)


class CSBoxConfig(_ConfigModel):
    locale: str = "zh_CN"
    student: StudentConfig = Field(default_factory=StudentConfig)
    course: CourseConfig = Field(default_factory=CourseConfig)
    lab: LabConfig = Field(default_factory=LabConfig)
    render: RenderConfig = Field(default_factory=RenderConfig)
    pack: PackConfig = Field(default_factory=PackConfig)
    check: CheckConfig = Field(default_factory=CheckConfig)
