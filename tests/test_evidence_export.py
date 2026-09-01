import zipfile
from datetime import UTC, datetime
from pathlib import Path
from xml.etree import ElementTree

import pytest
from PIL import Image

from csbox.core.events import TerminalSize
from csbox.evidence import (
    EvidenceReportExporter,
    ReportExportError,
    ReportExportPhase,
)
from csbox.evidence import exporter as exporter_module
from csbox.evidence.models import EvidenceItem, EvidenceSet, EvidenceSource
from csbox.evidence.resolver import LabCaptureResolver
from csbox.lab.captures import CaptureStore
from csbox.lab.repository import SessionRepository
from csbox.lab.screen import TerminalCell, TerminalCursor, TerminalSnapshot


def _snapshot(character: str) -> TerminalSnapshot:
    return TerminalSnapshot(
        rows=1,
        columns=4,
        cells=((TerminalCell(character), TerminalCell(" "), TerminalCell(" "), TerminalCell(" ")),),
        cursor=TerminalCursor(row=0, column=0),
    )


class RecordingRenderer:
    def __init__(self) -> None:
        self.snapshots: list[TerminalSnapshot] = []

    def render(self, snapshot: TerminalSnapshot, destination: Path, theme: object = None) -> Path:
        del theme
        self.snapshots.append(snapshot)
        destination.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (32, 16), (12, 12, 12)).save(destination, format="PNG")
        return destination


def _evidence_fixture(tmp_path: Path) -> tuple[EvidenceSet, SessionRepository, object, object]:
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
    now = datetime(2026, 8, 31, 0, 2, tzinfo=UTC)
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
            ),
            EvidenceItem(
                source=EvidenceSource(
                    source_type="lab_capture",
                    session_id="session-1",
                    capture_id=second.capture_id,
                ),
                title="查看路由",
            ),
        ),
        created_at=now,
        updated_at=now,
    )
    return evidence_set, repository, first, second


def test_evidence_package_exposes_report_exporter() -> None:
    import csbox.evidence as evidence

    assert hasattr(evidence, "EvidenceReportExporter")


def test_export_resolves_each_canonical_capture_and_preserves_lab_sources(
    tmp_path: Path,
) -> None:
    evidence_set, repository, first, second = _evidence_fixture(tmp_path)
    paths = repository.resolve("session-1")
    source_paths = (paths.captures, paths.metadata, paths.cast)
    before = {path: path.read_bytes() for path in source_paths}
    renderer = RecordingRenderer()
    destination = tmp_path / "报告材料"

    result = EvidenceReportExporter(
        LabCaptureResolver(repository),
        renderer,  # type: ignore[arg-type]
    ).export(evidence_set, destination)

    assert result.evidence_item_count == 2
    assert len(result.images) == 2
    assert all(path.is_file() for path in result.images)
    assert renderer.snapshots == [first.snapshot, second.snapshot]
    assert {path: path.read_bytes() for path in source_paths} == before


def _rich_fixture(tmp_path: Path) -> tuple[EvidenceSet, SessionRepository]:
    evidence_set, repository, first, second = _evidence_fixture(tmp_path)
    paths = repository.resolve("session-1")
    third_store = CaptureStore(
        paths.captures,
        id_factory=lambda: "capture-3",
        clock=lambda: datetime(2026, 8, 31, 0, 0, tzinfo=UTC),
    )
    third = third_store.create_capture(
        _snapshot("C"),
        cwd=tmp_path,
        title="DNS 查询",
    )
    now = datetime(2026, 8, 31, 0, 3, tzinfo=UTC)
    return (
        EvidenceSet(
            id=evidence_set.evidence_set_id,
            title="计算机网络实验一",
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
                    title="查看路由 route",
                    caption="自定义路由输出",
                ),
                EvidenceItem(
                    source=EvidenceSource(
                        source_type="lab_capture",
                        session_id="session-1",
                        capture_id=third.capture_id,
                    ),
                    title="DNS 查询",
                    note="",
                ),
            ),
            created_at=now,
            updated_at=now,
        ),
        repository,
    )


def test_markdown_preserves_order_captions_notes_and_relative_assets(tmp_path: Path) -> None:
    evidence_set, repository = _rich_fixture(tmp_path)
    destination = tmp_path / "中文 destination with spaces"

    result = EvidenceReportExporter(
        LabCaptureResolver(repository),
        RecordingRenderer(),  # type: ignore[arg-type]
    ).export(evidence_set, destination)

    markdown = result.markdown.read_text(encoding="utf-8")
    assert markdown.index("## 查看网络接口") < markdown.index("## 查看路由 route")
    assert markdown.index("## 查看路由 route") < markdown.index("## DNS 查询")
    assert "图 1 查看网络接口" in markdown
    assert "图 2 自定义路由输出" in markdown
    assert "图 3 DNS 查询" in markdown
    assert "第一行备注<br>第二行备注" in markdown
    assert "assets/01-" in markdown
    assert "assets/02-" in markdown
    assert "assets/03-" in markdown
    assert all(not line.startswith("/home/") for line in markdown.splitlines())
    assert ".csbox" not in markdown
    assert all(path.is_file() for path in result.images)
    assert len(result.images) == 3
    assert all(len(path.name.encode("utf-8")) <= 240 for path in result.images)


def test_markdown_multiline_user_text_cannot_create_markdown_blocks(tmp_path: Path) -> None:
    evidence_set, repository, first, _second = _evidence_fixture(tmp_path)
    multiline = evidence_set.model_copy(
        update={
            "title": "报告标题第一行\n---\n报告标题第三行",
            "items": (
                EvidenceItem(
                    source=EvidenceSource(
                        source_type="lab_capture",
                        session_id="session-1",
                        capture_id=first.capture_id,
                    ),
                    title="条目第一行\n`代码` [链接](url)",
                    caption="图注第一行\n*不应斜体*",
                    note="备注第一行\n- 不应成为列表",
                ),
            ),
        },
    )

    destination = tmp_path / "multiline"
    result = EvidenceReportExporter(
        LabCaptureResolver(repository),
        RecordingRenderer(),  # type: ignore[arg-type]
    ).export(multiline, destination)

    markdown = result.markdown.read_text(encoding="utf-8")
    assert "<span>报告标题第一行<br>---<br>报告标题第三行</span>" in markdown
    assert "<span>条目第一行<br>\\`代码\\` \\[链接\\]\\(url\\)</span>" in markdown
    assert "<span>图注第一行<br>\\*不应斜体\\*</span>" in markdown
    assert "<span>备注第一行<br>- 不应成为列表</span>" in markdown
    assert "\n---\n" not in markdown
    assert "\n> 不应成为引用块\n" not in markdown


def test_docx_is_valid_and_embeds_one_image_per_item(tmp_path: Path) -> None:
    evidence_set, repository = _rich_fixture(tmp_path)

    result = EvidenceReportExporter(
        LabCaptureResolver(repository),
        RecordingRenderer(),  # type: ignore[arg-type]
    ).export(evidence_set, tmp_path / "report")

    with zipfile.ZipFile(result.docx) as package:
        assert package.testzip() is None
        names = set(package.namelist())
        assert "[Content_Types].xml" in names
        assert "word/document.xml" in names
        assert len([name for name in names if name.startswith("word/media/")]) == 3
        assert not any(name.startswith("word/fonts/") for name in names)
        relationships = ElementTree.fromstring(package.read("word/_rels/document.xml.rels"))
        assert not any(
            element.attrib.get("TargetMode") == "External"
            for element in relationships
            if element.attrib.get("Type", "").endswith("/image")
        )
        document = ElementTree.fromstring(package.read("word/document.xml"))
        text = "".join(document.itertext())

    assert "计算机网络实验一" in text
    assert "查看网络接口" in text
    assert "查看路由 route" in text
    assert "DNS 查询" in text
    assert "图 1 查看网络接口" in text
    assert "图 2 自定义路由输出" in text
    assert "图 3 DNS 查询" in text
    assert "第一行备注" in text
    assert "第二行备注" in text


def test_missing_sources_block_export_and_report_all_affected_titles(tmp_path: Path) -> None:
    evidence_set, repository, *_captures = _evidence_fixture(tmp_path)
    now = datetime(2026, 8, 31, 0, 3, tzinfo=UTC)
    blocked = EvidenceSet(
        id=evidence_set.evidence_set_id,
        title=evidence_set.title,
        items=(
            *evidence_set.items,
            EvidenceItem(
                source=EvidenceSource(
                    source_type="lab_capture",
                    session_id="missing-session",
                    capture_id="missing-capture",
                ),
                title="缺失会话",
            ),
            EvidenceItem(
                source=EvidenceSource(
                    source_type="lab_capture",
                    session_id="session-1",
                    capture_id="missing-capture",
                ),
                title="缺失捕获",
            ),
        ),
        created_at=now,
        updated_at=now,
    )
    phases: list[ReportExportPhase] = []
    destination = tmp_path / "blocked"

    with pytest.raises(ReportExportError) as raised:
        EvidenceReportExporter(
            LabCaptureResolver(repository),
            RecordingRenderer(),  # type: ignore[arg-type]
        ).export(blocked, destination, phase_callback=phases.append)

    assert raised.value.unavailable_titles == ("缺失会话", "缺失捕获")
    assert phases == [ReportExportPhase.GENERATING, ReportExportPhase.FAILED]
    assert not destination.exists()
    assert not tuple(tmp_path.glob(".csbox-report-*.partial"))


def test_corrupt_capture_file_blocks_even_when_backup_contains_capture(tmp_path: Path) -> None:
    evidence_set, repository, *_captures = _evidence_fixture(tmp_path)
    captures_path = repository.resolve("session-1").captures
    captures_path.write_text("{broken", encoding="utf-8")

    with pytest.raises(ReportExportError) as raised:
        EvidenceReportExporter(
            LabCaptureResolver(repository),
            RecordingRenderer(),  # type: ignore[arg-type]
        ).export(evidence_set, tmp_path / "corrupt")

    assert raised.value.unavailable_titles == ("查看网络接口", "查看路由")


def test_existing_destination_requires_force_and_force_keeps_unrelated_files(
    tmp_path: Path,
) -> None:
    evidence_set, repository = _rich_fixture(tmp_path)
    destination = tmp_path / "existing"
    exporter = EvidenceReportExporter(
        LabCaptureResolver(repository),
        RecordingRenderer(),  # type: ignore[arg-type]
    )
    exporter.export(evidence_set, destination)
    unrelated = destination / "notes.txt"
    unrelated.write_text("user content", encoding="utf-8")
    before = {path: path.read_bytes() for path in destination.rglob("*") if path.is_file()}

    with pytest.raises(ReportExportError) as raised:
        exporter.export(evidence_set, destination)
    assert raised.value.kind == "existing_target"
    exporter.export(evidence_set, destination, force=True)

    assert unrelated.read_text(encoding="utf-8") == "user content"
    assert all(path.is_file() for path in destination.glob("assets/*.png"))
    assert destination.joinpath("report.md").read_bytes() != b""
    assert set(before) - {unrelated} <= {
        *destination.rglob("*"),
    }


def test_existing_unmanifested_asset_is_not_overwritten(tmp_path: Path) -> None:
    evidence_set, repository, *_captures = _evidence_fixture(tmp_path)
    destination = tmp_path / "collision"
    assets = destination / "assets"
    assets.mkdir(parents=True)
    expected_name = "01-查看网络接口.png"
    (assets / expected_name).write_bytes(b"user image")

    with pytest.raises(ReportExportError) as raised:
        EvidenceReportExporter(
            LabCaptureResolver(repository),
            RecordingRenderer(),  # type: ignore[arg-type]
        ).export(evidence_set, destination, force=True)

    assert raised.value.kind == "existing_target"
    assert (assets / expected_name).read_bytes() == b"user image"
    assert not (destination / "report.md").exists()


def test_writer_failure_leaves_previous_valid_bundle_unchanged(tmp_path: Path) -> None:
    evidence_set, repository = _rich_fixture(tmp_path)
    destination = tmp_path / "retry"
    exporter = EvidenceReportExporter(
        LabCaptureResolver(repository),
        RecordingRenderer(),  # type: ignore[arg-type]
    )
    exporter.export(evidence_set, destination)
    before = {path: path.read_bytes() for path in destination.rglob("*") if path.is_file()}

    def fail_writer(*_args: object) -> None:
        raise RuntimeError("writer failed")

    with pytest.raises(ReportExportError):
        EvidenceReportExporter(
            LabCaptureResolver(repository),
            RecordingRenderer(),  # type: ignore[arg-type]
            docx_writer=fail_writer,  # type: ignore[arg-type]
        ).export(evidence_set, destination, force=True)

    after = {path: path.read_bytes() for path in destination.rglob("*") if path.is_file()}
    assert after == before
    assert not tuple(tmp_path.glob(".csbox-report-*.partial"))


def test_publication_failure_rolls_back_replaced_and_created_targets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evidence_set, repository = _rich_fixture(tmp_path)
    destination = tmp_path / "publication"
    exporter = EvidenceReportExporter(
        LabCaptureResolver(repository),
        RecordingRenderer(),  # type: ignore[arg-type]
    )
    exporter.export(evidence_set, destination)
    before = {path: path.read_bytes() for path in destination.rglob("*") if path.is_file()}
    original_safe_rename = exporter_module.safe_rename
    failed = False

    def fail_once(source: Path, target: Path, *, replace_existing: bool = True) -> None:
        nonlocal failed
        if target.name == "report.docx" and not failed:
            failed = True
            raise OSError("simulated publication failure")
        original_safe_rename(source, target, replace_existing=replace_existing)

    monkeypatch.setattr(exporter_module, "safe_rename", fail_once)
    with pytest.raises(ReportExportError):
        exporter.export(evidence_set, destination, force=True)

    after = {path: path.read_bytes() for path in destination.rglob("*") if path.is_file()}
    assert failed
    assert after == before
    assert not tuple(tmp_path.glob(".csbox-report-*.partial"))


def test_destination_regular_file_is_rejected_without_writing(tmp_path: Path) -> None:
    evidence_set, repository, *_captures = _evidence_fixture(tmp_path)
    destination = tmp_path / "not-a-directory"
    destination.write_text("keep", encoding="utf-8")

    with pytest.raises(ReportExportError):
        EvidenceReportExporter(
            LabCaptureResolver(repository),
            RecordingRenderer(),  # type: ignore[arg-type]
        ).export(evidence_set, destination)

    assert destination.read_text(encoding="utf-8") == "keep"


def test_new_destination_created_between_preflight_and_publish_is_not_used(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evidence_set, repository, *_captures = _evidence_fixture(tmp_path)
    original_new_staging = exporter_module._new_staging
    destination = tmp_path / "raced"

    def stage_then_create_racer(output: Path) -> Path:
        staging = original_new_staging(output)
        output.mkdir()
        (output / "racer.txt").write_text("not ours", encoding="utf-8")
        return staging

    monkeypatch.setattr(exporter_module, "_new_staging", stage_then_create_racer)
    with pytest.raises(ReportExportError) as raised:
        EvidenceReportExporter(
            LabCaptureResolver(repository),
            RecordingRenderer(),  # type: ignore[arg-type]
        ).export(evidence_set, destination)

    assert raised.value.kind == "publication"
    assert (destination / "racer.txt").read_text(encoding="utf-8") == "not ours"
    assert not (destination / "report.md").exists()
    assert not tuple(tmp_path.glob(".csbox-report-*.partial"))


def test_unowned_asset_race_is_not_moved_or_deleted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evidence_set, repository, *_captures = _evidence_fixture(tmp_path)
    destination = tmp_path / "unowned-asset-race"
    (destination / "assets").mkdir(parents=True)
    expected_asset = destination / "assets" / "01-查看网络接口.png"
    original_new_staging = exporter_module._new_staging
    original_path_metadata = exporter_module._path_metadata
    staging_created = False
    metadata_checks = 0

    def stage_then_arm(output: Path) -> Path:
        nonlocal staging_created
        staging = original_new_staging(output)
        staging_created = True
        return staging

    def metadata_with_racer(path: Path) -> object:
        nonlocal metadata_checks
        metadata = original_path_metadata(path)
        if staging_created and path == expected_asset and metadata_checks == 0:
            metadata_checks += 1
            expected_asset.write_bytes(b"user-owned")
        return metadata

    monkeypatch.setattr(exporter_module, "_new_staging", stage_then_arm)
    monkeypatch.setattr(exporter_module, "_path_metadata", metadata_with_racer)

    with pytest.raises(ReportExportError) as raised:
        EvidenceReportExporter(
            LabCaptureResolver(repository),
            RecordingRenderer(),  # type: ignore[arg-type]
        ).export(evidence_set, destination, force=True)

    assert raised.value.kind == "existing_target"
    assert raised.value.destination == destination
    assert expected_asset.read_bytes() == b"user-owned"
    assert not (destination / "report.md").exists()
    assert not tuple(tmp_path.glob(".csbox-report-*.partial"))


@pytest.mark.parametrize(
    "raced_name",
    ("report.md", "report.docx", ".csbox-generated-report.json"),
)
def test_unowned_report_target_race_is_not_moved_or_deleted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    raced_name: str,
) -> None:
    evidence_set, repository, *_captures = _evidence_fixture(tmp_path)
    destination = tmp_path / "unowned-report-target-race"
    (destination / "assets").mkdir(parents=True)
    raced_target = destination / raced_name
    original_new_staging = exporter_module._new_staging
    original_path_metadata = exporter_module._path_metadata
    staging_created = False
    racer_created = False

    def stage_then_arm(output: Path) -> Path:
        nonlocal staging_created
        staging = original_new_staging(output)
        staging_created = True
        return staging

    def metadata_with_racer(path: Path) -> object:
        nonlocal racer_created
        metadata = original_path_metadata(path)
        if staging_created and path == raced_target and not racer_created:
            racer_created = True
            raced_target.write_bytes(b"user-owned")
        return metadata

    monkeypatch.setattr(exporter_module, "_new_staging", stage_then_arm)
    monkeypatch.setattr(exporter_module, "_path_metadata", metadata_with_racer)

    with pytest.raises(ReportExportError) as raised:
        EvidenceReportExporter(
            LabCaptureResolver(repository),
            RecordingRenderer(),  # type: ignore[arg-type]
        ).export(evidence_set, destination, force=True)

    assert raised.value.kind == "existing_target"
    assert raised.value.destination == destination
    assert raced_target.read_bytes() == b"user-owned"
    assert not tuple(tmp_path.glob(".csbox-report-*.partial"))


def test_new_destination_publication_failure_rolls_back_created_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evidence_set, repository, *_captures = _evidence_fixture(tmp_path)
    destination = tmp_path / "new-publication-failure"
    original_safe_rename = exporter_module.safe_rename
    failed = False

    def fail_report_markdown(
        source: Path,
        target: Path,
        *,
        replace_existing: bool = True,
    ) -> None:
        nonlocal failed
        if target.name == "report.md" and not failed:
            failed = True
            raise OSError("simulated new destination publication failure")
        original_safe_rename(source, target, replace_existing=replace_existing)

    monkeypatch.setattr(exporter_module, "safe_rename", fail_report_markdown)
    with pytest.raises(ReportExportError) as raised:
        EvidenceReportExporter(
            LabCaptureResolver(repository),
            RecordingRenderer(),  # type: ignore[arg-type]
        ).export(evidence_set, destination)

    assert raised.value.kind == "publication"
    assert failed
    assert not destination.exists()
    assert not tuple(tmp_path.glob(".csbox-report-*.partial"))
    assert not tuple(tmp_path.glob(".csbox-recovery-*.bak"))


def test_rollback_does_not_delete_byte_identical_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evidence_set, repository, *_captures = _evidence_fixture(tmp_path)
    destination = tmp_path / "rollback-race"
    expected_asset = destination / "assets" / "01-查看网络接口.png"
    original_safe_rename = exporter_module.safe_rename
    original_path_metadata = exporter_module._path_metadata
    publication_failed = False
    raced = False
    racer_content: bytes | None = None

    def fail_report_markdown(
        source: Path,
        target: Path,
        *,
        replace_existing: bool = True,
    ) -> None:
        nonlocal publication_failed
        if target.name == "report.md" and not publication_failed:
            publication_failed = True
            raise OSError("simulated rollback race")
        original_safe_rename(source, target, replace_existing=replace_existing)

    def replace_asset_after_rollback_stat(path: Path) -> object:
        nonlocal raced, racer_content
        metadata = original_path_metadata(path)
        if publication_failed and path == expected_asset and not raced and metadata is not None:
            content = path.read_bytes()
            path.unlink()
            path.write_bytes(content)
            racer_content = content
            raced = True
            metadata = original_path_metadata(path)
        return metadata

    monkeypatch.setattr(exporter_module, "safe_rename", fail_report_markdown)
    monkeypatch.setattr(exporter_module, "_path_metadata", replace_asset_after_rollback_stat)

    with pytest.raises(ReportExportError) as raised:
        EvidenceReportExporter(
            LabCaptureResolver(repository),
            RecordingRenderer(),  # type: ignore[arg-type]
        ).export(evidence_set, destination)

    assert raised.value.kind == "recovery"
    assert publication_failed
    assert raced
    assert racer_content is not None
    assert expected_asset.read_bytes() == racer_content
    assert not (destination / "report.md").exists()
    assert not tuple(tmp_path.glob(".csbox-report-*.partial"))


def test_destination_inside_lab_source_root_is_rejected(tmp_path: Path) -> None:
    evidence_set, repository, *_captures = _evidence_fixture(tmp_path)
    destination = repository.root / "session-1" / "report"

    with pytest.raises(ReportExportError) as raised:
        EvidenceReportExporter(
            LabCaptureResolver(repository),
            RecordingRenderer(),  # type: ignore[arg-type]
        ).export(evidence_set, destination)

    assert raised.value.kind == "destination"
    assert not destination.exists()


def test_relative_destination_is_supported(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    evidence_set, repository, *_captures = _evidence_fixture(tmp_path)
    monkeypatch.chdir(tmp_path)
    destination = Path("相对 报告")

    result = EvidenceReportExporter(
        LabCaptureResolver(repository),
        RecordingRenderer(),  # type: ignore[arg-type]
    ).export(evidence_set, destination)

    assert result.destination == destination
    assert (destination / "report.md").is_file()
    assert (destination / "report.docx").is_file()


def test_markdown_writer_failure_is_controlled_and_cleans_staging(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evidence_set, repository, *_captures = _evidence_fixture(tmp_path)
    original_atomic_write_text = exporter_module.atomic_write_text

    def fail_markdown(destination: Path, text: str, **kwargs: object) -> None:
        if destination.name == "report.md":
            raise PermissionError("simulated markdown failure")
        original_atomic_write_text(destination, text, **kwargs)

    monkeypatch.setattr(exporter_module, "atomic_write_text", fail_markdown)
    destination = tmp_path / "markdown-failure"

    with pytest.raises(ReportExportError) as raised:
        EvidenceReportExporter(
            LabCaptureResolver(repository),
            RecordingRenderer(),  # type: ignore[arg-type]
        ).export(evidence_set, destination)

    assert raised.value.kind == "permission"
    assert "Traceback" not in str(raised.value)
    assert not destination.exists()
    assert not tuple(tmp_path.glob(".csbox-report-*.partial"))


def test_renderer_failure_is_controlled_and_does_not_publish(
    tmp_path: Path,
) -> None:
    evidence_set, repository, *_captures = _evidence_fixture(tmp_path)

    class FailingRenderer:
        def render(self, *_args: object, **_kwargs: object) -> Path:
            raise RuntimeError("simulated renderer failure")

    destination = tmp_path / "renderer-failure"
    with pytest.raises(ReportExportError) as raised:
        EvidenceReportExporter(
            LabCaptureResolver(repository),
            FailingRenderer(),  # type: ignore[arg-type]
        ).export(evidence_set, destination)

    assert raised.value.kind == "generation"
    assert not destination.exists()
    assert not tuple(tmp_path.glob(".csbox-report-*.partial"))


def test_renderer_output_must_be_png(tmp_path: Path) -> None:
    evidence_set, repository, *_captures = _evidence_fixture(tmp_path)

    class JpegRenderer:
        def render(self, _snapshot: object, destination: Path, _theme: object = None) -> Path:
            destination.parent.mkdir(parents=True, exist_ok=True)
            Image.new("RGB", (32, 16), (12, 12, 12)).save(destination, format="JPEG")
            return destination

    destination = tmp_path / "jpeg-renderer"
    with pytest.raises(ReportExportError):
        EvidenceReportExporter(
            LabCaptureResolver(repository),
            JpegRenderer(),  # type: ignore[arg-type]
        ).export(evidence_set, destination)

    assert not destination.exists()
    assert not tuple(tmp_path.glob(".csbox-report-*.partial"))


def test_force_refresh_removes_only_manifest_owned_stale_assets(
    tmp_path: Path,
) -> None:
    evidence_set, repository = _rich_fixture(tmp_path)
    destination = tmp_path / "stale-assets"
    exporter = EvidenceReportExporter(
        LabCaptureResolver(repository),
        RecordingRenderer(),  # type: ignore[arg-type]
    )
    initial = exporter.export(evidence_set, destination)
    stale = initial.images[2]
    manual = destination / "assets" / "manual.txt"
    manual.write_text("keep me", encoding="utf-8")
    reduced = evidence_set.model_copy(update={"items": evidence_set.items[:2]})

    refreshed = exporter.export(reduced, destination, force=True)

    assert len(refreshed.images) == 2
    assert not stale.exists()
    assert manual.read_text(encoding="utf-8") == "keep me"


def test_stale_asset_appearing_after_preflight_is_not_deleted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evidence_set, repository = _rich_fixture(tmp_path)
    destination = tmp_path / "stale-race"
    exporter = EvidenceReportExporter(
        LabCaptureResolver(repository),
        RecordingRenderer(),  # type: ignore[arg-type]
    )
    initial = exporter.export(evidence_set, destination)
    stale = initial.images[2]
    stale_content = stale.read_bytes()
    stale.unlink()
    reduced = evidence_set.model_copy(update={"items": evidence_set.items[:2]})
    original_publish_created_target = exporter_module._publish_created_target
    racer_created = False

    def publish_then_create_stale_racer(
        source: Path,
        target: Path,
        expected_digest: str,
        created_targets: list[object],
    ) -> None:
        nonlocal racer_created
        original_publish_created_target(source, target, expected_digest, created_targets)  # type: ignore[arg-type]
        if target == destination / ".csbox-generated-report.json" and not racer_created:
            stale.write_bytes(stale_content)
            racer_created = True

    monkeypatch.setattr(
        exporter_module,
        "_publish_created_target",
        publish_then_create_stale_racer,
    )
    refreshed = exporter.export(reduced, destination, force=True)

    assert racer_created
    assert refreshed.evidence_item_count == 2
    assert stale.read_bytes() == stale_content


def test_force_refresh_blocks_modified_generated_asset(tmp_path: Path) -> None:
    evidence_set, repository, *_captures = _evidence_fixture(tmp_path)
    destination = tmp_path / "modified-asset"
    exporter = EvidenceReportExporter(
        LabCaptureResolver(repository),
        RecordingRenderer(),  # type: ignore[arg-type]
    )
    initial = exporter.export(evidence_set, destination)
    initial.images[0].write_bytes(b"user-owned")

    with pytest.raises(ReportExportError) as raised:
        exporter.export(evidence_set, destination, force=True)

    assert raised.value.kind == "existing_target"
    assert initial.images[0].read_bytes() == b"user-owned"


def test_invalid_docx_writer_never_publishes(tmp_path: Path) -> None:
    evidence_set, repository = _evidence_fixture(tmp_path)[:2]

    def invalid_writer(
        _evidence_set: EvidenceSet,
        _rendered: tuple[tuple[EvidenceItem, Path], ...],
        destination: Path,
    ) -> None:
        destination.write_bytes(b"not a docx")

    destination = tmp_path / "invalid-docx"
    with pytest.raises(ReportExportError):
        EvidenceReportExporter(
            LabCaptureResolver(repository),
            RecordingRenderer(),  # type: ignore[arg-type]
            docx_writer=invalid_writer,
        ).export(evidence_set, destination)

    assert not destination.exists()
    assert not tuple(tmp_path.glob(".csbox-report-*.partial"))


def test_docx_with_unreferenced_media_never_publishes(tmp_path: Path) -> None:
    evidence_set, repository = _evidence_fixture(tmp_path)[:2]

    def invalid_writer(
        _evidence_set: EvidenceSet,
        rendered: tuple[tuple[EvidenceItem, Path], ...],
        destination: Path,
    ) -> None:
        from docx import Document

        document = Document()
        document.add_picture(str(rendered[0][1]))
        document.save(destination)
        replacement = destination.with_name("replacement.docx")
        with (
            zipfile.ZipFile(destination) as source,
            zipfile.ZipFile(
                replacement,
                "w",
                compression=zipfile.ZIP_DEFLATED,
            ) as target,
        ):
            for name in source.namelist():
                target.writestr(name, source.read(name))
            target.writestr("word/media/orphan.png", rendered[1][1].read_bytes())
        replacement.replace(destination)

    destination = tmp_path / "orphan-media"
    with pytest.raises(ReportExportError):
        EvidenceReportExporter(
            LabCaptureResolver(repository),
            RecordingRenderer(),  # type: ignore[arg-type]
            docx_writer=invalid_writer,
        ).export(evidence_set, destination)

    assert not destination.exists()
    assert not tuple(tmp_path.glob(".csbox-report-*.partial"))
