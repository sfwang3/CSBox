from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, field_validator, model_validator

from csbox.check.models import CheckStatus
from csbox.core.safe_paths import validate_portable_relative_path
from csbox.evidence.models import validate_identifier
from csbox.pack.models import PackPlan

_CANONICAL_UTC_TIMESTAMP = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{6})?Z"
)
_SHA256 = re.compile(r"[0-9a-f]{64}")


class SubmissionReadiness(StrEnum):
    """The derived state of a Submission preflight."""

    READY = "READY"
    WARNING = "WARNING"
    BLOCKED = "BLOCKED"


class SubmissionDestinationState(StrEnum):
    """Safe ownership state observed for a requested final directory."""

    MISSING = "missing"
    OWNED = "owned"
    UNOWNED = "unowned"
    UNSAFE = "unsafe"
    INVALID = "invalid"


class SubmissionArtifactRole(StrEnum):
    """The two artifact roles owned by a v1 Submission Manifest."""

    REPORT_DOCX = "report_docx"
    PROJECT_ZIP = "project_zip"


SubmissionReadinessValue: TypeAlias = SubmissionReadiness
SubmissionDestinationStateValue: TypeAlias = SubmissionDestinationState
SubmissionArtifactRoleValue: TypeAlias = SubmissionArtifactRole


def _required_text(value: object, *, field_name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be text")
    if not value.strip():
        raise ValueError(f"{field_name} must not be blank")
    return value


def _sha256(value: object, *, field_name: str = "sha256") -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be 64 lowercase hexadecimal characters")
    return value


def _canonical_timestamp(value: object, *, field_name: str) -> str:
    if not isinstance(value, str) or _CANONICAL_UTC_TIMESTAMP.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be a canonical UTC timestamp")
    from datetime import datetime

    try:
        datetime.strptime(
            value,
            "%Y-%m-%dT%H:%M:%S.%fZ" if "." in value else "%Y-%m-%dT%H:%M:%SZ",
        )
    except ValueError as error:
        raise ValueError(f"{field_name} must be a canonical UTC timestamp") from error
    return value


class _SubmissionModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class SubmissionCheckSummary(_SubmissionModel):
    """The safe, public summary of a project Check run."""

    status: CheckStatus
    warning_count: int = Field(ge=0)
    build_requested: bool = False
    deep_requested: bool = False
    deep_status: CheckStatus | None = None

    @field_validator("status", "deep_status", mode="before")
    @classmethod
    def _validate_status(cls, value: object) -> CheckStatus | None:
        if value is None:
            return None
        if isinstance(value, CheckStatus):
            return value
        if isinstance(value, str):
            try:
                return CheckStatus(value)
            except ValueError:
                pass
        raise ValueError("status must be a supported Check status")


class SubmissionArchiveSummary(_SubmissionModel):
    """The safe, public summary of the verified project archive."""

    verified: bool
    contains_manifest: bool


class SubmissionResolvedSource(_SubmissionModel):
    """A public, payload-free summary of one resolved Evidence source."""

    index: int = Field(ge=1)
    source_type: str
    available: bool
    fingerprint: str
    unavailable_reason: str | None = None

    @field_validator("source_type", mode="before")
    @classmethod
    def _validate_source_type(cls, value: object) -> str:
        return _required_text(value, field_name="source_type")

    @field_validator("fingerprint", mode="before")
    @classmethod
    def _validate_source_fingerprint(cls, value: object) -> str:
        return _sha256(value, field_name="fingerprint")


class SubmissionManifestFile(_SubmissionModel):
    """One file record committed by the outer Submission Manifest."""

    role: SubmissionArtifactRole
    path: str
    size: int = Field(ge=0)
    sha256: str

    @field_validator("role", mode="before")
    @classmethod
    def _validate_role(cls, value: object) -> SubmissionArtifactRole:
        if isinstance(value, SubmissionArtifactRole):
            return value
        if isinstance(value, str):
            try:
                return SubmissionArtifactRole(value)
            except ValueError:
                pass
        raise ValueError("role must be a supported Submission artifact role")

    @field_validator("path", mode="before")
    @classmethod
    def _validate_path(cls, value: object) -> str:
        return validate_portable_relative_path(value).as_posix()

    @field_validator("sha256", mode="before")
    @classmethod
    def _validate_sha256(cls, value: object) -> str:
        return _sha256(value)


class SubmissionManifest(_SubmissionModel):
    """Strict v1 receipt for one prepared Submission handoff."""

    schema_version: Literal[1]
    csbox_version: str
    generated_at: str
    evidence_set_id: str
    check: SubmissionCheckSummary
    project_archive: SubmissionArchiveSummary
    files: tuple[SubmissionManifestFile, ...]
    manifest_sha256: str

    @field_validator("csbox_version", mode="before")
    @classmethod
    def _validate_csbox_version(cls, value: object) -> str:
        return _required_text(value, field_name="csbox_version")

    @field_validator("generated_at", mode="before")
    @classmethod
    def _validate_generated_at(cls, value: object) -> str:
        return _canonical_timestamp(value, field_name="generated_at")

    @field_validator("evidence_set_id", mode="before")
    @classmethod
    def _validate_evidence_set_id(cls, value: object) -> str:
        return validate_identifier(value, field_name="evidence_set_id")

    @field_validator("files", mode="before")
    @classmethod
    def _normalize_files(cls, value: object) -> tuple[object, ...]:
        if isinstance(value, (list, tuple)):
            return tuple(value)
        raise ValueError("files must be a sequence")

    @field_validator("manifest_sha256", mode="before")
    @classmethod
    def _validate_manifest_sha256(cls, value: object) -> str:
        return _sha256(value, field_name="manifest_sha256")

    @model_validator(mode="after")
    def _validate_files(self) -> SubmissionManifest:
        expected_roles = {"report_docx", "project_zip"}
        roles = {file.role for file in self.files}
        if len(self.files) != 2 or roles != expected_roles:
            raise ValueError("files must contain exactly one report_docx and one project_zip")

        path_identities = {
            unicodedata.normalize("NFC", file.path).casefold() for file in self.files
        }
        if len(path_identities) != len(self.files):
            raise ValueError("files must not contain duplicate or colliding paths")
        role_order = {
            SubmissionArtifactRole.REPORT_DOCX: 0,
            SubmissionArtifactRole.PROJECT_ZIP: 1,
        }
        self.files = tuple(sorted(self.files, key=lambda file: role_order[file.role]))
        return self

    def manifest_payload_without_digest(self) -> dict[str, Any]:
        return manifest_payload_without_digest(self)

    def manifest_digest(self) -> str:
        return manifest_digest(self)

    def serialize(self) -> str:
        return serialize_manifest(self)


class SubmissionPlan(_SubmissionModel):
    """Read-only preflight data retained for a later preparation recheck."""

    evidence_set_id: str
    evidence_fingerprint: str
    report_profile_fingerprint: str
    pack_source_fingerprint: str
    destination: Path
    report_filename: str = "report.docx"
    archive_filename: str
    check: SubmissionCheckSummary
    project_archive: SubmissionArchiveSummary
    warnings: tuple[str, ...] = ()
    blockers: tuple[str, ...] = ()
    readiness: SubmissionReadiness = SubmissionReadiness.BLOCKED
    pack_plan: PackPlan | None = None
    fingerprint_version: Literal[1] = 1
    check_fingerprint: str = ""
    destination_fingerprint: str = ""
    pack_fingerprint: str = ""
    plan_fingerprint: str = ""
    destination_state: SubmissionDestinationState = SubmissionDestinationState.MISSING
    resolved_sources: tuple[SubmissionResolvedSource, ...] = ()

    # These attributes are deliberately private and are not included in any
    # Pydantic dump.  Later preparation can revalidate the same typed resolver
    # results instead of asking Report to resolve a second, different snapshot.
    _evidence_set: Any = PrivateAttr(default=None)
    _report_profile: Any = PrivateAttr(default=None)
    _resolved_values: tuple[Any, ...] = PrivateAttr(default=())
    _check_report: Any = PrivateAttr(default=None)
    _fingerprint_payloads: dict[str, Any] = PrivateAttr(default_factory=dict)
    _pack_plan: Any = PrivateAttr(default=None)

    @field_validator("pack_plan", mode="before")
    @classmethod
    def _validate_pack_plan(cls, value: object) -> PackPlan | None:
        if value is None or isinstance(value, PackPlan):
            return value
        return PackPlan.model_validate(value, strict=True)

    @field_validator("evidence_set_id", mode="before")
    @classmethod
    def _validate_plan_evidence_set_id(cls, value: object) -> str:
        return validate_identifier(value, field_name="evidence_set_id")

    @field_validator("readiness", mode="before")
    @classmethod
    def _validate_readiness(cls, value: object) -> SubmissionReadiness:
        if isinstance(value, SubmissionReadiness):
            return value
        if isinstance(value, str):
            try:
                return SubmissionReadiness(value)
            except ValueError:
                pass
        raise ValueError("readiness must be a supported Submission readiness")

    @field_validator("destination_state", mode="before")
    @classmethod
    def _validate_destination_state(cls, value: object) -> SubmissionDestinationState:
        if isinstance(value, SubmissionDestinationState):
            return value
        if isinstance(value, str):
            try:
                return SubmissionDestinationState(value)
            except ValueError:
                pass
        raise ValueError("destination_state must be a supported destination state")

    @field_validator("report_filename", "archive_filename", mode="before")
    @classmethod
    def _validate_output_filename(cls, value: object) -> str:
        if not isinstance(value, str):
            raise ValueError("output filename must be text")
        try:
            normalized = validate_portable_relative_path(value)
        except (TypeError, UnicodeError, ValueError) as error:
            raise ValueError("output filename must be a safe portable name") from error
        if normalized.as_posix() != value or len(normalized.parts) != 1:
            raise ValueError("output filename must be one safe portable name")
        return value

    @field_validator("resolved_sources", mode="before")
    @classmethod
    def _normalize_resolved_sources(cls, value: object) -> tuple[object, ...]:
        if isinstance(value, (list, tuple)):
            return tuple(value)
        raise ValueError("resolved_sources must be a sequence")


class SubmissionResult(_SubmissionModel):
    """Safe final paths and statuses returned after preparation."""

    destination: Path
    report_path: Path
    archive_path: Path
    manifest_path: Path
    check_status: CheckStatus
    warning_count: int = Field(default=0, ge=0)
    verified: bool
    warnings: tuple[str, ...] = ()

    @field_validator("check_status", mode="before")
    @classmethod
    def _validate_check_status(cls, value: object) -> CheckStatus:
        if isinstance(value, CheckStatus):
            return value
        if isinstance(value, str):
            try:
                return CheckStatus(value)
            except ValueError:
                pass
        raise ValueError("check_status must be a supported Check status")


class SubmissionVerifyResult(_SubmissionModel):
    """Safe result returned by standalone copied-folder verification."""

    status: Literal["PASS", "FAIL"]
    verified: bool
    warning_count: int = Field(default=0, ge=0)
    files: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()


class SubmissionError(RuntimeError):
    """A Submission operation failed without exposing private filesystem data."""

    def __init__(
        self,
        message: str,
        *,
        kind: str = "submission_failed",
        phase: str | None = None,
        details: tuple[str, ...] = (),
    ) -> None:
        super().__init__(message)
        self.user_message = message
        self.kind = kind
        self.phase = phase
        self.details = tuple(details)


_SUBMISSION_VERIFICATION_MESSAGES = {
    "verification_failed": "提交材料校验失败。",
    "invalid_directory": "提交目录无效。",
    "manifest_missing": "提交清单缺失。",
    "manifest_too_large": "提交清单超过大小上限。",
    "manifest_invalid": "提交清单无效。",
    "manifest_digest_mismatch": "提交清单校验失败。",
    "layout_invalid": "提交目录结构无效。",
    "artifact_invalid": "提交文件校验失败。",
    "archive_invalid": "项目 ZIP 校验失败。",
    "archive_summary_mismatch": "项目 ZIP 校验状态不一致。",
}


class SubmissionVerificationError(RuntimeError):
    """A copied Submission handoff failed a safe public verification rule."""

    def __init__(self, kind: str = "verification_failed") -> None:
        safe_kind = (
            kind
            if isinstance(kind, str) and kind in _SUBMISSION_VERIFICATION_MESSAGES
            else "verification_failed"
        )
        self.kind = safe_kind
        self.user_message = _SUBMISSION_VERIFICATION_MESSAGES[safe_kind]
        super().__init__(self.user_message)


def manifest_payload_without_digest(manifest: SubmissionManifest) -> dict[str, Any]:
    """Return JSON-compatible manifest data with its self-digest excluded."""

    if not isinstance(manifest, SubmissionManifest):
        raise TypeError("manifest must be a SubmissionManifest")
    payload = manifest.model_dump(mode="json")
    payload["manifest_sha256"] = ""
    return payload


def _canonical_manifest_json(payload: dict[str, Any]) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def manifest_digest(manifest: SubmissionManifest) -> str:
    """Return the SHA-256 of canonical UTF-8 JSON excluding the digest field."""

    payload = manifest_payload_without_digest(manifest)
    return hashlib.sha256(_canonical_manifest_json(payload)).hexdigest()


def serialize_manifest(manifest: SubmissionManifest) -> str:
    """Serialize a manifest as sorted, readable, newline-terminated UTF-8 JSON."""

    if not isinstance(manifest, SubmissionManifest):
        raise TypeError("manifest must be a SubmissionManifest")
    return (
        json.dumps(
            manifest.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            allow_nan=False,
        )
        + "\n"
    )


__all__ = [
    "SubmissionArchiveSummary",
    "SubmissionArtifactRole",
    "SubmissionArtifactRoleValue",
    "SubmissionCheckSummary",
    "SubmissionError",
    "SubmissionManifest",
    "SubmissionManifestFile",
    "SubmissionPlan",
    "SubmissionDestinationState",
    "SubmissionDestinationStateValue",
    "SubmissionReadiness",
    "SubmissionReadinessValue",
    "SubmissionResolvedSource",
    "SubmissionResult",
    "SubmissionVerificationError",
    "SubmissionVerifyResult",
    "manifest_digest",
    "manifest_payload_without_digest",
    "serialize_manifest",
    "validate_identifier",
    "validate_portable_relative_path",
]
