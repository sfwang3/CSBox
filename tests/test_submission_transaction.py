import hashlib
import multiprocessing
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from csbox.check.models import CheckStatus
from csbox.evidence.models import EvidenceItem, EvidenceSet, LabCaptureSource
from csbox.evidence.resolver import ResolvedReportableEvidence
from csbox.pack.service import PackService
from csbox.report.models import ReportProfile
from csbox.submission import service as service_module
from csbox.submission.models import SubmissionError, SubmissionVerifyResult
from csbox.submission.service import SubmissionService
from csbox.submission.verifier import SubmissionVerifier

_NOW = datetime(2026, 9, 11, 12, 0, tzinfo=UTC)


class _EvidenceRepository:
    def load(self, evidence_set_id: str) -> EvidenceSet:
        return EvidenceSet(
            id=evidence_set_id,
            title="提交证据",
            items=(
                EvidenceItem(
                    source=LabCaptureSource(session_id="session-1", capture_id="capture-1"),
                    title="终端证据",
                ),
            ),
            created_at=_NOW,
            updated_at=_NOW,
        )


class _MutableEvidenceRepository(_EvidenceRepository):
    def __init__(self) -> None:
        self.value = super().load("set-1")

    def load(self, evidence_set_id: str) -> EvidenceSet:
        del evidence_set_id
        return self.value


class _Resolver:
    def resolve(self, source: object) -> ResolvedReportableEvidence:
        return ResolvedReportableEvidence(
            source=source,
            session_name="实验会话",
            capture=SimpleNamespace(
                model_dump=lambda mode="json": {"title": "capture snapshot", "body": "safe"}
            ),
        )


class _ReportService:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.exporter = SimpleNamespace(resolver=_Resolver())
        self.error = error

    def export(self, evidence_set: EvidenceSet, destination: Path, **kwargs: object) -> object:
        if self.error is not None:
            raise self.error
        destination.mkdir(parents=True)
        report = destination / "report.docx"
        report.write_bytes(b"deterministic report")
        return SimpleNamespace(docx=report, warnings=())


class _ProfileRepository:
    def __init__(self) -> None:
        self.value = ReportProfile.default()

    def load_or_default(
        self, evidence_set_id: str, *, default: ReportProfile | None = None
    ) -> ReportProfile:
        del evidence_set_id, default
        return self.value


class _CheckService:
    def run(self, root: Path, *, build: bool = False, deep: bool = False) -> object:
        return SimpleNamespace(
            root=root,
            status=CheckStatus.PASS,
            findings=(),
            builds=(),
            deep_scan=None,
            exit_code=0,
            projects=(),
        )


def _real_transaction_service(project: Path) -> SubmissionService:
    return SubmissionService(
        project,
        evidence_repository=_EvidenceRepository(),
        report_service=_ReportService(),
        profile_repository=_ProfileRepository(),
        check_service=_CheckService(),
        pack_service=PackService(),
        clock=lambda: _NOW,
    )


def _real_service_with_report_error(project: Path, error: Exception) -> SubmissionService:
    return SubmissionService(
        project,
        evidence_repository=_EvidenceRepository(),
        report_service=_ReportService(error=error),
        profile_repository=_ProfileRepository(),
        check_service=_CheckService(),
        pack_service=PackService(),
        clock=lambda: _NOW,
    )


def _project_file_digests(project: Path) -> dict[str, str]:
    return {
        path.relative_to(project).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in project.rglob("*")
        if path.is_file()
    }


def _assert_valid_handoff(destination: Path, archive_filename: str) -> None:
    assert {path.name for path in destination.iterdir()} == {
        "report.docx",
        archive_filename,
        "submission-manifest.json",
    }
    assert not (destination / "report-bundle").exists()
    with zipfile.ZipFile(destination / archive_filename) as archive:
        assert archive.testzip() is None
        assert "manifest.json" in archive.namelist()
    verification = SubmissionVerifier().verify(destination)
    assert verification == SubmissionVerifyResult(
        status="PASS",
        verified=True,
        files=("report.docx", archive_filename, "submission-manifest.json"),
    )


def test_prepare_absent_destination_publishes_verified_three_file_handoff(tmp_path: Path) -> None:
    project = tmp_path / "课程项目"
    project.mkdir()
    (project / "README.md").write_text("# safe project\n", encoding="utf-8")
    (project / "main.py").write_text("print('safe')\n", encoding="utf-8")
    before = _project_file_digests(project)
    service = _real_transaction_service(project)
    destination = tmp_path / "handoff"

    result = service.prepare(service.plan("set-1", destination=destination))

    assert result.verified is True
    _assert_valid_handoff(destination, result.archive_path.name)
    assert _project_file_digests(project) == before


def test_prepare_owned_refresh_allows_only_natural_pack_output_existence_change(
    tmp_path: Path,
) -> None:
    project = tmp_path / "课程项目"
    project.mkdir()
    (project / "README.md").write_text("# safe project\n", encoding="utf-8")
    (project / "main.py").write_text("print('safe')\n", encoding="utf-8")
    service = _real_transaction_service(project)
    destination = tmp_path / "handoff"

    service.prepare(service.plan("set-1", destination=destination))
    refresh_plan = service.plan("set-1", destination=destination)
    assert refresh_plan.destination_state == "owned"

    result = service.prepare(refresh_plan, force=True)

    assert result.verified is True
    _assert_valid_handoff(destination, result.archive_path.name)
    assert not list(tmp_path.glob(".handoff-*.backup"))


def test_prepare_owned_destination_requires_explicit_force(tmp_path: Path) -> None:
    project = tmp_path / "课程项目"
    project.mkdir()
    (project / "README.md").write_text("# safe project\n", encoding="utf-8")
    (project / "main.py").write_text("print('safe')\n", encoding="utf-8")
    service = _real_transaction_service(project)
    destination = tmp_path / "handoff"

    first = service.prepare(service.plan("set-1", destination=destination))
    before = {path.name: path.read_bytes() for path in destination.iterdir()}

    with pytest.raises(SubmissionError, match="--force") as caught:
        service.prepare(service.plan("set-1", destination=destination))

    assert caught.value.kind == "destination_exists"
    assert {path.name: path.read_bytes() for path in destination.iterdir()} == before
    _assert_owned_destination(destination, first.archive_path.name)


def test_prepare_owned_refresh_rejects_project_change_after_backup(tmp_path: Path) -> None:
    project = tmp_path / "课程项目"
    project.mkdir()
    (project / "README.md").write_text("# safe project\n", encoding="utf-8")
    main = project / "main.py"
    main.write_text("print('safe')\n", encoding="utf-8")
    service = _real_transaction_service(project)
    destination = tmp_path / "handoff"
    service.prepare(service.plan("set-1", destination=destination))
    plan = service.plan("set-1", destination=destination)

    def mutate_after_backup(phase: str) -> None:
        if phase == "after_backup":
            main.write_text("print('changed')\n", encoding="utf-8")

    service.phase_hook = mutate_after_backup

    with pytest.raises(SubmissionError, match="内容发生了变化") as caught:
        service.prepare(plan, force=True)

    assert caught.value.kind == "stale"
    _assert_valid_handoff(destination, plan.archive_filename)
    assert not list(tmp_path.glob(".handoff-*.backup"))


def test_prepare_is_exposed_as_submission_transaction(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    service = SubmissionService(project)

    with pytest.raises(SubmissionError):
        service.prepare(None)  # type: ignore[arg-type]


def test_destination_lease_keeps_stable_regular_lock_artifact(tmp_path: Path) -> None:
    destination = tmp_path / "handoff"
    key = service_module._path_identity(destination)
    lock_path = tmp_path / (
        f".csbox-submission-lock-{service_module.hashlib.sha256(key.encode()).hexdigest()[:24]}.lock"
    )

    with service_module._destination_lease(destination, key):
        assert lock_path.is_file()
        first_inode = lock_path.stat().st_ino
    assert lock_path.is_file()
    assert lock_path.stat().st_ino == first_inode


def test_destination_lease_rejects_a_symlink_lock_path(tmp_path: Path) -> None:
    destination = tmp_path / "handoff"
    key = service_module._path_identity(destination)
    lock_path = tmp_path / (
        f".csbox-submission-lock-{service_module.hashlib.sha256(key.encode()).hexdigest()[:24]}.lock"
    )
    target = tmp_path / "user-file"
    target.write_bytes(b"do not touch")
    lock_path.symlink_to(target)

    with (
        pytest.raises(SubmissionError, match="锁定"),
        service_module._destination_lease(destination, key),
    ):
        pass
    assert target.read_bytes() == b"do not touch"
    assert lock_path.is_symlink()


def _assert_destination_absent(destination: Path) -> None:
    assert not destination.exists()
    assert not destination.is_symlink()


def _assert_owned_destination(destination: Path, archive_filename: str) -> None:
    _assert_valid_handoff(destination, archive_filename)
    assert not list(destination.parent.glob(f".{destination.name}-*.backup"))


@pytest.mark.parametrize(
    "phase",
    (
        "after_preflight",
        "after_report",
        "after_pack",
        "after_manifest",
        "after_staged_verify",
    ),
)
def test_phase_failure_on_absent_destination_publishes_no_partial_handoff(
    tmp_path: Path, phase: str
) -> None:
    project = tmp_path / "课程项目"
    project.mkdir()
    (project / "README.md").write_text("# safe\n", encoding="utf-8")
    (project / "main.py").write_text("print('safe')\n", encoding="utf-8")
    destination = tmp_path / "handoff"
    service = _real_transaction_service(project)
    service.phase_hook = lambda current: (
        (_ for _ in ()).throw(RuntimeError(f"inject {current}")) if current == phase else None
    )

    with pytest.raises(SubmissionError):
        service.prepare(service.plan("set-1", destination=destination))

    _assert_destination_absent(destination)
    assert not list(tmp_path.glob(".handoff-*.backup"))
    assert not list(tmp_path.glob(".handoff-*.staging"))


@pytest.mark.parametrize(
    "phase",
    (
        "after_preflight",
        "after_report",
        "after_pack",
        "after_manifest",
        "after_staged_verify",
        "after_backup",
    ),
)
def test_phase_failure_on_owned_destination_preserves_previous_valid_handoff(
    tmp_path: Path, phase: str
) -> None:
    project = tmp_path / "课程项目"
    project.mkdir()
    (project / "README.md").write_text("# safe\n", encoding="utf-8")
    (project / "main.py").write_text("print('safe')\n", encoding="utf-8")
    destination = tmp_path / "handoff"
    service = _real_transaction_service(project)
    first = service.prepare(service.plan("set-1", destination=destination))
    before = {path.name: path.read_bytes() for path in destination.iterdir()}
    service.phase_hook = lambda current: (
        (_ for _ in ()).throw(RuntimeError(f"inject {current}")) if current == phase else None
    )

    with pytest.raises(SubmissionError):
        service.prepare(service.plan("set-1", destination=destination), force=True)

    assert {path.name: path.read_bytes() for path in destination.iterdir()} == before
    _assert_owned_destination(destination, first.archive_path.name)


@pytest.mark.parametrize("phase", ("after_publish", "after_backup_cleanup"))
def test_post_publication_hook_failure_leaves_complete_new_handoff(
    tmp_path: Path, phase: str
) -> None:
    project = tmp_path / "课程项目"
    project.mkdir()
    (project / "README.md").write_text("# safe\n", encoding="utf-8")
    (project / "main.py").write_text("print('safe')\n", encoding="utf-8")
    destination = tmp_path / "handoff"
    service = _real_transaction_service(project)
    service.prepare(service.plan("set-1", destination=destination))
    service.phase_hook = lambda current: (
        (_ for _ in ()).throw(RuntimeError(f"inject {current}")) if current == phase else None
    )

    with pytest.raises(SubmissionError):
        service.prepare(service.plan("set-1", destination=destination), force=True)

    archive_filename = next(path.name for path in destination.iterdir() if path.suffix == ".zip")
    _assert_valid_handoff(destination, archive_filename)
    if phase == "after_publish":
        assert len(list(tmp_path.glob(".handoff-*.backup"))) == 1
    else:
        assert not list(tmp_path.glob(".handoff-*.backup"))


def test_report_generation_failure_preserves_owned_destination(tmp_path: Path) -> None:
    project = tmp_path / "课程项目"
    project.mkdir()
    (project / "README.md").write_text("# safe\n", encoding="utf-8")
    (project / "main.py").write_text("print('safe')\n", encoding="utf-8")
    destination = tmp_path / "handoff"
    good = _real_transaction_service(project)
    first = good.prepare(good.plan("set-1", destination=destination))
    service = _real_service_with_report_error(project, RuntimeError("report failed"))

    with pytest.raises(SubmissionError):
        service.prepare(service.plan("set-1", destination=destination), force=True)

    _assert_valid_handoff(destination, first.archive_path.name)


def test_pack_failure_preserves_owned_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "课程项目"
    project.mkdir()
    (project / "README.md").write_text("# safe\n", encoding="utf-8")
    (project / "main.py").write_text("print('safe')\n", encoding="utf-8")
    destination = tmp_path / "handoff"
    service = _real_transaction_service(project)
    first = service.prepare(service.plan("set-1", destination=destination))
    original = service.pack_service.pack_plan

    def fail_pack(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise RuntimeError("pack failed")

    monkeypatch.setattr(service.pack_service, "pack_plan", fail_pack)
    with pytest.raises(SubmissionError):
        service.prepare(service.plan("set-1", destination=destination), force=True)
    monkeypatch.setattr(service.pack_service, "pack_plan", original)
    _assert_valid_handoff(destination, first.archive_path.name)


def test_manifest_write_failure_preserves_owned_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "课程项目"
    project.mkdir()
    (project / "README.md").write_text("# safe\n", encoding="utf-8")
    (project / "main.py").write_text("print('safe')\n", encoding="utf-8")
    destination = tmp_path / "handoff"
    service = _real_transaction_service(project)
    first = service.prepare(service.plan("set-1", destination=destination))
    original = service_module.atomic_write_text

    def fail_manifest(path: Path, text: str, *, replace_existing: bool = True) -> None:
        if path.name == "submission-manifest.json":
            raise OSError("manifest write failed")
        original(path, text, replace_existing=replace_existing)

    monkeypatch.setattr(service_module, "atomic_write_text", fail_manifest)
    with pytest.raises(SubmissionError):
        service.prepare(service.plan("set-1", destination=destination), force=True)
    _assert_valid_handoff(destination, first.archive_path.name)


def test_staged_verification_failure_preserves_owned_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "课程项目"
    project.mkdir()
    (project / "README.md").write_text("# safe\n", encoding="utf-8")
    (project / "main.py").write_text("print('safe')\n", encoding="utf-8")
    destination = tmp_path / "handoff"
    service = _real_transaction_service(project)
    first = service.prepare(service.plan("set-1", destination=destination))
    original = SubmissionVerifier.verify

    def fail_verification(self: SubmissionVerifier, path: Path) -> SubmissionVerifyResult:
        result = original(self, path)
        if path.name.endswith(".partial"):
            assert result.verified
            return SubmissionVerifyResult(status="FAIL", verified=False, files=result.files)
        return result

    plan = service.plan("set-1", destination=destination)
    monkeypatch.setattr(SubmissionVerifier, "verify", fail_verification)
    with pytest.raises(SubmissionError, match="校验"):
        service.prepare(plan, force=True)
    _assert_valid_handoff(destination, first.archive_path.name)


def test_backup_failure_runs_real_no_replace_rename_and_preserves_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "课程项目"
    project.mkdir()
    (project / "README.md").write_text("# safe\n", encoding="utf-8")
    (project / "main.py").write_text("print('safe')\n", encoding="utf-8")
    destination = tmp_path / "handoff"
    service = _real_transaction_service(project)
    first = service.prepare(service.plan("set-1", destination=destination))
    occupied = tmp_path / "occupied-backup"
    occupied.mkdir()
    monkeypatch.setattr(service_module, "_new_submission_backup", lambda path: occupied)

    with pytest.raises(SubmissionError):
        service.prepare(service.plan("set-1", destination=destination), force=True)

    _assert_valid_handoff(destination, first.archive_path.name)
    assert (occupied / "sentinel").exists() is False


def test_final_publication_failure_preserves_competing_destination_and_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "课程项目"
    project.mkdir()
    (project / "README.md").write_text("# safe\n", encoding="utf-8")
    (project / "main.py").write_text("print('safe')\n", encoding="utf-8")
    destination = tmp_path / "handoff"
    service = _real_transaction_service(project)
    first = service.prepare(service.plan("set-1", destination=destination))
    original = service_module.safe_rename
    raced = False

    def fail_final(source: Path, target: Path, *, replace_existing: bool = True) -> None:
        nonlocal raced
        if target == destination and source.name.startswith(".handoff-") and not raced:
            destination.mkdir()
            (destination / "raced.txt").write_text("keep me", encoding="utf-8")
            raced = True
        original(source, target, replace_existing=replace_existing)

    monkeypatch.setattr(service_module, "safe_rename", fail_final)
    with pytest.raises(SubmissionError):
        service.prepare(service.plan("set-1", destination=destination), force=True)

    assert (destination / "raced.txt").read_text(encoding="utf-8") == "keep me"
    assert len(list(tmp_path.glob(".handoff-*.backup"))) == 1
    assert first.archive_path.name.endswith(".zip")


def test_backup_cleanup_failure_returns_new_handoff_and_retains_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "课程项目"
    project.mkdir()
    (project / "README.md").write_text("# safe\n", encoding="utf-8")
    (project / "main.py").write_text("print('safe')\n", encoding="utf-8")
    destination = tmp_path / "handoff"
    service = _real_transaction_service(project)
    service.prepare(service.plan("set-1", destination=destination))
    original = service_module._remove_owned_tree

    def fail_backup_cleanup(path: Path) -> None:
        if path.name.startswith(".handoff-") and path.name.endswith(".backup"):
            raise OSError("cleanup failed")
        original(path)

    monkeypatch.setattr(service_module, "_remove_owned_tree", fail_backup_cleanup)
    result = service.prepare(service.plan("set-1", destination=destination), force=True)

    archive_filename = next(path.name for path in destination.iterdir() if path.suffix == ".zip")
    _assert_valid_handoff(destination, archive_filename)
    assert any("备份" in warning for warning in result.warnings)
    assert len(list(tmp_path.glob(".handoff-*.backup"))) == 1


def _hold_destination_lease(
    destination: str, ready: object, release: object, result: object
) -> None:
    path = Path(destination)
    key = service_module._path_identity(path)
    try:
        with service_module._destination_lease(path, key):
            ready.set()
            release.wait(10)
    except Exception as error:  # pragma: no cover - only child transport
        result.put(("error", type(error).__name__, str(error)))
    else:
        result.put(("released",))


def test_destination_lease_blocks_second_process_without_sleep_or_retry(tmp_path: Path) -> None:
    context = multiprocessing.get_context("spawn")
    destination = tmp_path / "handoff"
    ready = context.Event()
    release = context.Event()
    result = context.Queue()
    child = context.Process(
        target=_hold_destination_lease,
        args=(str(destination), ready, release, result),
    )
    child.start()
    try:
        assert ready.wait(10)
        key = service_module._path_identity(destination)
        lock_path = tmp_path / (
            f".csbox-submission-lock-{service_module.hashlib.sha256(key.encode()).hexdigest()[:24]}.lock"
        )
        assert lock_path.is_file()
        inode = lock_path.stat().st_ino
        with (
            pytest.raises(SubmissionError) as caught,
            service_module._destination_lease(destination, key),
        ):
            pass
        assert caught.value.kind == "busy"
        assert lock_path.stat().st_ino == inode
    finally:
        release.set()
        child.join(10)
        if child.is_alive():
            child.terminate()
            child.join()
    assert child.exitcode == 0
    assert result.get(timeout=2) == ("released",)


@pytest.mark.parametrize(
    "mutation", ("project", "evidence", "profile", "pack_config", "destination")
)
def test_same_size_mutation_at_preflight_boundary_blocks_before_publication(
    tmp_path: Path, mutation: str
) -> None:
    project = tmp_path / "课程项目"
    project.mkdir()
    main = project / "main.py"
    main.write_text("print('safe')\n", encoding="utf-8")
    (project / "README.md").write_text("# safe\n", encoding="utf-8")
    destination = tmp_path / "handoff"
    evidence_repository = _MutableEvidenceRepository()
    profile_repository = _ProfileRepository()
    service = SubmissionService(
        project,
        evidence_repository=evidence_repository,
        report_service=_ReportService(),
        profile_repository=profile_repository,
        check_service=_CheckService(),
        pack_service=PackService(),
        clock=lambda: _NOW,
    )
    plan = service.plan("set-1", destination=destination)

    def mutate(phase: str) -> None:
        if phase != "after_preflight":
            return
        if mutation == "project":
            main.write_text("print('done')\n", encoding="utf-8")
        elif mutation == "evidence":
            evidence_repository.value = evidence_repository.value.model_copy(
                update={"title": "变更证据"}
            )
        elif mutation == "profile":
            profile_repository.value = profile_repository.value.model_copy(
                update={"report_title": "变更报告"}
            )
        elif mutation == "pack_config":
            service.pack_service.filename_template = "{name}-changed.zip"  # type: ignore[attr-defined]
        else:
            destination.mkdir()
            (destination / "user.txt").write_text("user content", encoding="utf-8")

    service.phase_hook = mutate
    with pytest.raises(SubmissionError) as caught:
        service.prepare(plan)

    assert caught.value.kind in {"stale", "destination_unowned", "blocked"}
    if mutation == "destination":
        assert (destination / "user.txt").read_text(encoding="utf-8") == "user content"
    else:
        _assert_destination_absent(destination)


def test_corrupt_owned_manifest_blocks_and_never_deletes_content(tmp_path: Path) -> None:
    project = tmp_path / "课程项目"
    project.mkdir()
    (project / "README.md").write_text("# safe\n", encoding="utf-8")
    (project / "main.py").write_text("print('safe')\n", encoding="utf-8")
    destination = tmp_path / "handoff"
    service = _real_transaction_service(project)
    service.prepare(service.plan("set-1", destination=destination))
    manifest = destination / "submission-manifest.json"
    manifest.write_text('{"schema_version": 99}\n', encoding="utf-8")
    before = {path.name: path.read_bytes() for path in destination.iterdir()}
    plan = service.plan("set-1", destination=destination)

    assert plan.readiness.value == "BLOCKED"
    with pytest.raises(SubmissionError):
        service.prepare(plan)
    assert {path.name: path.read_bytes() for path in destination.iterdir()} == before
