from __future__ import annotations

import json
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from xml.etree import ElementTree

import pytest
from PIL import Image

from csbox.core.events import TerminalSize
from csbox.evidence import EvidenceReportExporter, ReportExportError
from csbox.evidence.models import EvidenceItem, EvidenceSet, EvidenceSource
from csbox.evidence.resolver import LabCaptureResolver
from csbox.lab.captures import CaptureStore
from csbox.lab.repository import SessionRepository
from csbox.lab.screen import TerminalCell, TerminalCursor, TerminalSnapshot
from csbox.report.models import ReportProfile, ReportSection


def _snapshot(character: str) -> TerminalSnapshot:
    return TerminalSnapshot(
        rows=1,
        columns=4,
        cells=((TerminalCell(character), TerminalCell(" "), TerminalCell(" "), TerminalCell(" ")),),
        cursor=TerminalCursor(row=0, column=0),
    )


class RecordingRenderer:
    def render(self, snapshot: TerminalSnapshot, destination: Path, theme: object = None) -> Path:
        del snapshot, theme
        destination.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (32, 16), (12, 12, 12)).save(destination, format="PNG")
        return destination


def _fixture(tmp_path: Path) -> tuple[EvidenceSet, SessionRepository]:
    repository = SessionRepository(tmp_path / ".csbox" / "sessions")
    paths = repository.create_starting(
        "网络实验",
        shell="bash",
        shell_version=None,
        size=TerminalSize(columns=80, rows=24),
        cwd=tmp_path,
        session_id="session-1",
        started_at=datetime(2026, 8, 31, 0, 0, tzinfo=UTC),
    )
    paths.cast.write_bytes(b"canonical cast")
    store = CaptureStore(
        paths.captures,
        id_factory=iter(("capture-1", "capture-2")).__next__,
        clock=lambda: datetime(2026, 8, 31, 0, 0, tzinfo=UTC),
    )
    first = store.create_capture(_snapshot("A"), cwd=tmp_path, title="接口")
    second = store.create_capture(_snapshot("B"), cwd=tmp_path, title="路由")
    repository.finish(
        paths,
        "completed",
        ended_at=datetime(2026, 8, 31, 0, 1, tzinfo=UTC),
    )
    evidence_set = EvidenceSet(
        id="set-1",
        title="网络证据",
        items=(
            EvidenceItem(
                source=EvidenceSource(
                    source_type="lab_capture",
                    session_id="session-1",
                    capture_id=first.capture_id,
                ),
                title="查看网络接口",
                note="第一行备注\n第二行备注",
            ),
            EvidenceItem(
                source=EvidenceSource(
                    source_type="lab_capture",
                    session_id="session-1",
                    capture_id=second.capture_id,
                ),
                title="查看路由",
                caption="自定义路由输出",
            ),
        ),
        created_at=datetime(2026, 8, 31, 0, 2, tzinfo=UTC),
        updated_at=datetime(2026, 8, 31, 0, 2, tzinfo=UTC),
    )
    return evidence_set, repository


def _profile() -> ReportProfile:
    return ReportProfile(
        report_title="课程报告",
        course_name="计算机网络",
        course_code="CS-101",
        student_name="张三",
        student_id="2026001",
        instructor="李老师",
        semester="2026 秋",
        report_date="2026-09-01",
        sections=(
            ReportSection(
                heading="实验目的 *用户标题*",
                body="用户目的第一行\n- 不应成为列表 | [链接]",
            ),
            ReportSection(
                heading="实验结果",
                body="用户结果前言",
                include_evidence=True,
            ),
            ReportSection(heading="实验结论", body="用户结论"),
        ),
    )


def _export(tmp_path: Path, profile: ReportProfile) -> tuple[Path, EvidenceSet]:
    evidence_set, repository = _fixture(tmp_path)
    destination = tmp_path / "报告材料"
    EvidenceReportExporter(
        LabCaptureResolver(repository),
        RecordingRenderer(),  # type: ignore[arg-type]
    ).export(evidence_set, destination, report_profile=profile)
    return destination, evidence_set


def test_profile_markdown_preserves_metadata_sections_and_evidence_order(tmp_path: Path) -> None:
    destination, _evidence_set = _export(tmp_path, _profile())
    markdown = (destination / "report.md").read_text(encoding="utf-8")

    assert markdown.startswith("# 课程报告\n")
    assert "| 课程名称 | 计算机网络 |" in markdown
    assert "| 学生编号 | 2026001 |" in markdown
    assert "## 实验目的 \\*用户标题\\*" in markdown
    assert "用户目的第一行<br>- 不应成为列表 \\| \\[链接\\]" in markdown
    assert markdown.index("## 实验目的") < markdown.index("## 实验结果")
    assert markdown.index("## 实验结果") < markdown.index("## 实验结论")
    assert markdown.index("### 查看网络接口") < markdown.index("### 查看路由")
    assert "图 1 查看网络接口" in markdown
    assert "图 2 自定义路由输出" in markdown
    assert "第一行备注<br>第二行备注" in markdown
    assert "目的" in markdown
    assert "实验分析" not in markdown
    assert "/home/" not in markdown


def test_profile_docx_contains_same_order_and_embedded_images(tmp_path: Path) -> None:
    destination, _evidence_set = _export(tmp_path, _profile())

    with zipfile.ZipFile(destination / "report.docx") as package:
        assert package.testzip() is None
        names = set(package.namelist())
        assert len([name for name in names if name.startswith("word/media/")]) == 2
        assert not any(name.startswith("word/fonts/") for name in names)
        document = ElementTree.fromstring(package.read("word/document.xml"))
        relationships = ElementTree.fromstring(package.read("word/_rels/document.xml.rels"))
        text = "".join(document.itertext())

    assert "课程报告" in text
    assert "课程名称" in text and "计算机网络" in text
    assert "用户目的第一行" in text and "用户目的第二行" not in text
    assert "实验目的 *用户标题*" in text
    assert "用户结果前言" in text
    assert "图 1 查看网络接口" in text
    assert "图 2 自定义路由输出" in text
    assert "第一行备注" in text and "第二行备注" in text
    assert text.index("实验目的 *用户标题*") < text.index("实验结果")
    assert text.index("实验结果") < text.index("查看网络接口")
    assert text.index("查看网络接口") < text.index("查看路由")
    assert not any(
        element.attrib.get("TargetMode") == "External"
        for element in relationships
        if element.attrib.get("Type", "").endswith("/image")
    )


def test_profile_without_evidence_marker_appends_builtin_evidence_section(tmp_path: Path) -> None:
    profile = ReportProfile(
        report_title="无标记报告",
        sections=(ReportSection(heading="用户章节", body="用户正文"),),
    )
    destination, _evidence_set = _export(tmp_path, profile)
    markdown = (destination / "report.md").read_text(encoding="utf-8")

    assert markdown.index("## 用户章节") < markdown.index("## 实验记录")
    assert "### 查看网络接口" in markdown
    assert "### 查看路由" in markdown


@pytest.mark.parametrize("filename", ["report.md", "report.docx"])
def test_profile_refresh_preserves_modified_report_targets(
    tmp_path: Path,
    filename: str,
) -> None:
    destination, evidence_set = _export(tmp_path, _profile())
    target = destination / filename
    manual_bytes = target.read_bytes() + b"\nmanual edit"
    target.write_bytes(manual_bytes)

    with pytest.raises(ReportExportError) as raised:
        repository = SessionRepository(tmp_path / ".csbox" / "sessions")
        EvidenceReportExporter(
            LabCaptureResolver(repository),
            RecordingRenderer(),  # type: ignore[arg-type]
        ).export(evidence_set, destination, force=True, report_profile=_profile())

    assert raised.value.kind == "existing_target"
    assert target.read_bytes() == manual_bytes


def test_profile_refresh_does_not_trust_report_hash_in_asset_manifest(
    tmp_path: Path,
) -> None:
    destination, evidence_set = _export(tmp_path, _profile())
    manifest_path = destination / ".csbox-generated-report.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    report_entry = next(
        item for item in manifest["generated"] if item["path"] == "report.md"
    )
    manifest["generated"].remove(report_entry)
    manifest["files"].append(report_entry)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    original_bytes = (destination / "report.md").read_bytes()

    changed_profile = _profile().model_copy(update={"report_title": "新报告"})
    with pytest.raises(ReportExportError) as raised:
        EvidenceReportExporter(
            LabCaptureResolver(SessionRepository(tmp_path / ".csbox" / "sessions")),
            RecordingRenderer(),  # type: ignore[arg-type]
        ).export(evidence_set, destination, force=True, report_profile=changed_profile)

    assert raised.value.kind == "existing_target"
    assert (destination / "report.md").read_bytes() == original_bytes


def test_profile_refresh_replaces_unchanged_owned_report_targets(tmp_path: Path) -> None:
    destination, evidence_set = _export(tmp_path, _profile())
    result = EvidenceReportExporter(
        LabCaptureResolver(SessionRepository(tmp_path / ".csbox" / "sessions")),
        RecordingRenderer(),  # type: ignore[arg-type]
    ).export(evidence_set, destination, force=True, report_profile=_profile())

    assert result.destination == destination
