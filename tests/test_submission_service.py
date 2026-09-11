from __future__ import annotations

import hashlib
import os
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

import csbox.submission.service as service_module
from csbox.check.models import CheckFinding, CheckStatus
from csbox.evidence.models import (
    ApiStepSource,
    EvidenceItem,
    EvidenceSet,
    LabCaptureSource,
)
from csbox.evidence.resolver import ResolvedReportableEvidence
from csbox.pack.models import PackPlan
from csbox.report.models import ReportProfile
from csbox.report.repository import ReportProfilePersistenceError
from csbox.submission import (
    SubmissionReadiness,
    SubmissionService,
    SubmissionVerifyResult,
    create_submission_service,
)

_NOW = datetime(2026, 9, 11, 12, 0, tzinfo=UTC)


def _evidence_set(
    *,
    items: tuple[EvidenceItem, ...] | None = None,
    title: str = "提交证据",
) -> EvidenceSet:
    selected = items or (
        EvidenceItem(
            source=LabCaptureSource(session_id="session-1", capture_id="capture-1"),
            title="终端证据",
        ),
    )
    return EvidenceSet(
        id="set-1",
        title=title,
        items=selected,
        created_at=_NOW,
        updated_at=_NOW,
    )


def _project(tmp_path: Path, *, name: str = "课程项目") -> Path:
    project = tmp_path / name
    project.mkdir()
    project.joinpath("README.md").write_text("# safe project\n", encoding="utf-8")
    project.joinpath("main.py").write_text("print('safe')\n", encoding="utf-8")
    return project


class FakeEvidenceRepository:
    def __init__(self, evidence_set: EvidenceSet) -> None:
        self.evidence_set = evidence_set
        self.calls: list[str] = []

    def load(self, evidence_set_id: str) -> EvidenceSet:
        self.calls.append(evidence_set_id)
        return self.evidence_set


class FakeResolver:
    def __init__(self, *, unavailable: set[object] | None = None) -> None:
        self.unavailable = unavailable or set()
        self.calls: list[object] = []

    def resolve(self, source: object) -> ResolvedReportableEvidence:
        self.calls.append(source)
        if source in self.unavailable:
            return ResolvedReportableEvidence(
                source=source,
                unavailable_reason="capture_unavailable",
            )
        if isinstance(source, LabCaptureSource):
            return ResolvedReportableEvidence(
                source=source,
                session_name="实验会话",
                capture=SimpleNamespace(
                    model_dump=lambda mode="json": {
                        "title": "capture snapshot",
                        "body": "LAB_CAPTURE_CONTENT",
                        "mode": mode,
                    }
                ),
            )
        if isinstance(source, ApiStepSource):
            return ResolvedReportableEvidence(
                source=source,
                scenario_name="API 场景",
                step_name="请求步骤",
                run_status="PASS",
                api_evidence=SimpleNamespace(
                    model_dump=lambda mode="json": {
                        "title": "api evidence",
                        "body": "API_SECRET_SENTINEL",
                        "mode": mode,
                    }
                ),
            )
        return ResolvedReportableEvidence(
            source=source,
            unavailable_reason="unsupported_source_type",
        )


class FakeReportService:
    def __init__(self, resolver: FakeResolver) -> None:
        self.exporter = SimpleNamespace(resolver=resolver)


class FakeProfileRepository:
    def __init__(
        self,
        profile: ReportProfile | None = None,
        *,
        error: Exception | None = None,
    ) -> None:
        self.profile = profile
        self.error = error
        self.calls: list[tuple[str, ReportProfile | None]] = []

    def load_or_default(
        self,
        evidence_set_id: str,
        *,
        default: ReportProfile | None = None,
    ) -> ReportProfile:
        self.calls.append((evidence_set_id, default))
        if self.error is not None:
            raise self.error
        return self.profile or default or ReportProfile.default()


class FakeCheckService:
    def __init__(
        self,
        status: CheckStatus = CheckStatus.PASS,
        *,
        findings: tuple[CheckFinding, ...] = (),
        deep_scan: CheckFinding | None = None,
    ) -> None:
        self.status = status
        self.findings = findings
        self.deep_scan = deep_scan
        self.calls: list[tuple[Path, bool, bool]] = []

    def run(self, root: Path, *, build: bool = False, deep: bool = False) -> object:
        self.calls.append((Path(root), build, deep))
        return SimpleNamespace(
            root=Path(root),
            status=self.status,
            findings=self.findings,
            builds=(),
            deep_scan=self.deep_scan,
            exit_code=1 if self.status is CheckStatus.FAIL else 0,
            projects=(),
        )


class FakePackService:
    def __init__(
        self,
        project_dir: Path,
        *,
        filename: str = "configured-project.zip",
        rejected: tuple[str, ...] = (),
        warnings: tuple[str, ...] = (),
    ) -> None:
        self.project_dir = project_dir
        self.filename = filename
        self.rejected = rejected
        self.warnings = warnings
        self.calls: list[tuple[Path, dict[str, object]]] = []

    def plan(self, root: Path, **kwargs: object) -> PackPlan:
        self.calls.append((Path(root), kwargs))
        destination = Path(kwargs["destination"])
        plan = PackPlan(
            source_root=Path(root).resolve(),
            destination=destination,
            output_filename=destination.name,
            project_type="python",
            included=("README.md", "main.py", "manifest.json"),
            excluded=(),
            rejected=self.rejected,
            warnings=self.warnings,
            source_bytes=34,
            include_manifest=bool(kwargs["include_manifest"]),
            verification_requested=bool(kwargs["verify"]),
            force=bool(kwargs["force"]),
            output_exists=destination.exists(),
        )
        plan._source_fingerprint = "a" * 64
        plan._source_file_fingerprints = (
            ("README.md", "b" * 64),
            ("main.py", "c" * 64),
        )
        return plan


def _service(
    project: Path,
    *,
    evidence_set: EvidenceSet | None = None,
    resolver: FakeResolver | None = None,
    profile_repository: FakeProfileRepository | None = None,
    check_service: FakeCheckService | None = None,
    pack_service: FakePackService | None = None,
) -> tuple[
    SubmissionService,
    FakeEvidenceRepository,
    FakeResolver,
    FakeCheckService,
    FakePackService,
]:
    repository = FakeEvidenceRepository(evidence_set or _evidence_set())
    selected_resolver = resolver or FakeResolver()
    selected_check = check_service or FakeCheckService()
    selected_pack = pack_service or FakePackService(project)
    service = SubmissionService(
        project,
        evidence_repository=repository,
        report_service=FakeReportService(selected_resolver),
        profile_repository=profile_repository or FakeProfileRepository(),
        check_service=selected_check,
        pack_service=selected_pack,
        clock=lambda: _NOW,
    )
    return service, repository, selected_resolver, selected_check, selected_pack


def test_create_submission_service_uses_cwd_and_plan_resolves_mixed_sources(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)
    evidence = _evidence_set(
        items=(
            EvidenceItem(
                source=LabCaptureSource(session_id="session-1", capture_id="capture-1"),
                title="终端证据",
            ),
            EvidenceItem(
                source=ApiStepSource(run_id="run-1", step_index=1),
                title="API 证据",
            ),
        )
    )
    service, repository, resolver, checker, packer = _service(project, evidence_set=evidence)

    plan = service.plan("set-1", deep=True)

    assert isinstance(create_submission_service(project), SubmissionService)
    assert repository.calls == ["set-1"]
    assert len(resolver.calls) == 2
    assert plan.readiness is SubmissionReadiness.READY
    assert plan.destination == tmp_path / "课程项目-submission"
    assert plan.report_filename == "report.docx"
    assert plan.archive_filename == "configured-project.zip"
    assert plan.project_archive.verified is True
    assert plan.project_archive.contains_manifest is True
    assert plan.pack_plan is not None
    assert checker.calls == [(project.resolve(), False, True)]
    assert len(packer.calls) == 1
    packer_kwargs = packer.calls[0][1]
    assert packer_kwargs == {
        "destination": plan.destination / "configured-project.zip",
        "verify": True,
        "force": True,
        "include_manifest": True,
    }
    assert plan.resolved_sources and len(plan.resolved_sources) == 2
    assert "API_SECRET_SENTINEL" not in plan.model_dump_json()
    assert "LAB_CAPTURE_CONTENT" not in plan.model_dump_json()


@pytest.mark.parametrize(
    "source_kind",
    ("lab", "api"),
)
def test_plan_supports_lab_only_and_api_only_sets(tmp_path: Path, source_kind: str) -> None:
    project = _project(tmp_path)
    source = (
        LabCaptureSource(session_id="session-1", capture_id="capture-1")
        if source_kind == "lab"
        else ApiStepSource(run_id="run-1", step_index=1)
    )
    service, _repository, resolver, _checker, _packer = _service(
        project,
        evidence_set=_evidence_set(items=(EvidenceItem(source=source, title="证据"),)),
    )

    plan = service.plan("set-1")

    assert plan.readiness is SubmissionReadiness.READY
    assert resolver.calls == [source]
    assert plan.resolved_sources[0].available is True


def test_unavailable_source_blocks_without_copying_source_payload(tmp_path: Path) -> None:
    project = _project(tmp_path)
    source = LabCaptureSource(session_id="missing", capture_id="capture-1")
    resolver = FakeResolver(unavailable={source})
    service, _repository, _resolver, _checker, _packer = _service(
        project,
        evidence_set=_evidence_set(items=(EvidenceItem(source=source, title="不可用证据"),)),
        resolver=resolver,
    )

    plan = service.plan("set-1")

    assert plan.readiness is SubmissionReadiness.BLOCKED
    assert plan.blockers
    assert any("来源" in blocker for blocker in plan.blockers)
    assert "capture-1" not in plan.model_dump_json()


def test_missing_profile_uses_deterministic_default(tmp_path: Path) -> None:
    project = _project(tmp_path)
    profile_repository = FakeProfileRepository()
    service, _repository, _resolver, _checker, _packer = _service(
        project,
        profile_repository=profile_repository,
    )

    plan = service.plan("set-1")

    assert plan.readiness is SubmissionReadiness.READY
    assert profile_repository.calls[0][1] == ReportProfile.default()
    assert len(plan.report_profile_fingerprint) == 64


def test_invalid_profile_blocks_and_does_not_expose_profile_error(tmp_path: Path) -> None:
    project = _project(tmp_path)
    profile_repository = FakeProfileRepository(
        error=ReportProfilePersistenceError("PRIVATE_PROFILE_PAYLOAD")
    )
    service, _repository, _resolver, _checker, _packer = _service(
        project,
        profile_repository=profile_repository,
    )

    plan = service.plan("set-1")

    assert plan.readiness is SubmissionReadiness.BLOCKED
    assert any("报告结构" in blocker for blocker in plan.blockers)
    assert "PRIVATE_PROFILE_PAYLOAD" not in plan.model_dump_json()


@pytest.mark.parametrize("status", tuple(CheckStatus))
def test_check_status_is_preserved_and_build_is_always_false(
    tmp_path: Path,
    status: CheckStatus,
) -> None:
    project = _project(tmp_path)
    checker = FakeCheckService(status=status)
    service, _repository, _resolver, _checker, _packer = _service(
        project,
        check_service=checker,
    )

    plan = service.plan("set-1", deep=True)

    assert plan.check.status is status
    assert plan.check.build_requested is False
    assert plan.check.deep_requested is True
    assert checker.calls == [(project.resolve(), False, True)]
    if status is CheckStatus.FAIL:
        assert plan.readiness is SubmissionReadiness.BLOCKED
    else:
        assert plan.readiness in {SubmissionReadiness.READY, SubmissionReadiness.WARNING}


def test_check_warning_details_are_sanitized_and_warning_readiness_is_explicit(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)
    checker = FakeCheckService(
        status=CheckStatus.WARN,
        findings=(
            CheckFinding(
                rule_id="readme",
                status=CheckStatus.WARN,
                message="RAW_WARNING_SECRET_SENTINEL",
                path=project / "secret-name.txt",
                category="README",
            ),
        ),
    )
    service, _repository, _resolver, _checker, _packer = _service(
        project,
        check_service=checker,
    )

    plan = service.plan("set-1")

    assert plan.readiness is SubmissionReadiness.WARNING
    assert plan.check.warning_count == 1
    assert plan.warnings
    assert "RAW_WARNING_SECRET_SENTINEL" not in plan.model_dump_json()


def test_deep_skip_is_truthful_and_adds_a_warning_limitation(tmp_path: Path) -> None:
    project = _project(tmp_path)
    checker = FakeCheckService(
        status=CheckStatus.PASS,
        deep_scan=CheckFinding(
            rule_id="deep-secret-scan",
            status=CheckStatus.SKIP,
            message="gitleaks unavailable",
            category="deep-secret-scan",
        ),
    )
    service, _repository, _resolver, _checker, _packer = _service(
        project,
        check_service=checker,
    )

    plan = service.plan("set-1", deep=True)

    assert plan.check.status is CheckStatus.PASS
    assert plan.check.deep_status is CheckStatus.SKIP
    assert plan.readiness is SubmissionReadiness.WARNING
    assert any("深度" in warning for warning in plan.warnings)


def test_pack_rejection_blocks_and_never_exposes_raw_secret_text(tmp_path: Path) -> None:
    project = _project(tmp_path)
    packer = FakePackService(
        project,
        rejected=("credentials.txt:hard-coded-secret:RAW_PACK_SECRET",),
    )
    service, _repository, _resolver, _checker, _packer = _service(
        project,
        pack_service=packer,
    )

    plan = service.plan("set-1")

    assert plan.readiness is SubmissionReadiness.BLOCKED
    assert plan.blockers
    assert "RAW_PACK_SECRET" not in plan.model_dump_json()
    assert "credentials.txt:hard-coded-secret" not in plan.model_dump_json()


@pytest.mark.parametrize(
    "destination_factory",
    (
        lambda project, tmp_path: project,
        lambda project, tmp_path: project / "inside",
        lambda project, tmp_path: project.parent,
        lambda project, tmp_path: project.parent.parent,
        lambda project, tmp_path: project.parent / "bad:name",
        lambda project, tmp_path: project.parent / "CON",
    ),
)
def test_destination_root_descendant_ancestor_or_name_is_blocked(
    tmp_path: Path,
    destination_factory,
) -> None:
    project = _project(tmp_path)
    service, _repository, _resolver, _checker, packer = _service(project)

    plan = service.plan("set-1", destination=destination_factory(project, tmp_path))

    assert plan.readiness is SubmissionReadiness.BLOCKED
    assert plan.blockers
    assert packer.calls == []


@pytest.mark.skipif(os.name == "nt", reason="FIFO fixture is not portable to Windows")
def test_destination_rejects_symlink_and_special_parents(tmp_path: Path) -> None:
    project = _project(tmp_path)
    real_parent = tmp_path / "real-parent"
    real_parent.mkdir()
    symlink_parent = tmp_path / "linked-parent"
    symlink_parent.symlink_to(real_parent, target_is_directory=True)
    service, _repository, _resolver, _checker, packer = _service(project)

    symlink_plan = service.plan("set-1", destination=symlink_parent / "handoff")

    fifo = tmp_path / "special-parent"
    os.mkfifo(fifo)
    special_plan = service.plan("set-1", destination=fifo / "handoff")

    assert symlink_plan.readiness is SubmissionReadiness.BLOCKED
    assert special_plan.readiness is SubmissionReadiness.BLOCKED
    assert packer.calls == []


def test_default_sibling_and_explicit_relative_destination_are_canonical(tmp_path: Path) -> None:
    project = _project(tmp_path)
    service, _repository, _resolver, _checker, _packer = _service(project)

    default_plan = service.plan("set-1")
    explicit_plan = service.plan(
        "set-1",
        destination=Path("..") / "deliverables" / "submission",
    )

    assert default_plan.destination == tmp_path / "课程项目-submission"
    assert explicit_plan.destination == tmp_path / "deliverables" / "submission"
    assert explicit_plan.readiness is SubmissionReadiness.READY


def test_existing_unowned_directory_blocks_even_when_pack_is_forced(tmp_path: Path) -> None:
    project = _project(tmp_path)
    destination = tmp_path / "owned-check"
    destination.mkdir()
    destination.joinpath("notes.txt").write_text("user content", encoding="utf-8")
    service, _repository, _resolver, _checker, packer = _service(project)

    plan = service.plan("set-1", destination=destination)

    assert plan.readiness is SubmissionReadiness.BLOCKED
    assert plan.destination_state != "owned"
    assert packer.calls
    assert destination.joinpath("notes.txt").read_text(encoding="utf-8") == "user content"


def test_unowned_destination_fingerprint_tracks_existing_file_content(tmp_path: Path) -> None:
    project = _project(tmp_path)
    destination = tmp_path / "existing-handoff"
    destination.mkdir()
    notes = destination / "notes.txt"
    notes.write_text("before", encoding="utf-8")
    service, _repository, _resolver, _checker, _packer = _service(project)

    first = service.plan("set-1", destination=destination)
    notes.write_text("after", encoding="utf-8")
    second = service.plan("set-1", destination=destination)

    assert first.destination_state == second.destination_state == "unowned"
    assert first.destination_fingerprint != second.destination_fingerprint


@pytest.mark.parametrize(
    ("probe_result", "expected_readiness"),
    (
        (
            SubmissionVerifyResult(
                status="PASS",
                verified=True,
                files=("report.docx", "configured-project.zip", "submission-manifest.json"),
            ),
            SubmissionReadiness.READY,
        ),
        (
            SubmissionVerifyResult(
                status="FAIL",
                verified=False,
                errors=("提交清单缺失。",),
            ),
            SubmissionReadiness.BLOCKED,
        ),
    ),
)
def test_existing_destination_uses_guarded_verifier_ownership_probe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    probe_result: SubmissionVerifyResult,
    expected_readiness: SubmissionReadiness,
) -> None:
    project = _project(tmp_path)
    destination = tmp_path / "existing-handoff"
    destination.mkdir()
    for filename in ("report.docx", "configured-project.zip", "submission-manifest.json"):
        destination.joinpath(filename).write_bytes(filename.encode())

    class Probe:
        def verify(self, path: Path) -> SubmissionVerifyResult:
            assert path == destination
            return probe_result

    monkeypatch.setattr(service_module, "SubmissionVerifier", lambda: Probe())
    service, _repository, _resolver, _checker, _packer = _service(project)

    plan = service.plan("set-1", destination=destination)

    assert plan.destination_state == (
        "owned" if expected_readiness is SubmissionReadiness.READY else "unowned"
    )
    assert plan.readiness is expected_readiness
    if expected_readiness is SubmissionReadiness.BLOCKED:
        assert any("提交目录" in blocker for blocker in plan.blockers)


def test_fingerprints_are_deterministic_and_change_with_inputs(tmp_path: Path) -> None:
    project = _project(tmp_path)
    evidence = _evidence_set()
    repository = FakeEvidenceRepository(evidence)
    service, repository, _resolver, _checker, _packer = _service(
        project,
        evidence_set=evidence,
    )

    first = service.plan("set-1")
    second = service.plan("set-1")
    repository.evidence_set = evidence.model_copy(update={"title": "变化后的集合"})
    changed = service.plan("set-1")

    assert first.evidence_fingerprint == second.evidence_fingerprint
    assert first.report_profile_fingerprint == second.report_profile_fingerprint
    assert first.pack_source_fingerprint == second.pack_source_fingerprint
    assert first.plan_fingerprint == second.plan_fingerprint
    assert first.evidence_fingerprint != changed.evidence_fingerprint
    assert all(
        len(value) == 64
        for value in (
            first.evidence_fingerprint,
            first.report_profile_fingerprint,
            first.pack_source_fingerprint,
            first.check_fingerprint,
            first.destination_fingerprint,
            first.plan_fingerprint,
        )
    )


def test_plan_does_not_write_project_or_destination(tmp_path: Path) -> None:
    project = _project(tmp_path)
    before = {
        path.relative_to(tmp_path).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in project.rglob("*")
        if path.is_file()
    }
    service, _repository, _resolver, _checker, _packer = _service(project)

    plan = service.plan("set-1", destination=Path("..") / "new" / "handoff")

    after = {
        path.relative_to(tmp_path).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in project.rglob("*")
        if path.is_file()
    }
    assert plan.readiness is SubmissionReadiness.READY
    assert before == after
    assert not (tmp_path / "new").exists()
