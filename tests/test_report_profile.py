from __future__ import annotations

import json
from pathlib import Path

import pytest

from csbox.evidence.models import EvidenceSet
from csbox.report.models import (
    MAX_REPORT_SECTIONS,
    REPORT_PROFILE_VERSION,
    ReportProfile,
    ReportSection,
)
from csbox.report.repository import ReportProfilePersistenceError, ReportProfileRepository


def test_default_profile_is_separate_and_places_evidence() -> None:
    profile = ReportProfile.default(
        course_name="计算机网络",
        student_name="张三",
        student_id="2026001",
    )

    assert REPORT_PROFILE_VERSION == 1
    assert profile.course_name == "计算机网络"
    assert profile.student_name == "张三"
    assert profile.student_id == "2026001"
    assert profile.sections == (ReportSection(heading="实验记录", include_evidence=True),)
    assert "sections" not in EvidenceSet.model_fields
    assert "course_name" not in EvidenceSet.model_fields


def test_report_profile_rejects_blank_headings_and_duplicate_evidence_slots() -> None:
    with pytest.raises(ValueError, match="heading"):
        ReportSection(heading="   ")

    sections = tuple(
        ReportSection(heading=f"第 {index}") for index in range(MAX_REPORT_SECTIONS + 1)
    )
    with pytest.raises(ValueError, match="sections"):
        ReportProfile(sections=sections)

    with pytest.raises(ValueError, match="evidence"):
        ReportProfile(
            sections=(
                ReportSection(heading="实验结果", include_evidence=True),
                ReportSection(heading="补充结果", include_evidence=True),
            )
        )


def test_report_profile_requires_real_text_values() -> None:
    with pytest.raises(ValueError):
        ReportProfile(course_name=123)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        ReportSection(heading="章节", body=123)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        ReportSection(heading="章节", include_evidence=1)  # type: ignore[arg-type]


def test_profile_repository_round_trips_cjk_and_multiline_text(tmp_path: Path) -> None:
    repository = ReportProfileRepository.from_cwd(tmp_path)
    profile = ReportProfile(
        report_title="课程报告",
        course_name="计算机网络",
        report_date="2026-09-01",
        sections=(
            ReportSection(
                heading="实验分析",
                body="第一行\n第二行 | 不应变成表格",
            ),
        ),
    )

    saved = repository.save("set-1", profile)

    assert saved == profile
    assert repository.path_for("set-1") == tmp_path / ".csbox" / "report-profiles" / "set-1.json"
    assert repository.load("set-1") == profile
    raw = json.loads(repository.path_for("set-1").read_text(encoding="utf-8"))
    assert raw["version"] == 1
    assert raw["profile"]["sections"][0]["body"] == "第一行\n第二行 | 不应变成表格"


def test_profile_repository_returns_default_only_for_missing_document(tmp_path: Path) -> None:
    repository = ReportProfileRepository.from_cwd(tmp_path)
    default = ReportProfile.default(course_name="课程")

    assert repository.load_or_default("missing", default=default) == default

    path = repository.path_for("broken")
    path.parent.mkdir(parents=True)
    path.write_text('{"version": 99, "profile": {}}', encoding="utf-8")
    with pytest.raises(ReportProfilePersistenceError):
        repository.load_or_default("broken", default=default)


@pytest.mark.parametrize(
    "payload",
    [
        {"version": 1, "profile": {}, "extra": True},
        {"version": True, "profile": {}},
        {"version": 1, "profile": {"unexpected": "field"}},
    ],
)
def test_profile_repository_rejects_unknown_or_invalid_envelopes(
    tmp_path: Path,
    payload: dict[str, object],
) -> None:
    repository = ReportProfileRepository.from_cwd(tmp_path)
    path = repository.path_for("broken")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(ReportProfilePersistenceError):
        repository.load("broken")


def test_profile_repository_rejects_unsafe_ids(tmp_path: Path) -> None:
    repository = ReportProfileRepository.from_cwd(tmp_path)

    with pytest.raises(ValueError):
        repository.path_for("../escape")
