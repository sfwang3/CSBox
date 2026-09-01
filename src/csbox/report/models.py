from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

MAX_REPORT_SECTIONS = 8
REPORT_PROFILE_VERSION = 1


def _text(value: object, *, field_name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be text")
    return value


def _heading(value: object) -> str:
    value = _text(value, field_name="heading")
    if not value.strip():
        raise ValueError("heading must not be blank")
    return value


class ReportSection(BaseModel):
    """One ordered user-authored section in a built-in report layout."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    heading: str
    body: str = ""
    include_evidence: bool = False

    @field_validator("heading", mode="before")
    @classmethod
    def _validate_heading(cls, value: object) -> str:
        return _heading(value)

    @field_validator("body", mode="before")
    @classmethod
    def _validate_body(cls, value: object) -> str:
        return _text(value, field_name="body")


class ReportProfile(BaseModel):
    """Course metadata and authored structure kept outside EvidenceSet."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    report_title: str = ""
    course_name: str = ""
    course_code: str = ""
    student_name: str = ""
    student_id: str = ""
    instructor: str = ""
    semester: str = ""
    report_date: str = ""
    sections: tuple[ReportSection, ...] = ()

    _TEXT_FIELDS: ClassVar[tuple[str, ...]] = (
        "report_title",
        "course_name",
        "course_code",
        "student_name",
        "student_id",
        "instructor",
        "semester",
        "report_date",
    )

    @field_validator(
        "report_title",
        "course_name",
        "course_code",
        "student_name",
        "student_id",
        "instructor",
        "semester",
        "report_date",
        mode="before",
    )
    @classmethod
    def _validate_text(cls, value: object, info: object) -> str:
        return _text(value, field_name=getattr(info, "field_name", "value"))

    @field_validator("sections", mode="before")
    @classmethod
    def _normalize_sections(cls, value: object) -> tuple[ReportSection, ...]:
        if isinstance(value, (list, tuple)):
            return tuple(value)
        raise ValueError("sections must be a sequence")

    @model_validator(mode="after")
    def _validate_sections(self) -> ReportProfile:
        if len(self.sections) > MAX_REPORT_SECTIONS:
            raise ValueError(f"sections must contain at most {MAX_REPORT_SECTIONS} items")
        if sum(section.include_evidence for section in self.sections) > 1:
            raise ValueError("sections may contain at most one evidence section")
        return self

    @classmethod
    def default(
        cls,
        *,
        course_name: str = "",
        student_name: str = "",
        student_id: str = "",
    ) -> ReportProfile:
        """Return the deterministic built-in structure with supplied metadata defaults."""

        return cls(
            course_name=course_name,
            student_name=student_name,
            student_id=student_id,
            sections=(ReportSection(heading="实验记录", include_evidence=True),),
        )


__all__ = [
    "MAX_REPORT_SECTIONS",
    "REPORT_PROFILE_VERSION",
    "ReportProfile",
    "ReportSection",
]
