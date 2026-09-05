from __future__ import annotations

import hashlib
import zipfile
from pathlib import Path

import pytest

import csbox.check.detectors as detectors
from csbox.check.models import CheckFinding, CheckReport, CheckStatus
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


def test_pack_blocks_when_source_directory_cannot_be_enumerated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "project"
    source.mkdir()
    (source / "README.md").write_text("readme\n", encoding="utf-8")
    original_scandir = detectors.os.scandir

    def denied_scandir(path: object):
        if isinstance(path, int) or Path(path) == source:
            raise PermissionError("directory enumeration denied")
        return original_scandir(path)

    monkeypatch.setattr(detectors.os, "scandir", denied_scandir)

    with pytest.raises(PackServiceError, match="安全建立打包计划") as caught:
        PackService().plan(source, destination=tmp_path / "archive.zip")

    assert caught.value.kind == "plan_failed"
    assert not (tmp_path / "archive.zip").exists()


def test_warn_only_plan_and_real_pack_share_the_same_preflight_result(tmp_path: Path) -> None:
    source = tmp_path / "中文项目"
    source.mkdir()
    (source / "main.py").write_text("print('safe')\n", encoding="utf-8")
    (source / "build").mkdir()
    (source / ".cache").mkdir()
    (source / ".csbox").mkdir()
    destination = tmp_path / "交付.zip"
    service = PackService()

    plan = service.plan(source, destination=destination, verify=True)
    report = service.pack(source, destination=destination, verify=True)

    assert plan.can_publish is True
    assert plan.rejected == ()
    assert plan.warnings
    assert report.rejected == ()
    assert set(plan.excluded) == {".cache:directory", ".csbox:directory", "build:directory"}
    assert set(report.excluded) == set(plan.excluded)
    assert report.entries == plan.included


def test_nonblocking_warning_drift_does_not_make_a_pack_plan_blocked(tmp_path: Path) -> None:
    source = tmp_path / "project"
    source.mkdir()
    (source / "main.py").write_text("print('safe')\n", encoding="utf-8")
    (source / "build").mkdir()
    destination = tmp_path / "archive.zip"
    reports = [
        type(
            "ReadmeWarningReport",
            (),
            {
                "status": CheckStatus.WARN,
                "exit_code": 0,
                "findings": (
                    CheckFinding(
                        rule_id="readme",
                        status=CheckStatus.WARN,
                        message="warning only",
                        category="README",
                    ),
                ),
                "projects": (),
            },
        )(),
        type(
            "ArtifactWarningReport",
            (),
            {
                "status": CheckStatus.WARN,
                "exit_code": 0,
                "findings": (
                    CheckFinding(
                        rule_id="artifacts",
                        status=CheckStatus.WARN,
                        message="warning only",
                        path=Path("build"),
                        category="artifact",
                    ),
                ),
                "projects": (),
            },
        )(),
    ]

    class ChangingWarnings:
        def run(self, root: Path, *, build: bool = False) -> object:
            del root, build
            return reports.pop(0)

    service = PackService(check_service=ChangingWarnings())
    plan = service.plan(source, destination=destination)
    report = service.pack_plan(plan)

    assert plan.warnings != ()
    assert report.destination == destination
    assert destination.exists()


def test_pack_plan_reports_removed_last_included_source_path(tmp_path: Path) -> None:
    source = tmp_path / "project"
    source.mkdir()
    included = source / "only.txt"
    included.write_text("one\n", encoding="utf-8")
    service = PackService()
    plan = service.plan(source, destination=tmp_path / "archive.zip")

    included.unlink()

    with pytest.raises(PackServiceError) as caught:
        service.pack_plan(plan)

    assert caught.value.kind == "plan_changed"
    assert caught.value.details == ("only.txt",)
    assert caught.value.path == included.resolve()


def test_existing_target_is_a_shared_preflight_blocker(tmp_path: Path) -> None:
    source = tmp_path / "中文项目"
    source.mkdir()
    (source / "main.py").write_text("print('safe')\n", encoding="utf-8")
    destination = tmp_path / "交付.zip"
    destination.write_bytes(b"keep")
    service = PackService()

    plan = service.plan(source, destination=destination)

    assert plan.can_publish is False
    assert plan.blockers == ("交付.zip:destination-exists",)
    with pytest.raises(PackServiceError) as caught:
        service.pack(source, destination=destination)
    assert caught.value.kind == "destination_exists"
    assert destination.read_bytes() == b"keep"


def test_pack_rejection_error_has_safe_blocker_details(tmp_path: Path) -> None:
    source = tmp_path / "project"
    source.mkdir()
    (source / "README.md").write_text("readme\n", encoding="utf-8")
    (source / ".env").write_text("TOKEN=local-only\n", encoding="utf-8")

    with pytest.raises(PackServiceError) as caught:
        PackService().pack(source, destination=tmp_path / "archive.zip")

    assert caught.value.kind == "content_rejected"
    assert ".env:env" in caught.value.details
    assert "local-only" not in str(caught.value)
