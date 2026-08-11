from __future__ import annotations

import hashlib
import zipfile
from pathlib import Path

import pytest

from csbox.check.models import CheckReport
from csbox.pack.models import PackReport
from csbox.pack.service import PackService, PackServiceError


class FakeCheckService:
    def __init__(self, report: CheckReport | None = None) -> None:
        self.calls: list[tuple[Path, bool]] = []
        self.report = report or CheckReport(root=Path("."))

    def run(self, root: Path, *, build: bool = False) -> CheckReport:
        self.calls.append((root, build))
        return self.report


def test_pack_runs_check_stages_safely_and_verifies_zip(tmp_path: Path) -> None:
    source = tmp_path / "项目"
    source.mkdir()
    content = "中文源文件\n"
    source_file = source / "src.txt"
    source_file.write_text(content, encoding="utf-8")
    before = hashlib.sha256(source_file.read_bytes()).hexdigest()
    checker = FakeCheckService()
    destination = tmp_path / "交付.zip"

    report = PackService(check_service=checker).pack(
        source,
        destination=destination,
        verify=True,
    )

    assert isinstance(report, PackReport)
    assert checker.calls == [(source.resolve(), False)]
    assert source_file.is_file()
    assert hashlib.sha256(source_file.read_bytes()).hexdigest() == before
    with zipfile.ZipFile(destination) as archive:
        assert archive.testzip() is None
        assert archive.namelist() == ["src.txt"]
    assert report.verified is True


def test_pack_refuses_overwrite_without_force_and_sanitizes_filename(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "README.md").write_text("readme")
    destination = tmp_path / "已有.zip"
    destination.write_bytes(b"keep")

    service = PackService(student_id="A/B", student_name="学生:一", course_name="网络")
    with pytest.raises(PackServiceError):
        service.pack(source, destination=destination)
    assert destination.read_bytes() == b"keep"

    report = service.pack(source, destination=destination, force=True)

    assert report.destination == destination
    assert destination.read_bytes() != b"keep"


def test_pack_check_failure_does_not_touch_existing_destination(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "README.md").write_text("readme")
    destination = tmp_path / "archive.zip"
    destination.write_bytes(b"unchanged")
    check = CheckReport(
        root=source,
        findings=(),
        builds=(),
    )
    check = check.model_copy(update={"findings": (type("Finding", (), {})(),)})
    checker = FakeCheckService()
    checker.report = type(
        "FailingReport",
        (),
        {"exit_code": 1},
    )()

    with pytest.raises(PackServiceError):
        PackService(check_service=checker).pack(source, destination=destination)
    assert destination.read_bytes() == b"unchanged"


def test_pack_service_excludes_runtime_state_from_real_archive(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "README.md").write_text("readme")
    (source / ".csbox" / "sessions" / "session-1").mkdir(parents=True)
    (source / ".csbox" / "sessions" / "session-1" / "session.cast").write_text("cast")
    destination = tmp_path / "archive.zip"

    report = PackService().pack(source, destination=destination, verify=True)

    assert report.verified is True
    with zipfile.ZipFile(destination) as archive:
        assert archive.namelist() == ["README.md"]
