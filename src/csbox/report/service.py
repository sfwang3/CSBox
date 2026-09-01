from __future__ import annotations

from pathlib import Path

from csbox.config.loader import load_config
from csbox.report.models import ReportProfile


def default_report_profile(cwd: Path | str) -> ReportProfile:
    """Build the deterministic profile from existing project metadata."""

    config = load_config(Path(cwd))
    return ReportProfile.default(
        course_name=config.course.name or "",
        student_name=config.student.name or "",
        student_id=config.student.id or "",
    )


__all__ = ["default_report_profile"]
