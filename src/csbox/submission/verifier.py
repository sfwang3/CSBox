from __future__ import annotations

import hashlib
import hmac
import json
import os
import stat
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError

from csbox.core.safe_paths import (
    is_reparse_metadata,
    open_regular_binary,
    read_regular_bytes,
    validate_portable_relative_path,
)
from csbox.pack.verifier import PackArchiveVerifier
from csbox.submission.models import (
    SubmissionManifest,
    SubmissionManifestFile,
    SubmissionVerificationError,
    SubmissionVerifyResult,
    manifest_digest,
)

_MANIFEST_NAME = "submission-manifest.json"
_MANIFEST_NAME_IDENTITY = unicodedata.normalize("NFC", _MANIFEST_NAME).casefold()

# A copied handoff is intentionally much smaller than the Pack archive limits:
# only two artifacts and the outer manifest may survive the final-folder check.
_MAX_MANIFEST_BYTES = 8 * 1024 * 1024
_MAX_FILES = 1024
_MAX_SUBMISSION_FILES = _MAX_FILES
_MAX_PATH_DEPTH = 1
_MAX_NAME_BYTES = 255
_MAX_TOTAL_ARTIFACT_BYTES = 2 * 1024 * 1024 * 1024
_VERIFY_CHUNK_BYTES = 1024 * 1024


@dataclass(frozen=True, slots=True)
class _RegularEntry:
    name: str
    path: Path


class _DuplicateJsonKey(ValueError):
    pass


class SubmissionVerifier:
    """Verify a prepared Submission directory using only its final contents."""

    def __init__(self, pack_verifier: object = PackArchiveVerifier) -> None:
        self._pack_verifier = pack_verifier

    def verify(self, submission_dir: Path | str) -> SubmissionVerifyResult:
        """Return a safe PASS/FAIL result without exposing input or parser details."""

        try:
            return self._verify(submission_dir)
        except SubmissionVerificationError as error:
            return _failed_result(error)
        except Exception:
            # The public boundary deliberately hides OS, JSON, Pydantic, ZIP,
            # and injected-dependency details, including any input paths.
            return _failed_result(SubmissionVerificationError())

    def _verify(self, submission_dir: Path | str) -> SubmissionVerifyResult:
        root = _require_directory(submission_dir)
        entries = _enumerate_direct_regular_files(root)
        manifest_entry = entries.get(_MANIFEST_NAME)
        if manifest_entry is None:
            _reject("manifest_missing")

        manifest = _read_manifest(manifest_entry.path)
        if not hmac.compare_digest(manifest.manifest_sha256, manifest_digest(manifest)):
            _reject("manifest_digest_mismatch")

        _validate_final_manifest(manifest)
        expected_names = {file.path for file in manifest.files} | {_MANIFEST_NAME}
        if len(expected_names) != 3 or set(entries) != expected_names:
            _reject("layout_invalid")

        total_artifact_bytes = 0
        archive_record: SubmissionManifestFile | None = None
        for record in manifest.files:
            entry = entries.get(record.path)
            if entry is None:
                _reject("layout_invalid")
            total_artifact_bytes = _verify_artifact(
                entry.path,
                record,
                total_bytes=total_artifact_bytes,
            )
            if record.role.value == "project_zip":
                archive_record = record

        if archive_record is None:
            _reject("layout_invalid")
        archive_entry = entries.get(archive_record.path)
        if archive_entry is None:
            _reject("layout_invalid")

        try:
            nested = self._pack_verifier.verify(
                archive_entry.path,
                require_manifest=True,
            )
            nested_verified = nested.verified
            nested_contains_manifest = nested.contains_manifest
        except Exception:
            raise SubmissionVerificationError("archive_invalid") from None

        if type(nested_verified) is not bool or type(nested_contains_manifest) is not bool:
            _reject("archive_invalid")
        if (
            manifest.project_archive.verified != nested_verified
            or manifest.project_archive.contains_manifest != nested_contains_manifest
        ):
            _reject("archive_summary_mismatch")
        if nested_verified is not True or nested_contains_manifest is not True:
            _reject("archive_invalid")

        return SubmissionVerifyResult(
            status="PASS",
            verified=True,
            warning_count=manifest.check.warning_count,
            files=(*tuple(file.path for file in manifest.files), _MANIFEST_NAME),
        )


def _failed_result(error: SubmissionVerificationError) -> SubmissionVerifyResult:
    return SubmissionVerifyResult(
        status="FAIL",
        verified=False,
        errors=(error.user_message,),
    )


def _reject(kind: str) -> None:
    raise SubmissionVerificationError(kind)


def _require_directory(submission_dir: Path | str) -> Path:
    try:
        root = Path(submission_dir)
        metadata = root.lstat()
    except (OSError, TypeError, ValueError):
        _reject("invalid_directory")
    if is_reparse_metadata(metadata) or not stat.S_ISDIR(metadata.st_mode):
        _reject("invalid_directory")
    return root


def _enumerate_direct_regular_files(root: Path) -> dict[str, _RegularEntry]:
    entries: dict[str, _RegularEntry] = {}
    try:
        with os.scandir(root) as directory:
            for directory_entry in directory:
                if len(entries) >= min(_MAX_FILES, _MAX_SUBMISSION_FILES):
                    _reject("layout_invalid")
                name = directory_entry.name
                _validate_filesystem_name(name)
                metadata = directory_entry.stat(follow_symlinks=False)
                if is_reparse_metadata(metadata):
                    _reject("layout_invalid")
                if stat.S_ISDIR(metadata.st_mode):
                    # A v1 handoff is a flat final folder. Reject the directory
                    # before opening or traversing it, so nested links cannot be
                    # followed during an unnecessary recursive walk.
                    _reject("layout_invalid")
                if not stat.S_ISREG(metadata.st_mode):
                    _reject("layout_invalid")
                entries[name] = _RegularEntry(name=name, path=Path(directory_entry.path))
    except SubmissionVerificationError:
        raise
    except (OSError, UnicodeError):
        raise SubmissionVerificationError("layout_invalid") from None
    return entries


def _validate_filesystem_name(name: object) -> None:
    if not isinstance(name, str):
        _reject("layout_invalid")
    try:
        if len(name.encode("utf-8")) > _MAX_NAME_BYTES:
            _reject("layout_invalid")
        normalized = validate_portable_relative_path(name)
    except (TypeError, UnicodeError, ValueError):
        _reject("layout_invalid")
    if (
        normalized.as_posix() != name
        or len(normalized.parts) > _MAX_PATH_DEPTH
        or len(normalized.parts) != 1
    ):
        _reject("layout_invalid")


def _read_manifest(path: Path) -> SubmissionManifest:
    try:
        metadata = path.lstat()
    except OSError:
        _reject("manifest_invalid")
    if is_reparse_metadata(metadata) or not stat.S_ISREG(metadata.st_mode):
        _reject("manifest_invalid")
    if metadata.st_size > _MAX_MANIFEST_BYTES:
        _reject("manifest_too_large")
    try:
        raw = read_regular_bytes(path, max_bytes=_MAX_MANIFEST_BYTES)
        text = raw.decode("utf-8")
        parsed = json.loads(
            text,
            object_pairs_hook=_object_pairs_without_duplicates,
            parse_constant=_reject_json_constant,
        )
    except _DuplicateJsonKey:
        _reject("manifest_invalid")
    except (UnicodeError, ValueError, RecursionError):
        _reject("manifest_invalid")
    except OSError:
        _reject("manifest_invalid")
    if not isinstance(parsed, dict):
        _reject("manifest_invalid")
    try:
        return SubmissionManifest.model_validate(parsed, strict=True)
    except (ValidationError, TypeError, ValueError, RecursionError):
        _reject("manifest_invalid")


def _object_pairs_without_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonKey(key)
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    del value
    raise ValueError("non-finite JSON number")


def _validate_final_manifest(manifest: SubmissionManifest) -> None:
    for record in manifest.files:
        try:
            normalized = validate_portable_relative_path(record.path)
        except (TypeError, UnicodeError, ValueError):
            _reject("layout_invalid")
        if (
            normalized.as_posix() != record.path
            or len(normalized.parts) > _MAX_PATH_DEPTH
            or len(normalized.parts) != 1
            or unicodedata.normalize("NFC", normalized.as_posix()).casefold()
            == _MANIFEST_NAME_IDENTITY
        ):
            _reject("layout_invalid")


def _verify_artifact(
    path: Path,
    record: SubmissionManifestFile,
    *,
    total_bytes: int,
) -> int:
    if record.size > _MAX_TOTAL_ARTIFACT_BYTES:
        _reject("artifact_invalid")
    if total_bytes > _MAX_TOTAL_ARTIFACT_BYTES - record.size:
        _reject("artifact_invalid")
    try:
        with open_regular_binary(path) as stream:
            metadata = os.fstat(stream.fileno())
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_size != record.size:
                _reject("artifact_invalid")
            digest = hashlib.sha256()
            actual_size = 0
            while chunk := stream.read(_VERIFY_CHUNK_BYTES):
                actual_size += len(chunk)
                if actual_size > record.size:
                    _reject("artifact_invalid")
                digest.update(chunk)
            final_metadata = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(final_metadata.st_mode)
                or final_metadata.st_size != record.size
                or actual_size != record.size
                or not hmac.compare_digest(digest.hexdigest(), record.sha256)
            ):
                _reject("artifact_invalid")
    except SubmissionVerificationError:
        raise
    except (OSError, ValueError):
        raise SubmissionVerificationError("artifact_invalid") from None
    return total_bytes + record.size


__all__ = ["SubmissionVerificationError", "SubmissionVerifier"]
