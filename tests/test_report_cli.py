from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from csbox.cli.main import app
from csbox.core.events import TerminalSize
from csbox.evidence.models import EvidenceItem, EvidenceSet, EvidenceSource
from csbox.evidence.repository import EvidenceSetRepository
from csbox.lab.captures import CaptureStore
from csbox.lab.repository import SessionRepository
from csbox.lab.screen import TerminalCell, TerminalCursor, TerminalSnapshot
from csbox.report.models import ReportProfile, ReportSection
from csbox.report.repository import ReportProfileRepository
from csbox.report.service import default_report_profile


def _snapshot() -> TerminalSnapshot:
    return TerminalSnapshot(
        rows=1,
        columns=4,
        cells=((TerminalCell("中"), TerminalCell(" "), TerminalCell(" "), TerminalCell(" ")),),
        cursor=TerminalCursor(row=0, column=0),
    )


def _project_with_evidence(tmp_path: Path) -> EvidenceSet:
    sessions = SessionRepository(tmp_path / ".csbox" / "sessions")
    paths = sessions.create_starting(
        "网络实验",
        shell="bash",
        shell_version=None,
        size=TerminalSize(columns=80, rows=24),
        cwd=tmp_path,
        session_id="session-1",
        started_at=datetime(2026, 8, 31, 0, 0, tzinfo=UTC),
    )
    paths.cast.write_bytes(b"canonical cast")
    capture = CaptureStore(
        paths.captures,
        id_factory=lambda: "capture-1",
        clock=lambda: datetime(2026, 8, 31, 0, 0, tzinfo=UTC),
    ).create_capture(_snapshot(), cwd=tmp_path, title="接口")
    sessions.finish(
        paths,
        "completed",
        ended_at=datetime(2026, 8, 31, 0, 1, tzinfo=UTC),
    )
    repository = EvidenceSetRepository(
        tmp_path / ".csbox" / "evidence",
        id_factory=lambda: "set-1",
        clock=lambda: datetime(2026, 8, 31, 0, 2, tzinfo=UTC),
    )
    created = repository.create("网络证据")
    return repository.save(
        created.model_copy(
            update={
                "items": (
                    EvidenceItem(
                        source=EvidenceSource(
                            source_type="lab_capture",
                            session_id="session-1",
                            capture_id=capture.capture_id,
                        ),
                        title="查看接口",
                    ),
                )
            }
        )
    )


def test_default_report_profile_uses_existing_config_without_generating_prose(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / ".csbox" / "config.toml"
    config_path.parent.mkdir()
    config_path.write_text(
        '[student]\nid = "2026001"\nname = "张三"\n[course]\nname = "计算机网络"\n',
        encoding="utf-8",
    )

    profile = default_report_profile(tmp_path)

    assert profile.student_id == "2026001"
    assert profile.student_name == "张三"
    assert profile.course_name == "计算机网络"
    assert all(section.body == "" for section in profile.sections)


def test_default_report_profile_maps_all_existing_course_and_student_defaults(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / ".csbox" / "config.toml"
    config_path.parent.mkdir()
    config_path.write_text(
        """[student]
id = "2026001"
name = "张三"
[course]
name = "计算机网络"
code = "CS201"
instructor = "王老师"
semester = "2026 秋"
""",
        encoding="utf-8",
    )

    profile = default_report_profile(tmp_path)

    assert profile.course_name == "计算机网络"
    assert profile.course_code == "CS201"
    assert profile.instructor == "王老师"
    assert profile.semester == "2026 秋"
    assert profile.student_name == "张三"
    assert profile.student_id == "2026001"


def test_report_export_cli_uses_saved_profile_and_prints_outputs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    evidence_set = _project_with_evidence(tmp_path)
    profile = ReportProfile(
        report_title="课程报告",
        course_name="计算机网络",
        sections=(ReportSection(heading="用户章节", body="用户填写的正文", include_evidence=True),),
    )
    ReportProfileRepository.from_cwd(tmp_path).save(evidence_set.evidence_set_id, profile)
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(
        app,
        ["report", "export", evidence_set.evidence_set_id, "--output", "报告输出"],
    )

    assert result.exit_code == 0, result.stdout
    destination = tmp_path / "报告输出"
    output_text = result.stdout.replace("\n", "")
    assert f"Markdown：{destination / 'report.md'}" in output_text
    assert f"DOCX：{destination / 'report.docx'}" in output_text
    assert "用户填写的正文" in (destination / "report.md").read_text(encoding="utf-8")
    assert (destination / "report.docx").is_file()
    assert "\x1b[" not in result.stdout
