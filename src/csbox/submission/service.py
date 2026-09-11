"""Read-only Submission preflight and stale-state plan construction."""

from __future__ import annotations

import dataclasses
import errno
import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import threading
import unicodedata
from collections.abc import Callable, Mapping
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path

from pydantic import BaseModel

from csbox import __version__
from csbox.check.models import CheckStatus
from csbox.check.service import create_check_service
from csbox.core.safe_paths import (
    atomic_copy_file,
    atomic_write_text,
    is_reparse_metadata,
    mkdir_exclusive,
    open_regular_binary,
    safe_rename,
    validate_portable_relative_path,
)
from csbox.evidence.models import EvidenceSet, validate_identifier
from csbox.evidence.repository import EvidencePersistenceError, EvidenceSetRepository
from csbox.evidence.resolver import ResolvedReportableEvidence
from csbox.evidence.service import create_report_handoff_service
from csbox.pack.models import PackPlan
from csbox.pack.service import PackServiceError, create_pack_service
from csbox.report.models import ReportProfile
from csbox.report.repository import ReportProfilePersistenceError, ReportProfileRepository
from csbox.report.service import default_report_profile
from csbox.submission.models import (
    SubmissionArchiveSummary,
    SubmissionArtifactRole,
    SubmissionCheckSummary,
    SubmissionDestinationState,
    SubmissionError,
    SubmissionManifest,
    SubmissionManifestFile,
    SubmissionPlan,
    SubmissionReadiness,
    SubmissionResolvedSource,
    SubmissionResult,
    manifest_digest,
)
from csbox.submission.verifier import SubmissionVerifier

_FINGERPRINT_VERSION = 1
_REPORT_FILENAME = "report.docx"
_MANIFEST_FILENAME = "submission-manifest.json"
_DEFAULT_DESTINATION_FALLBACK = "csbox-submission"
_OUTPUT_COMPONENT_BUDGET = 236
_WINDOWS_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{index}" for index in range(1, 10)}
    | {f"LPT{index}" for index in range(1, 10)}
)
_FORBIDDEN_NAME_CHARACTERS = frozenset('<>:"/\\|?*')
_SAFE_SOURCE_REASONS = {
    "capture_missing": "source-missing",
    "capture_unavailable": "source-unavailable",
    "invalid_source_reference": "source-invalid",
    "run_missing": "source-missing",
    "run_unavailable": "source-unavailable",
    "step_missing": "source-missing",
    "unsupported_source_type": "source-unsupported",
}
_CHECK_WARNING_MESSAGES = {
    "readme": "项目缺少 README；建议补充运行和提交说明。",
    "artifacts": "项目包含会被打包过滤的构建、缓存或日志产物。",
    "large-file": "项目包含超过配置阈值的文件；请确认课程提交要求。",
    "local-absolute-path": "项目包含本机绝对路径引用；建议改用相对路径或配置。",
    "windows-absolute-path": "项目包含 Windows 本机绝对路径引用；建议改用相对路径或配置。",
    "unix-absolute-path": "项目包含 Unix 本机绝对路径引用；建议改用相对路径或配置。",
    "git-status": "Git 工作区存在尚未确认的变更。",
}

_DESTINATION_LOCKS: dict[str, threading.Lock] = {}
_DESTINATION_LOCKS_GUARD = threading.Lock()
_PREPARE_CHUNK_BYTES = 1024 * 1024


@dataclass(frozen=True, slots=True)
class _DestinationObservation:
    path: Path
    state: SubmissionDestinationState
    blockers: tuple[str, ...] = ()
    fingerprint_payload: Mapping[str, object] = dataclasses.field(default_factory=dict)


class SubmissionService:
    """Coordinate read-only Evidence, Report, Check, Pack, and destination checks."""

    def __init__(
        self,
        project_dir: Path | str,
        *,
        evidence_repository: object | None = None,
        report_service: object | None = None,
        profile_repository: object | None = None,
        check_service: object | None = None,
        pack_service: object | None = None,
        clock: Callable[[], datetime] | None = None,
        phase_hook: Callable[[str], None] | None = None,
    ) -> None:
        self.project_dir = _canonical_project_dir(project_dir)
        self.evidence_repository = (
            evidence_repository
            if evidence_repository is not None
            else EvidenceSetRepository.from_cwd(self.project_dir)
        )
        self.report_service = (
            report_service
            if report_service is not None
            else create_report_handoff_service(self.project_dir)
        )
        self.profile_repository = (
            profile_repository
            if profile_repository is not None
            else ReportProfileRepository.from_cwd(self.project_dir)
        )
        self.check_service = (
            check_service if check_service is not None else create_check_service(self.project_dir)
        )
        self.pack_service = (
            pack_service if pack_service is not None else create_pack_service(self.project_dir)
        )
        self.clock = clock if clock is not None else (lambda: datetime.now(UTC))
        self.phase_hook = phase_hook
        self._resolver = _report_resolver(self.report_service)

    def plan(
        self,
        evidence_set_id: str,
        *,
        destination: Path | str | None = None,
        deep: bool = False,
    ) -> SubmissionPlan:
        """Build a deterministic, non-publishing plan for one Evidence Set."""

        try:
            validated_id = validate_identifier(evidence_set_id, field_name="evidence_set_id")
        except (TypeError, ValueError):
            raise SubmissionError("证据集不可读取。", kind="evidence_unavailable") from None

        evidence_set = self._load_evidence_set(validated_id)
        blockers: list[str] = []
        warnings: list[str] = []

        profile, profile_fingerprint = self._load_profile(validated_id, blockers)
        resolved_values, resolved_summaries, resolved_payloads = self._resolve_sources(
            evidence_set,
            blockers,
        )
        evidence_fingerprint = _fingerprint(
            "evidence",
            {
                "evidence_set": evidence_set,
                "resolved_sources": resolved_payloads,
            },
        )

        check_report, check_summary = self._run_check(deep, blockers, warnings)
        check_fingerprint = _fingerprint(
            "check",
            {
                "options": {"build": False, "deep": deep},
                "summary": check_summary,
                "report": check_report,
            },
        )

        pack_filename, filename_blockers = self._pack_filename()
        blockers.extend(filename_blockers)
        destination_observation = self._observe_destination(
            destination,
            expected_archive_filename=pack_filename,
        )
        blockers.extend(destination_observation.blockers)

        raw_pack_plan: PackPlan | None = None
        safe_pack_plan: PackPlan | None = None
        archive_summary = SubmissionArchiveSummary(verified=False, contains_manifest=False)
        pack_source_fingerprint = _fingerprint("pack-source", {"state": "not-planned"})
        pack_fingerprint = _fingerprint("pack", {"state": "not-planned"})
        if destination_observation.state not in {
            SubmissionDestinationState.UNSAFE,
            SubmissionDestinationState.INVALID,
        }:
            try:
                raw_pack_plan = self._build_pack_plan(
                    destination_observation.path,
                    pack_filename,
                )
                safe_pack_plan = _safe_pack_plan(raw_pack_plan)
                archive_summary = SubmissionArchiveSummary(
                    verified=bool(getattr(raw_pack_plan, "verification_requested", False)),
                    contains_manifest=bool(getattr(raw_pack_plan, "include_manifest", False)),
                )
                raw_source_fingerprint = getattr(raw_pack_plan, "_source_fingerprint", "")
                if isinstance(raw_source_fingerprint, str) and re.fullmatch(
                    r"[0-9a-f]{64}", raw_source_fingerprint
                ):
                    # Pack already owns this source fingerprint. Retain it in
                    # the public plan for PackService's later revalidation;
                    # the Submission-domain pack fingerprint below adds the
                    # version/domain separation and content digest.
                    pack_source_fingerprint = raw_source_fingerprint
                else:
                    pack_source_fingerprint = _fingerprint(
                        "pack-source",
                        {"source_fingerprint": raw_source_fingerprint},
                    )
                # The owned destination is moved to backup before the final
                # live revalidation. Pack's output_exists therefore changes
                # naturally, while source files and Pack configuration remain
                # authoritative parts of this commitment.
                fingerprint_plan = raw_pack_plan.model_copy(update={"output_exists": False})
                pack_fingerprint = _fingerprint(
                    "pack",
                    {
                        "plan": fingerprint_plan,
                        "source_fingerprint": pack_source_fingerprint,
                        "source_files": _pack_source_content_fingerprints(raw_pack_plan),
                        "configuration": _pack_configuration(self.pack_service),
                    },
                )
                if _pack_has_blockers(raw_pack_plan):
                    blockers.append("项目内容无法安全打包。")
                warnings.extend(_safe_pack_warnings(raw_pack_plan))
            except Exception:
                blockers.append("无法建立项目 ZIP 计划。")
        else:
            pack_fingerprint = _fingerprint(
                "pack",
                {"state": "destination-blocked", "filename": pack_filename},
            )

        destination_fingerprint = _fingerprint(
            "destination",
            {
                "path": _path_identity(destination_observation.path),
                "state": destination_observation.state,
                "observation": destination_observation.fingerprint_payload,
                "final_names": {
                    "report": _REPORT_FILENAME,
                    "archive": pack_filename,
                    "manifest": _MANIFEST_FILENAME,
                },
            },
        )
        plan_fingerprint = _fingerprint(
            "plan",
            {
                "evidence": evidence_fingerprint,
                "profile": profile_fingerprint,
                "check": check_fingerprint,
                "pack_source": pack_source_fingerprint,
                "pack": pack_fingerprint,
                "destination": destination_fingerprint,
                "destination_path": _path_identity(destination_observation.path),
                "final_names": {
                    "report": _REPORT_FILENAME,
                    "archive": pack_filename,
                    "manifest": _MANIFEST_FILENAME,
                },
                "options": {"build": False, "deep": deep},
            },
        )

        _append_unique(blockers, _check_blockers(check_summary))
        _append_unique(warnings, _check_warnings(check_report, check_summary))
        if profile is None:
            _append_unique(blockers, ("报告结构配置不可读取。",))
        readiness = _readiness(blockers, warnings)

        plan = SubmissionPlan(
            evidence_set_id=validated_id,
            evidence_fingerprint=evidence_fingerprint,
            report_profile_fingerprint=profile_fingerprint,
            pack_source_fingerprint=pack_source_fingerprint,
            destination=destination_observation.path,
            report_filename=_REPORT_FILENAME,
            archive_filename=pack_filename,
            check=check_summary,
            project_archive=archive_summary,
            warnings=tuple(_deduplicate_text(warnings)),
            blockers=tuple(_deduplicate_text(blockers)),
            readiness=readiness,
            pack_plan=safe_pack_plan,
            fingerprint_version=1,
            check_fingerprint=check_fingerprint,
            destination_fingerprint=destination_fingerprint,
            pack_fingerprint=pack_fingerprint,
            plan_fingerprint=plan_fingerprint,
            destination_state=destination_observation.state,
            resolved_sources=tuple(resolved_summaries),
        )
        # Keep only trusted typed objects and the source-side raw Pack plan in
        # private state. Neither payloads nor the raw Pack rejection details
        # can cross the public Pydantic serialization boundary.
        plan._evidence_set = evidence_set
        plan._report_profile = profile
        plan._resolved_values = tuple(resolved_values)
        plan._check_report = check_report
        plan._fingerprint_payloads = {
            "resolved_sources": tuple(
                {
                    "index": item.index,
                    "source_type": item.source_type,
                    "available": item.available,
                    "fingerprint": item.fingerprint,
                    "unavailable_reason": item.unavailable_reason,
                }
                for item in resolved_summaries
            ),
            "final_names": {
                "report": _REPORT_FILENAME,
                "archive": pack_filename,
                "manifest": _MANIFEST_FILENAME,
            },
        }
        plan._pack_plan = raw_pack_plan
        return plan

    def prepare(
        self,
        plan: SubmissionPlan,
        *,
        force: bool = False,
        phase_callback: Callable[[str], None] | None = None,
    ) -> SubmissionResult:
        """Materialize and atomically publish a previously previewed plan."""
        if not isinstance(plan, SubmissionPlan):
            raise SubmissionError("提交预览无效，请重新预览提交材料。", kind="plan_invalid")
        destination = Path(plan.destination)
        lock_key = _path_identity(destination)
        with _destination_lease(destination, lock_key):
            staging: Path | None = None
            backup: Path | None = None
            published = False
            try:
                _emit_progress(phase_callback, "checking")
                current = self.plan(
                    plan.evidence_set_id,
                    destination=destination,
                    deep=plan.check.deep_requested,
                )
                if plan.readiness is SubmissionReadiness.BLOCKED:
                    raise SubmissionError("提交预览未通过，无法准备材料。", kind="blocked")
                _revalidate_submission_plan(plan, current)
                if current.destination_state is SubmissionDestinationState.OWNED and not force:
                    raise SubmissionError(
                        "已有提交目录，请使用 --force 刷新，或选择新的输出位置。",
                        kind="destination_exists",
                    )
                if current.pack_plan is None or plan._pack_plan is None:
                    raise SubmissionError("内容发生了变化，请重新预览提交材料。", kind="stale")
                try:
                    self.pack_service.validate_plan(plan._pack_plan, verify=True, force=True)
                except Exception as error:
                    raise SubmissionError(
                        "内容发生了变化，请重新预览提交材料。", kind="stale"
                    ) from error
                _invoke_phase(self.phase_hook, phase_callback, "after_preflight")
                _revalidate_live_plan(self, plan, destination)

                staging = _new_submission_staging(destination)
                report_bundle = staging / "report-bundle"
                _emit_progress(phase_callback, "reporting")
                report = self.report_service.export(
                    plan._evidence_set,
                    report_bundle,
                    force=False,
                    report_profile=plan._report_profile,
                    resolved_snapshot=tuple(
                        zip(plan._evidence_set.items, plan._resolved_values, strict=True)
                    ),
                )
                report_warnings = tuple(
                    value
                    for value in (getattr(report, "warnings", ()) or ())
                    if isinstance(value, str)
                )
                report_source = Path(getattr(report, "docx", report_bundle / _REPORT_FILENAME))
                if not _is_regular_file(report_source):
                    raise SubmissionError(
                        "报告材料导出失败，未生成成功结果。", kind="report_failed"
                    )
                atomic_copy_file(
                    report_source,
                    staging / plan.report_filename,
                    replace_existing=False,
                )
                _remove_owned_tree(report_bundle)
                _invoke_phase(self.phase_hook, phase_callback, "after_report")
                _revalidate_live_plan(self, plan, destination)

                _emit_progress(phase_callback, "packing")
                self.pack_service.pack_plan(
                    plan._pack_plan,
                    verify=True,
                    force=True,
                    destination=staging / plan.archive_filename,
                )
                _invoke_phase(self.phase_hook, phase_callback, "after_pack")
                _revalidate_live_plan(self, plan, destination)

                manifest = _build_submission_manifest(self, plan, staging)
                atomic_write_text(
                    staging / _MANIFEST_FILENAME,
                    manifest.serialize(),
                    replace_existing=False,
                )
                _invoke_phase(self.phase_hook, phase_callback, "after_manifest")
                _revalidate_live_plan(self, plan, destination)

                _emit_progress(phase_callback, "verifying")
                verification = SubmissionVerifier().verify(staging)
                if not verification.verified:
                    raise SubmissionError("提交材料校验失败。", kind="verification_failed")
                _invoke_phase(self.phase_hook, phase_callback, "after_staged_verify")
                _revalidate_live_plan(self, plan, destination)

                _emit_progress(phase_callback, "publishing")
                observation = self._observe_destination(
                    destination,
                    expected_archive_filename=plan.archive_filename,
                )
                if observation.state is SubmissionDestinationState.UNOWNED:
                    raise SubmissionError(
                        "已有提交目录不是可安全刷新的 CSBox 交付目录。", kind="destination_unowned"
                    )
                if observation.state is SubmissionDestinationState.OWNED:
                    backup = _new_submission_backup(destination)
                    safe_rename(destination, backup, replace_existing=False)
                    backup_observation = self._probe_existing_destination(
                        backup,
                        plan.archive_filename,
                    )
                    if backup_observation.state is not SubmissionDestinationState.OWNED:
                        raise SubmissionError(
                            "旧的提交目录校验失败，已保留备份。", kind="publication_failed"
                        )
                    _invoke_phase(self.phase_hook, phase_callback, "after_backup")
                    _revalidate_live_plan(
                        self,
                        plan,
                        destination,
                        allow_destination_change=True,
                    )
                safe_rename(staging, destination, replace_existing=False)
                staging = None
                published = True
                _invoke_phase(self.phase_hook, phase_callback, "after_publish")

                warnings = [*plan.warnings, *report_warnings]
                if backup is not None:
                    try:
                        _remove_owned_tree(backup)
                    except Exception:
                        warnings.append("旧的提交目录未能清理，已保留为备份。")
                    _invoke_phase(self.phase_hook, phase_callback, "after_backup_cleanup")
                _emit_progress(phase_callback, "complete")
                return SubmissionResult(
                    destination=destination,
                    report_path=destination / plan.report_filename,
                    archive_path=destination / plan.archive_filename,
                    manifest_path=destination / _MANIFEST_FILENAME,
                    check_status=plan.check.status,
                    warning_count=plan.check.warning_count,
                    verified=True,
                    warnings=tuple(warnings),
                )
            except SubmissionError:
                if not published:
                    _rollback_submission(staging, backup, destination)
                raise
            except Exception as error:
                if not published:
                    _rollback_submission(staging, backup, destination)
                raise SubmissionError(
                    "提交材料准备失败，请检查后重试。", kind="submission_failed"
                ) from error

    def _load_evidence_set(self, evidence_set_id: str) -> EvidenceSet:
        try:
            evidence_set = self.evidence_repository.load(evidence_set_id)
        except (EvidencePersistenceError, OSError, RuntimeError, TypeError, ValueError):
            raise SubmissionError("证据集不可读取。", kind="evidence_unavailable") from None
        if not isinstance(evidence_set, EvidenceSet):
            raise SubmissionError("证据集不可读取。", kind="evidence_unavailable")
        if evidence_set.evidence_set_id != evidence_set_id:
            raise SubmissionError("证据集不可读取。", kind="evidence_unavailable")
        return evidence_set

    def _load_profile(
        self,
        evidence_set_id: str,
        blockers: list[str],
    ) -> tuple[ReportProfile | None, str]:
        try:
            default = default_report_profile(self.project_dir)
        except (OSError, RuntimeError, TypeError, ValueError):
            default = ReportProfile.default()
            blockers.append("报告默认结构不可用。")
        try:
            loader = getattr(self.profile_repository, "load_or_default", None)
            if callable(loader):
                profile = loader(evidence_set_id, default=default)
            else:
                loader = getattr(self.profile_repository, "load", None)
                if not callable(loader):
                    raise TypeError("profile repository has no load boundary")
                try:
                    profile = loader(evidence_set_id)
                except FileNotFoundError:
                    profile = default
        except (
            FileNotFoundError,
            ReportProfilePersistenceError,
            OSError,
            RuntimeError,
            TypeError,
            ValueError,
        ):
            return None, _fingerprint("report-profile", {"state": "invalid"})
        if not isinstance(profile, ReportProfile):
            return None, _fingerprint("report-profile", {"state": "invalid"})
        return profile, _fingerprint("report-profile", {"profile": profile})

    def _resolve_sources(
        self,
        evidence_set: EvidenceSet,
        blockers: list[str],
    ) -> tuple[tuple[object, ...], tuple[SubmissionResolvedSource, ...], tuple[object, ...]]:
        values: list[object] = []
        summaries: list[SubmissionResolvedSource] = []
        payloads: list[object] = []
        for index, item in enumerate(evidence_set.items, start=1):
            source = item.source
            try:
                resolved = self._resolver.resolve(source) if self._resolver is not None else None
            except Exception:
                resolved = ResolvedReportableEvidence(
                    source=source,
                    unavailable_reason="source-unavailable",
                )
            if resolved is None:
                resolved = ResolvedReportableEvidence(
                    source=source,
                    unavailable_reason="source-unsupported",
                )
            available = _resolved_available(resolved)
            reason = _safe_source_reason(getattr(resolved, "unavailable_reason", None))
            payload = _resolved_payload(source, resolved)
            source_fingerprint = _fingerprint("resolved-source", payload)
            values.append(resolved)
            payloads.append(payload)
            summaries.append(
                SubmissionResolvedSource(
                    index=index,
                    source_type=_source_type(source),
                    available=available,
                    fingerprint=source_fingerprint,
                    unavailable_reason=None if available else reason,
                )
            )
            if not available:
                _append_unique(blockers, ("证据来源不可用，请检查对应的 Lab 或 API 记录。",))
        return tuple(values), tuple(summaries), tuple(payloads)

    def _run_check(
        self,
        deep: bool,
        blockers: list[str],
        warnings: list[str],
    ) -> tuple[object, SubmissionCheckSummary]:
        try:
            report = self.check_service.run(self.project_dir, build=False, deep=deep)
        except Exception:
            blockers.append("项目检查无法完成。")
            summary = SubmissionCheckSummary(
                status=CheckStatus.FAIL,
                warning_count=0,
                build_requested=False,
                deep_requested=deep,
                deep_status=None,
            )
            return None, summary

        findings = _check_findings(report, include_deep=deep)
        warning_count = sum(_status(finding) is CheckStatus.WARN for finding in findings)
        status = _status(report) or CheckStatus.FAIL
        deep_scan = getattr(report, "deep_scan", None) if deep else None
        deep_status = _status(deep_scan)
        summary = SubmissionCheckSummary(
            status=status,
            warning_count=warning_count,
            build_requested=False,
            deep_requested=deep,
            deep_status=deep_status,
        )
        if status is CheckStatus.SKIP:
            warnings.append("项目检查未给出完整结果；请确认检查环境后再交付。")
        if deep and deep_status is CheckStatus.SKIP:
            warnings.append("深度扫描未完成，本次结果不能证明项目没有 secret。")
        return report, summary

    def _pack_filename(self) -> tuple[str, tuple[str, ...]]:
        candidates: list[object] = []
        method = getattr(self.pack_service, "_filename", None)
        if callable(method):
            with suppress(Exception):
                candidates.append(method())
        for attribute in ("filename", "pack_filename", "output_filename"):
            value = getattr(self.pack_service, attribute, None)
            if value is not None:
                candidates.append(value)
        template = getattr(self.pack_service, "filename_template", None)
        if isinstance(template, str):
            with suppress(KeyError, ValueError):
                candidates.append(
                    template.format(
                        id=getattr(self.pack_service, "student_id", None) or "student",
                        name=getattr(self.pack_service, "student_name", None) or "project",
                        course=getattr(self.pack_service, "course_name", None) or "course",
                    )
                )
        for value in candidates:
            if not isinstance(value, str):
                continue
            safe = _safe_pack_filename(value)
            if safe is not None:
                return safe, ()
        return "project.zip", ("打包文件名配置不安全。",)

    def _observe_destination(
        self,
        destination: Path | str | None,
        *,
        expected_archive_filename: str,
    ) -> _DestinationObservation:
        try:
            raw = _destination_path(self.project_dir, destination)
        except (TypeError, ValueError, OSError):
            return _invalid_destination(
                self.project_dir,
                SubmissionDestinationState.UNSAFE,
                "提交目录名称不安全。",
            )

        if destination is None:
            candidate = self.project_dir.parent / _default_destination_name(self.project_dir.name)
        else:
            try:
                _validate_explicit_destination_name(raw, destination)
            except (TypeError, UnicodeError, ValueError):
                return _invalid_destination(
                    self.project_dir,
                    SubmissionDestinationState.UNSAFE,
                    "提交目录名称不安全。",
                )
            # Normalize navigation lexically before inspecting existing
            # components.  The final canonical relation checks below still
            # reject the project root, descendants, and ancestors.
            candidate = Path(os.path.normpath(os.fspath(raw)))

        try:
            candidate = candidate.absolute()
            _validate_destination_chain(candidate)
            canonical = candidate.resolve(strict=False)
        except _UnsafeDestination:
            return _invalid_destination(
                candidate,
                SubmissionDestinationState.UNSAFE,
                "提交目录的父路径不安全。",
            )
        except _InvalidDestination:
            return _invalid_destination(
                candidate,
                SubmissionDestinationState.INVALID,
                "提交目录的父路径不可用。",
            )
        except (OSError, RuntimeError, ValueError):
            return _invalid_destination(
                candidate,
                SubmissionDestinationState.UNSAFE,
                "提交目录无法安全解析。",
            )

        relation = _destination_relation(canonical, self.project_dir)
        if relation == "root":
            return _invalid_destination(
                candidate,
                SubmissionDestinationState.INVALID,
                "提交目录不能是项目目录。",
            )
        if relation == "descendant":
            return _invalid_destination(
                candidate,
                SubmissionDestinationState.INVALID,
                "提交目录不能位于项目目录内。",
            )
        if relation == "ancestor":
            return _invalid_destination(
                candidate,
                SubmissionDestinationState.INVALID,
                "提交目录不能包含项目目录。",
            )

        try:
            metadata = candidate.lstat()
        except FileNotFoundError:
            return _DestinationObservation(
                path=candidate,
                state=SubmissionDestinationState.MISSING,
                fingerprint_payload={"state": "missing"},
            )
        except OSError:
            return _invalid_destination(
                candidate,
                SubmissionDestinationState.UNSAFE,
                "提交目录无法安全读取。",
            )
        if is_reparse_metadata(metadata):
            return _invalid_destination(
                candidate,
                SubmissionDestinationState.UNSAFE,
                "提交目录或其父路径包含不可安全使用的链接。",
            )
        if not stat.S_ISDIR(metadata.st_mode):
            return _invalid_destination(
                candidate,
                SubmissionDestinationState.INVALID,
                "提交目录必须是目录。",
            )

        return self._probe_existing_destination(candidate, expected_archive_filename)

    def _probe_existing_destination(
        self,
        destination: Path,
        expected_archive_filename: str,
    ) -> _DestinationObservation:
        expected_files = (_REPORT_FILENAME, expected_archive_filename, _MANIFEST_FILENAME)
        verified = False
        reported_files: tuple[str, ...] = ()
        try:
            result = SubmissionVerifier().verify(destination)
            verified = (
                getattr(result, "status", None) == "PASS"
                and getattr(result, "verified", False) is True
            )
            raw_files = getattr(result, "files", ())
            if isinstance(raw_files, (list, tuple)) and all(
                isinstance(value, str) for value in raw_files
            ):
                reported_files = tuple(raw_files)
        except Exception:
            verified = False

        direct_files = _direct_regular_file_names(destination)
        exactly_owned = (
            direct_files == frozenset(expected_files) and reported_files == expected_files
        )
        if verified and exactly_owned:
            records = _destination_file_records(destination, expected_files)
            return _DestinationObservation(
                path=destination,
                state=SubmissionDestinationState.OWNED,
                fingerprint_payload={"state": "owned", "files": records},
            )
        return _DestinationObservation(
            path=destination,
            state=SubmissionDestinationState.UNOWNED,
            blockers=("已有提交目录不是可安全刷新的 CSBox 交付目录。",),
            fingerprint_payload={
                "state": "unowned",
                "file_count": None if direct_files is None else len(direct_files),
                "files": (
                    _destination_file_records(destination, tuple(sorted(direct_files)))
                    if direct_files is not None
                    else ("unreadable",)
                ),
            },
        )

    def _build_pack_plan(self, destination: Path, archive_filename: str) -> PackPlan:
        result = self.pack_service.plan(
            self.project_dir,
            destination=destination / archive_filename,
            verify=True,
            force=True,
            include_manifest=True,
        )
        if not isinstance(result, PackPlan):
            try:
                result = PackPlan.model_validate(result, strict=True)
            except Exception:
                raise PackServiceError("无法建立项目 ZIP 计划。", kind="plan_failed") from None
        if (
            Path(result.destination) != destination / archive_filename
            or result.output_filename != archive_filename
            or result.verification_requested is not True
            or result.include_manifest is not True
            or result.force is not True
        ):
            raise PackServiceError("无法建立项目 ZIP 计划。", kind="plan_failed")
        return result


def create_submission_service(cwd: Path | str | None = None) -> SubmissionService:
    """Create a SubmissionService rooted at ``cwd`` or the current directory."""

    return SubmissionService(Path.cwd() if cwd is None else Path(cwd))


class _UnsafeDestination(ValueError):
    pass


class _InvalidDestination(ValueError):
    pass


def _close_lock_descriptor(descriptor: object) -> OSError | ValueError | None:
    """Close a lock descriptor without masking the primary lease error.

    Windows can report ``PermissionError`` while a competing process holds
    the byte-range lock.  The close fallback still releases our OS handle if
    the buffered wrapper could not finish its own close operation.
    """

    try:
        descriptor.close()  # type: ignore[attr-defined]
    except (OSError, ValueError) as error:
        try:
            descriptor_fd = descriptor.fileno()  # type: ignore[attr-defined]
        except (OSError, ValueError, AttributeError):
            return error
        with suppress(OSError, ValueError):
            os.close(descriptor_fd)
        return error
    return None


def _canonical_project_dir(project_dir: Path | str) -> Path:
    try:
        raw = Path(project_dir)
        metadata = raw.lstat()
        if is_reparse_metadata(metadata) or not stat.S_ISDIR(metadata.st_mode):
            raise ValueError
        resolved = raw.resolve(strict=True)
        resolved_metadata = resolved.lstat()
        if is_reparse_metadata(resolved_metadata) or not stat.S_ISDIR(resolved_metadata.st_mode):
            raise ValueError
        return resolved
    except (OSError, RuntimeError, TypeError, ValueError):
        raise SubmissionError("项目目录不可用。", kind="project_unavailable") from None


@contextmanager
def _destination_lease(destination: Path, key: str):
    """Hold both the process and native inter-process destination lease."""
    with _DESTINATION_LOCKS_GUARD:
        process_lock = _DESTINATION_LOCKS.setdefault(key, threading.Lock())
    if not process_lock.acquire(blocking=False):
        raise SubmissionError("该提交目录正在准备，请稍后重试。", kind="busy")
    lock_path = (
        destination.parent
        / f".csbox-submission-lock-{hashlib.sha256(key.encode()).hexdigest()[:24]}.lock"
    )
    try:
        try:
            _validate_destination_chain(lock_path.parent)
        except (_UnsafeDestination, _InvalidDestination, OSError, RuntimeError, ValueError):
            raise SubmissionError(
                "提交目录无法安全锁定，请稍后重试。", kind="lock_failed"
            ) from None
        descriptor_fd: int | None = None
        try:
            flags = os.O_RDWR
            no_follow = getattr(os, "O_NOFOLLOW", 0)
            try:
                metadata = lock_path.lstat()
            except FileNotFoundError:
                try:
                    descriptor_fd = os.open(
                        lock_path,
                        flags | os.O_CREAT | os.O_EXCL | no_follow,
                        0o600,
                    )
                except FileExistsError:
                    metadata = lock_path.lstat()
                else:
                    metadata = None
            if descriptor_fd is None:
                if (
                    metadata is None
                    or is_reparse_metadata(metadata)
                    or not stat.S_ISREG(metadata.st_mode)
                ):
                    raise OSError("destination lock is not a regular file")
                descriptor_fd = os.open(lock_path, flags | no_follow)
            descriptor = os.fdopen(descriptor_fd, "a+b")
            descriptor_fd = None
        except (OSError, ValueError):
            if descriptor_fd is not None:
                with suppress(OSError):
                    os.close(descriptor_fd)
            raise SubmissionError(
                "提交目录无法安全锁定，请稍后重试。", kind="lock_failed"
            ) from None
        native_locked = False
        primary_error: BaseException | None = None
        try:
            try:
                if os.name == "posix":
                    import fcntl

                    fcntl.flock(descriptor.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                elif os.name == "nt":
                    import msvcrt

                    descriptor.seek(0)
                    descriptor.write(b"0")
                    descriptor.flush()
                    descriptor.seek(0)
                    msvcrt.locking(descriptor.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    raise OSError("native destination locking is unavailable")
                native_locked = True
            except (OSError, ImportError, AttributeError, ValueError) as error:
                if isinstance(error, OSError) and error.errno in {
                    getattr(errno, "EACCES", None),
                    getattr(errno, "EAGAIN", None),
                }:
                    raise SubmissionError("该提交目录正在准备，请稍后重试。", kind="busy") from None
                raise SubmissionError(
                    "提交目录无法安全锁定，请稍后重试。", kind="lock_failed"
                ) from None
            yield
        except BaseException as error:
            primary_error = error
            raise
        finally:
            if native_locked:
                with suppress(OSError, ValueError):
                    if os.name == "posix":
                        import fcntl

                        fcntl.flock(descriptor.fileno(), fcntl.LOCK_UN)
                    elif os.name == "nt":
                        import msvcrt

                        descriptor.seek(0)
                        msvcrt.locking(descriptor.fileno(), msvcrt.LK_UNLCK, 1)
            close_error = _close_lock_descriptor(descriptor)
            if close_error is not None and primary_error is None:
                raise SubmissionError(
                    "提交目录无法安全解锁，请稍后重试。", kind="lock_failed"
                ) from close_error
    finally:
        process_lock.release()


def _emit_progress(callback: Callable[[str], None] | None, phase: str) -> None:
    if callback is not None:
        callback(phase)


def _invoke_phase(
    hook: Callable[[str], None] | None,
    callback: Callable[[str], None] | None,
    phase: str,
) -> None:
    if hook is not None:
        hook(phase)
    if callback is not None and callback is not hook:
        callback(phase)


def _revalidate_submission_plan(expected: SubmissionPlan, current: SubmissionPlan) -> None:
    fields = (
        "evidence_fingerprint",
        "report_profile_fingerprint",
        "pack_source_fingerprint",
        "check_fingerprint",
        "pack_fingerprint",
        "destination_fingerprint",
        "plan_fingerprint",
        "archive_filename",
    )
    if any(getattr(expected, field) != getattr(current, field) for field in fields):
        raise SubmissionError("内容发生了变化，请重新预览提交材料。", kind="stale")


def _revalidate_live_plan(
    service: SubmissionService,
    expected: SubmissionPlan,
    destination: Path,
    *,
    allow_destination_change: bool = False,
) -> None:
    current = service.plan(
        expected.evidence_set_id,
        destination=destination,
        deep=expected.check.deep_requested,
    )
    if allow_destination_change:
        fields = (
            "evidence_fingerprint",
            "report_profile_fingerprint",
            "pack_source_fingerprint",
            "check_fingerprint",
            "pack_fingerprint",
            "archive_filename",
        )
        if any(getattr(expected, field) != getattr(current, field) for field in fields):
            raise SubmissionError("内容发生了变化，请重新预览提交材料。", kind="stale")
    else:
        _revalidate_submission_plan(expected, current)


def _new_submission_staging(destination: Path) -> Path:
    parent = destination.parent
    for _ in range(8):
        candidate = parent / f".{destination.name}-{next(tempfile._get_candidate_names())}.partial"
        try:
            mkdir_exclusive(candidate)
        except FileExistsError:
            continue
        return candidate
    raise SubmissionError("提交材料暂存位置不可用。", kind="staging_failed")


def _new_submission_backup(destination: Path) -> Path:
    for _ in range(8):
        candidate = (
            destination.parent
            / f".{destination.name}-{next(tempfile._get_candidate_names())}.backup"
        )
        if not candidate.exists() and not candidate.is_symlink():
            return candidate
    raise SubmissionError("旧的提交目录无法安全备份。", kind="publication_failed")


def _is_regular_file(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except OSError:
        return False
    return stat.S_ISREG(metadata.st_mode) and not is_reparse_metadata(metadata)


def _remove_owned_tree(path: Path) -> None:
    if not path.exists() and not path.is_symlink():
        return
    metadata = path.lstat()
    if is_reparse_metadata(metadata):
        raise ValueError("refusing to remove a linked path")
    if not stat.S_ISDIR(metadata.st_mode):
        raise ValueError("refusing to remove a non-directory")
    _validate_private_tree(path)
    shutil.rmtree(path)


def _validate_private_tree(path: Path) -> None:
    """Validate every entry before removing a private staging/backup tree."""
    try:
        with os.scandir(path) as directory:
            for entry in directory:
                metadata = entry.stat(follow_symlinks=False)
                if is_reparse_metadata(metadata):
                    raise ValueError("refusing to remove a linked private entry")
                child = Path(entry.path)
                if stat.S_ISDIR(metadata.st_mode):
                    _validate_private_tree(child)
                elif not stat.S_ISREG(metadata.st_mode):
                    raise ValueError("refusing to remove a special private entry")
    except OSError:
        raise


def _rollback_submission(staging: Path | None, backup: Path | None, destination: Path) -> None:
    if staging is not None:
        with suppress(OSError, ValueError):
            _remove_owned_tree(staging)
    if backup is not None:
        try:
            backup_metadata = backup.lstat()
            destination_metadata = destination.lstat()
        except FileNotFoundError:
            destination_metadata = None
            try:
                backup_metadata = backup.lstat()
            except OSError:
                return
        except OSError:
            return
        if (
            is_reparse_metadata(backup_metadata)
            or not stat.S_ISDIR(backup_metadata.st_mode)
            or destination_metadata is not None
        ):
            return
        with suppress(OSError, ValueError):
            safe_rename(backup, destination, replace_existing=False)


def _build_submission_manifest(
    service: SubmissionService,
    plan: SubmissionPlan,
    staging: Path,
) -> SubmissionManifest:
    report_path = staging / plan.report_filename
    archive_path = staging / plan.archive_filename
    report_size, report_sha = _regular_file_digest(report_path)
    archive_size, archive_sha = _regular_file_digest(archive_path)
    manifest = SubmissionManifest(
        schema_version=1,
        csbox_version=__version__,
        generated_at=service.clock()
        .astimezone(UTC)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z"),
        evidence_set_id=plan.evidence_set_id,
        check=plan.check,
        project_archive=plan.project_archive,
        files=(
            SubmissionManifestFile(
                role=SubmissionArtifactRole.REPORT_DOCX,
                path=plan.report_filename,
                size=report_size,
                sha256=report_sha,
            ),
            SubmissionManifestFile(
                role=SubmissionArtifactRole.PROJECT_ZIP,
                path=plan.archive_filename,
                size=archive_size,
                sha256=archive_sha,
            ),
        ),
        manifest_sha256="0" * 64,
    )
    return manifest.model_copy(update={"manifest_sha256": manifest_digest(manifest)})


def _report_resolver(report_service: object) -> object | None:
    for candidate in (
        report_service,
        getattr(report_service, "exporter", None),
    ):
        if candidate is not None and callable(getattr(candidate, "resolve", None)):
            return candidate
        resolver = getattr(candidate, "resolver", None) if candidate is not None else None
        if resolver is not None and callable(getattr(resolver, "resolve", None)):
            return resolver
    return None


def _status(value: object) -> CheckStatus | None:
    raw = getattr(value, "status", None)
    if isinstance(raw, CheckStatus):
        return raw
    if isinstance(raw, str):
        try:
            return CheckStatus(raw)
        except ValueError:
            return None
    return None


def _check_findings(report: object, *, include_deep: bool = True) -> tuple[object, ...]:
    if report is None:
        return ()
    findings = list(getattr(report, "findings", ()) or ())
    findings.extend(getattr(report, "builds", ()) or ())
    deep_scan = getattr(report, "deep_scan", None)
    if include_deep and deep_scan is not None:
        findings.append(deep_scan)
    return tuple(findings)


def _check_blockers(summary: SubmissionCheckSummary) -> tuple[str, ...]:
    blockers: list[str] = []
    if summary.status is CheckStatus.FAIL:
        blockers.append("项目检查未通过，请先处理安全检查问题。")
    if summary.deep_status is CheckStatus.FAIL:
        blockers.append("深度扫描未通过，请先处理安全检查问题。")
    return tuple(blockers)


def _check_warnings(
    report: object,
    summary: SubmissionCheckSummary,
) -> tuple[str, ...]:
    messages: list[str] = []
    for finding in _check_findings(report, include_deep=summary.deep_requested):
        if _status(finding) is not CheckStatus.WARN:
            continue
        rule_id = str(getattr(finding, "rule_id", ""))
        category = str(getattr(finding, "category", ""))
        messages.append(
            _CHECK_WARNING_MESSAGES.get(
                rule_id,
                _CHECK_WARNING_MESSAGES.get(
                    category,
                    "检查项目发现一项提醒，请运行 csbox check 查看安全建议。",
                ),
            )
        )
    if summary.status is CheckStatus.WARN and not messages:
        messages.append("项目检查发现提醒，请确认后再交付。")
    if summary.status is CheckStatus.SKIP and not messages:
        messages.append("项目检查未给出完整结果；请确认检查环境后再交付。")
    if summary.deep_status is CheckStatus.WARN:
        messages.append("深度扫描发现提醒，请确认项目中没有需要移除的 secret。")
    if summary.deep_status is CheckStatus.SKIP:
        messages.append("深度扫描未完成，本次结果不能证明项目没有 secret。")
    return tuple(messages)


def _readiness(blockers: list[str], warnings: list[str]) -> SubmissionReadiness:
    if blockers:
        return SubmissionReadiness.BLOCKED
    return SubmissionReadiness.WARNING if warnings else SubmissionReadiness.READY


def _resolved_available(resolved: object) -> bool:
    try:
        available = resolved.available  # type: ignore[attr-defined]
    except Exception:
        available = False
    if isinstance(available, bool):
        return available
    return getattr(resolved, "unavailable_reason", None) is None and (
        getattr(resolved, "capture", None) is not None
        or getattr(resolved, "api_evidence", None) is not None
    )


def _source_type(source: object) -> str:
    value = getattr(source, "source_type", None)
    return value if isinstance(value, str) and value else "unsupported"


def _safe_source_reason(reason: object) -> str:
    if isinstance(reason, str):
        return _SAFE_SOURCE_REASONS.get(reason, "source-unavailable")
    return "source-unavailable"


def _resolved_payload(source: object, resolved: object) -> dict[str, object]:
    return {
        "source": _json_ready(source),
        "source_type": _source_type(source),
        "available": _resolved_available(resolved),
        "session_name": getattr(resolved, "session_name", None),
        "scenario_name": getattr(resolved, "scenario_name", None),
        "step_name": getattr(resolved, "step_name", None),
        "run_status": getattr(resolved, "run_status", None),
        "capture": _json_ready(getattr(resolved, "capture", None)),
        "api_evidence": _json_ready(getattr(resolved, "api_evidence", None)),
        "unavailable_reason": _safe_source_reason(getattr(resolved, "unavailable_reason", None)),
    }


def _safe_pack_plan(plan: PackPlan) -> PackPlan:
    rejected = tuple("project:content-rejected" for _ in getattr(plan, "rejected", ()))
    warnings = tuple("打包计划包含过滤提醒。" for _ in getattr(plan, "warnings", ()))
    if not rejected and _pack_has_blockers(plan):
        rejected = ("project:pack-blocked",)
    return plan.model_copy(update={"rejected": rejected, "warnings": warnings})


def _safe_pack_warnings(plan: PackPlan) -> tuple[str, ...]:
    return tuple("打包计划包含过滤提醒。" for _ in getattr(plan, "warnings", ()))


def _pack_has_blockers(plan: PackPlan) -> bool:
    try:
        blockers = plan.blockers
    except Exception:
        blockers = getattr(plan, "rejected", ())
    return bool(tuple(blockers or ()))


def _pack_configuration(pack_service: object) -> dict[str, object]:
    values: dict[str, object] = {}
    for name in (
        "filename_template",
        "student_id",
        "student_name",
        "course_name",
    ):
        value = getattr(pack_service, name, None)
        if isinstance(value, (str, type(None))):
            values[name] = value
    return values


def _pack_source_content_fingerprints(plan: PackPlan) -> tuple[dict[str, object], ...]:
    root = Path(plan.source_root)
    records: list[dict[str, object]] = []
    names = tuple(name for name in getattr(plan, "included", ()) if name != "manifest.json")
    for name in names:
        try:
            relative = validate_portable_relative_path(name)
            path = root.joinpath(*relative.parts)
            size, digest = _regular_file_digest(path)
        except (OSError, TypeError, UnicodeError, ValueError):
            records.append({"path": str(name), "state": "unavailable"})
        else:
            records.append({"path": relative.as_posix(), "size": size, "sha256": digest})
    return tuple(records)


def _regular_file_digest(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with open_regular_binary(path) as stream:
        while chunk := stream.read(1024 * 1024):
            size += len(chunk)
            digest.update(chunk)
    return size, digest.hexdigest()


def _destination_path(project_dir: Path, destination: Path | str | None) -> Path:
    if destination is None:
        return project_dir.parent / _default_destination_name(project_dir.name)
    candidate = Path(destination)
    if not candidate.is_absolute():
        candidate = project_dir / candidate
    return candidate


def _validate_explicit_destination_name(raw: Path, supplied: Path | str) -> None:
    del raw
    candidate = Path(supplied)
    for component in candidate.parts:
        if component == candidate.anchor:
            continue
        if component in {".", ".."}:
            continue
        _validate_output_component(component)


def _validate_destination_chain(path: Path) -> None:
    components = path.parts[1:] if path.anchor else path.parts
    current = Path(path.anchor) if path.anchor else Path.cwd()
    missing = False
    for index, component in enumerate(components):
        current = current / component
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            missing = True
            continue
        except OSError:
            raise _UnsafeDestination from None
        if is_reparse_metadata(metadata):
            raise _UnsafeDestination
        if missing:
            # A path cannot safely reappear below a missing component without
            # a concurrent writer; fail closed instead of guessing its parent.
            raise _UnsafeDestination
        if index < len(components) - 1 and not stat.S_ISDIR(metadata.st_mode):
            raise _InvalidDestination


def _destination_relation(candidate: Path, project_dir: Path) -> str | None:
    candidate_key = _path_identity(candidate)
    project_key = _path_identity(project_dir)
    if candidate_key == project_key:
        return "root"
    if _path_is_relative(candidate, project_dir):
        return "descendant"
    if _path_is_relative(project_dir, candidate):
        return "ancestor"
    return None


def _path_is_relative(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=False))
    except (OSError, RuntimeError, ValueError):
        pass
    else:
        return True
    path_parts = _path_identity(path).split(os.sep)
    root_parts = _path_identity(root).split(os.sep)
    return len(path_parts) > len(root_parts) and path_parts[: len(root_parts)] == root_parts


def _invalid_destination(
    path: Path,
    state: SubmissionDestinationState,
    blocker: str,
) -> _DestinationObservation:
    safe_path = path if path.is_absolute() else Path.cwd() / path
    return _DestinationObservation(
        path=safe_path,
        state=state,
        blockers=(blocker,),
        fingerprint_payload={"state": state.value},
    )


def _direct_regular_file_names(path: Path) -> frozenset[str] | None:
    names: set[str] = set()
    try:
        with os.scandir(path) as directory:
            for entry in directory:
                metadata = entry.stat(follow_symlinks=False)
                if is_reparse_metadata(metadata) or not stat.S_ISREG(metadata.st_mode):
                    return None
                names.add(entry.name)
    except OSError:
        return None
    return frozenset(names)


def _destination_file_records(
    destination: Path,
    expected_files: tuple[str, ...],
) -> tuple[dict[str, object], ...]:
    records: list[dict[str, object]] = []
    for name in expected_files:
        try:
            size, digest = _regular_file_digest(destination / name)
        except (OSError, TypeError, ValueError):
            records.append({"path": name, "state": "unavailable"})
        else:
            records.append({"path": name, "size": size, "sha256": digest})
    return tuple(records)


def _default_destination_name(project_name: str) -> str:
    normalized = unicodedata.normalize("NFKC", project_name)
    characters: list[str] = []
    for character in normalized:
        category = unicodedata.category(character)
        if (
            ord(character) < 0x20
            or ord(character) == 0x7F
            or category.startswith("C")
            or character in _FORBIDDEN_NAME_CHARACTERS
        ):
            characters.append("_")
        else:
            characters.append(character)
    stem = re.sub(r"_+", "_", "".join(characters)).strip(" ._")
    if not stem:
        stem = "csbox-project"
    candidate = f"{stem}-submission"
    return _bounded_component(candidate, normalized)


def _safe_pack_filename(value: str) -> str | None:
    normalized = unicodedata.normalize("NFKC", value)
    sanitized = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", normalized).strip(" .")
    sanitized = re.sub(r"_+", "_", sanitized)
    if not sanitized or sanitized in {".", ".."}:
        return None
    stem = sanitized.split(".", maxsplit=1)[0].upper()
    if stem in _WINDOWS_RESERVED_NAMES:
        sanitized = f"_{sanitized}"
    if not sanitized.casefold().endswith(".zip"):
        sanitized += ".zip"
    try:
        _validate_output_component(sanitized)
    except (TypeError, UnicodeError, ValueError):
        return None
    return sanitized


def _validate_output_component(value: str) -> None:
    normalized = validate_portable_relative_path(value)
    if len(normalized.parts) != 1 or normalized.as_posix() != value:
        raise ValueError("output component is not safe")


def _bounded_component(value: str, source: str) -> str:
    try:
        _validate_output_component(value)
    except (TypeError, UnicodeError, ValueError):
        value = _DEFAULT_DESTINATION_FALLBACK
    if (
        len(value.encode("utf-8")) <= _OUTPUT_COMPONENT_BUDGET
        and _windows_units(value) <= _OUTPUT_COMPONENT_BUDGET
    ):
        return value
    suffix = f"-{hashlib.sha256(source.encode('utf-8')).hexdigest()[:8]}"
    budget_bytes = _OUTPUT_COMPONENT_BUDGET - len(suffix.encode("utf-8"))
    budget_units = _OUTPUT_COMPONENT_BUDGET - _windows_units(suffix)
    prefix: list[str] = []
    byte_count = 0
    unit_count = 0
    for character in value:
        character_bytes = len(character.encode("utf-8"))
        character_units = _windows_units(character)
        if (
            byte_count + character_bytes > budget_bytes
            or unit_count + character_units > budget_units
        ):
            break
        prefix.append(character)
        byte_count += character_bytes
        unit_count += character_units
    result = "".join(prefix).rstrip(" ._") + suffix
    try:
        _validate_output_component(result)
    except (TypeError, UnicodeError, ValueError):
        return _DEFAULT_DESTINATION_FALLBACK
    return result


def _windows_units(value: str) -> int:
    return len(value.encode("utf-16-le")) // 2


def _path_identity(path: Path) -> str:
    value = os.path.normcase(os.path.abspath(os.fspath(path)))
    return unicodedata.normalize("NFC", value).casefold()


def _fingerprint(domain: str, payload: object) -> str:
    envelope = {
        "fingerprint_version": _FINGERPRINT_VERSION,
        "domain": f"csbox.submission.{domain}.v{_FINGERPRINT_VERSION}",
        "payload": _json_ready(payload),
    }
    return hashlib.sha256(_strict_json(envelope)).hexdigest()


def _strict_json(value: object) -> bytes:
    return json.dumps(
        _json_ready(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _json_ready(value: object, *, _depth: int = 0) -> object:
    if _depth > 64:
        return {"type": "depth-limit"}
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        if value != value or value in {float("inf"), float("-inf")}:
            return {"type": "non-finite"}
        return value
    if isinstance(value, Path):
        return value.as_posix()
    if isinstance(value, Enum):
        return _json_ready(value.value, _depth=_depth + 1)
    if isinstance(value, BaseModel):
        try:
            return _json_ready(value.model_dump(mode="json"), _depth=_depth + 1)
        except Exception:
            return {"type": type(value).__qualname__}
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        try:
            return _json_ready(model_dump(mode="json"), _depth=_depth + 1)
        except Exception:
            return {"type": type(value).__qualname__}
    if dataclasses.is_dataclass(value):
        try:
            return _json_ready(dataclasses.asdict(value), _depth=_depth + 1)
        except Exception:
            return {"type": type(value).__qualname__}
    if isinstance(value, Mapping):
        return {str(key): _json_ready(item, _depth=_depth + 1) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        values = [_json_ready(item, _depth=_depth + 1) for item in value]
        if isinstance(value, (set, frozenset)):
            values.sort(key=lambda item: json.dumps(item, ensure_ascii=False, sort_keys=True))
        return values
    if hasattr(value, "__dict__"):
        try:
            return _json_ready(vars(value), _depth=_depth + 1)
        except (TypeError, ValueError):
            pass
    return {"type": f"{type(value).__module__}.{type(value).__qualname__}"}


def _append_unique(target: list[str], values: tuple[str, ...] | list[str]) -> None:
    for value in values:
        if value not in target:
            target.append(value)


def _deduplicate_text(values: list[str]) -> list[str]:
    result: list[str] = []
    _append_unique(result, tuple(values))
    return result


__all__ = ["SubmissionService", "create_submission_service"]
