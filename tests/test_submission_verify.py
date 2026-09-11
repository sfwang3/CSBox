from __future__ import annotations

import hashlib
import json
import os
import shutil
import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

import csbox.submission.verifier as verifier_module
from csbox.pack import PackArchiveVerification, PackArchiveVerifier, PackService
from csbox.submission import (
    SubmissionManifest,
    SubmissionVerificationError,
    SubmissionVerifier,
    manifest_digest,
    serialize_manifest,
)

_MANIFEST_NAME = "submission-manifest.json"
_REPORT_NAME = "报告-实验1.docx"
_ARCHIVE_NAME = "项目-1.zip"


def _write_outer_manifest(
    handoff: Path,
    *,
    mutate: Callable[[dict[str, Any]], None] | None = None,
) -> None:
    report = handoff / _REPORT_NAME
    archive = handoff / _ARCHIVE_NAME
    payload: dict[str, Any] = {
        "schema_version": 1,
        "csbox_version": "0.7.0.dev0",
        "generated_at": "2026-09-11T12:00:00Z",
        "evidence_set_id": "set-1",
        "check": {
            "status": "WARN",
            "warning_count": 2,
            "build_requested": False,
            "deep_requested": False,
        },
        "project_archive": {"verified": True, "contains_manifest": True},
        "files": [
            {
                "role": "report_docx",
                "path": report.name,
                "size": report.stat().st_size,
                "sha256": hashlib.sha256(report.read_bytes()).hexdigest(),
            },
            {
                "role": "project_zip",
                "path": archive.name,
                "size": archive.stat().st_size,
                "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
            },
        ],
        "manifest_sha256": "0" * 64,
    }
    if mutate is not None:
        mutate(payload)
    manifest = SubmissionManifest.model_validate(payload, strict=True)
    payload["manifest_sha256"] = manifest_digest(manifest)
    handoff.joinpath(_MANIFEST_NAME).write_text(
        serialize_manifest(SubmissionManifest.model_validate(payload, strict=True)),
        encoding="utf-8",
    )


@pytest.fixture
def valid_handoff(tmp_path: Path) -> Path:
    source = tmp_path / "原始项目"
    source.mkdir()
    source.joinpath("README.md").write_text("# handoff\n", encoding="utf-8")
    source.joinpath("src").mkdir()
    source.joinpath("src", "main.py").write_text("print('ok')\n", encoding="utf-8")

    built_archive = tmp_path / "build" / _ARCHIVE_NAME
    built_archive.parent.mkdir()
    PackService().pack(
        source,
        destination=built_archive,
        include_manifest=True,
        verify=True,
    )

    handoff = tmp_path / "copied-handoff"
    handoff.mkdir()
    handoff.joinpath(_REPORT_NAME).write_bytes(b"PK\x03\x04CSBox report\n")
    shutil.copy2(built_archive, handoff / _ARCHIVE_NAME)
    _write_outer_manifest(handoff)

    # The standalone verifier must only need the final copied directory.
    shutil.rmtree(source)
    shutil.rmtree(built_archive.parent)
    return handoff


def _rewrite_manifest(
    handoff: Path,
    mutate: Callable[[dict[str, Any]], None],
) -> None:
    payload = json.loads(handoff.joinpath(_MANIFEST_NAME).read_text(encoding="utf-8"))
    mutate(payload)
    payload["manifest_sha256"] = "0" * 64
    manifest = SubmissionManifest.model_validate(payload, strict=True)
    payload["manifest_sha256"] = manifest_digest(manifest)
    handoff.joinpath(_MANIFEST_NAME).write_text(
        serialize_manifest(SubmissionManifest.model_validate(payload, strict=True)),
        encoding="utf-8",
    )


def _recommit_raw_manifest(
    handoff: Path,
    mutate: Callable[[dict[str, Any]], None],
) -> None:
    """Recommit raw JSON so strict-model tests do not depend on a stale digest."""

    payload = json.loads(handoff.joinpath(_MANIFEST_NAME).read_text(encoding="utf-8"))
    mutate(payload)
    digest_payload = dict(payload)
    digest_payload["manifest_sha256"] = ""
    payload["manifest_sha256"] = hashlib.sha256(
        json.dumps(
            digest_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
    handoff.joinpath(_MANIFEST_NAME).write_text(
        json.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
    )


def _recommit_outer_manifest_for_current_archive(handoff: Path) -> None:
    archive = handoff / _ARCHIVE_NAME

    def refresh_archive_record(payload: dict[str, Any]) -> None:
        record = next(file for file in payload["files"] if file["role"] == "project_zip")
        record.update(
            {
                "size": archive.stat().st_size,
                "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
            }
        )

    _rewrite_manifest(handoff, refresh_archive_record)


def _assert_failed(result: object, *, forbidden: tuple[str, ...] = ()) -> None:
    assert result.status == "FAIL"  # type: ignore[union-attr]
    assert result.verified is False  # type: ignore[union-attr]
    assert result.errors  # type: ignore[union-attr]
    for message in result.errors:  # type: ignore[union-attr]
        assert all(value not in message for value in forbidden)


def test_copied_handoff_verifies_without_source_or_private_state(valid_handoff: Path) -> None:
    copied = valid_handoff.parent / "moved" / "提交材料"
    copied.parent.mkdir()
    shutil.copytree(valid_handoff, copied)

    result = SubmissionVerifier().verify(copied)

    assert result.status == "PASS"
    assert result.verified is True
    assert result.warning_count == 2
    assert result.files == (_REPORT_NAME, _ARCHIVE_NAME, _MANIFEST_NAME)
    assert result.errors == ()
    assert set(path.name for path in copied.iterdir()) == {
        _REPORT_NAME,
        _ARCHIVE_NAME,
        _MANIFEST_NAME,
    }
    assert not (copied / ".csbox").exists()


class _RecordingPackVerifier:
    calls: list[tuple[Path, bool]] = []

    @classmethod
    def verify(
        cls,
        path: Path | str,
        *,
        require_manifest: bool = False,
    ) -> PackArchiveVerification:
        cls.calls.append((Path(path), require_manifest))
        return PackArchiveVerification(verified=True, contains_manifest=True)


class _RecordingRealPackVerifier:
    calls: list[tuple[Path, bool]] = []

    @classmethod
    def verify(
        cls,
        path: Path | str,
        *,
        require_manifest: bool = False,
    ) -> PackArchiveVerification:
        cls.calls.append((Path(path), require_manifest))
        return PackArchiveVerifier.verify(path, require_manifest=require_manifest)


def test_verifier_injects_pack_dependency_and_requires_nested_manifest(
    valid_handoff: Path,
) -> None:
    _RecordingPackVerifier.calls = []

    result = SubmissionVerifier(pack_verifier=_RecordingPackVerifier).verify(valid_handoff)

    assert result.status == "PASS"
    assert _RecordingPackVerifier.calls == [
        (valid_handoff / _ARCHIVE_NAME, True),
    ]


@pytest.mark.parametrize(
    "mutation",
    [
        lambda handoff: (handoff / _REPORT_NAME).unlink(),
        lambda handoff: (handoff / _REPORT_NAME).write_bytes(b"modified report"),
        lambda handoff: (handoff / _ARCHIVE_NAME).write_bytes(b"modified zip"),
    ],
)
def test_modified_or_missing_outer_artifact_fails_closed(
    valid_handoff: Path,
    mutation: Callable[[Path], None],
) -> None:
    mutation(valid_handoff)

    result = SubmissionVerifier().verify(valid_handoff)

    _assert_failed(result, forbidden=(str(valid_handoff), "modified"))


@pytest.mark.parametrize(
    "mutate",
    [
        lambda payload: payload["files"][0].update({"size": 999}),
        lambda payload: payload["files"][1].update({"sha256": "0" * 64}),
    ],
)
def test_manifest_file_size_or_hash_mismatch_is_rejected(
    valid_handoff: Path,
    mutate: Callable[[dict[str, Any]], None],
) -> None:
    _rewrite_manifest(valid_handoff, mutate)

    result = SubmissionVerifier().verify(valid_handoff)

    _assert_failed(result, forbidden=(str(valid_handoff), _REPORT_NAME, _ARCHIVE_NAME))


def test_modified_manifest_commitment_is_rejected(valid_handoff: Path) -> None:
    manifest_path = valid_handoff / _MANIFEST_NAME
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["evidence_set_id"] = "set-2"
    manifest_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    result = SubmissionVerifier().verify(valid_handoff)

    _assert_failed(result, forbidden=(str(valid_handoff), "set-2"))


@pytest.mark.parametrize(
    "raw_manifest",
    [
        b"{not-json\n",
        b'{"schema_version": 1, "schema_version": 1}',
        b'{"schema_version": 1, "check": {"warning_count": NaN}}',
        b'{"schema_version": 1, "check": {"warning_count": Infinity}}',
    ],
)
def test_malformed_duplicate_and_nonfinite_json_is_rejected(
    valid_handoff: Path,
    raw_manifest: bytes,
) -> None:
    (valid_handoff / _MANIFEST_NAME).write_bytes(raw_manifest)

    result = SubmissionVerifier().verify(valid_handoff)

    _assert_failed(result, forbidden=(str(valid_handoff), "not-json", "NaN", "Infinity"))


def test_invalid_utf8_manifest_is_rejected(valid_handoff: Path) -> None:
    (valid_handoff / _MANIFEST_NAME).write_bytes(b"{\xff\n")

    result = SubmissionVerifier().verify(valid_handoff)

    _assert_failed(result, forbidden=(str(valid_handoff),))


def test_oversized_manifest_is_rejected_before_json_parsing(
    valid_handoff: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(verifier_module, "_MAX_MANIFEST_BYTES", 32)

    result = SubmissionVerifier().verify(valid_handoff)

    _assert_failed(result, forbidden=(str(valid_handoff),))


@pytest.mark.parametrize(
    "mutate",
    [
        lambda payload: payload.update({"unexpected": True}),
        lambda payload: payload.update({"schema_version": 2}),
        lambda payload: payload["files"][0].update({"size": "1"}),
    ],
)
def test_unknown_future_and_coercible_manifest_values_are_rejected(
    valid_handoff: Path,
    mutate: Callable[[dict[str, Any]], None],
) -> None:
    _recommit_raw_manifest(valid_handoff, mutate)

    result = SubmissionVerifier().verify(valid_handoff)

    _assert_failed(result, forbidden=(str(valid_handoff), "unexpected"))


@pytest.mark.parametrize(
    ("mutate", "recommit"),
    [
        (
            lambda payload: payload["files"][1].update({"path": payload["files"][0]["path"]}),
            False,
        ),
        (
            lambda payload: payload["files"][1].update({"path": _REPORT_NAME.upper()}),
            False,
        ),
        (
            lambda payload: payload["files"][0].update({"path": "nested/report.docx"}),
            True,
        ),
    ],
)
def test_duplicate_colliding_or_nested_artifact_paths_are_rejected(
    valid_handoff: Path,
    mutate: Callable[[dict[str, Any]], None],
    recommit: bool,
) -> None:
    if recommit:
        _rewrite_manifest(valid_handoff, mutate)
    else:
        payload = json.loads((valid_handoff / _MANIFEST_NAME).read_text(encoding="utf-8"))
        mutate(payload)
        # These two collisions are model-invalid, so no valid commitment is needed.
        (valid_handoff / _MANIFEST_NAME).write_text(
            json.dumps(payload, ensure_ascii=False),
            encoding="utf-8",
        )

    result = SubmissionVerifier().verify(valid_handoff)

    _assert_failed(result, forbidden=(str(valid_handoff), "nested"))


def test_artifact_path_casefold_colliding_with_manifest_name_is_rejected(
    valid_handoff: Path,
) -> None:
    colliding_name = "Submission-Manifest.json"
    (valid_handoff / colliding_name).write_bytes((valid_handoff / _REPORT_NAME).read_bytes())
    (valid_handoff / _REPORT_NAME).unlink()
    _rewrite_manifest(
        valid_handoff,
        lambda payload: payload["files"][0].update({"path": colliding_name}),
    )

    result = SubmissionVerifier().verify(valid_handoff)

    _assert_failed(result, forbidden=(str(valid_handoff), colliding_name))


def test_unexpected_file_and_nested_directory_are_rejected(valid_handoff: Path) -> None:
    (valid_handoff / "extra.txt").write_text("extra", encoding="utf-8")
    (valid_handoff / "nested").mkdir()

    result = SubmissionVerifier().verify(valid_handoff)

    _assert_failed(result, forbidden=(str(valid_handoff), "extra.txt"))


def test_symlinked_object_is_rejected_without_following_it(valid_handoff: Path) -> None:
    link = valid_handoff / "extra-link.txt"
    try:
        link.symlink_to(valid_handoff / _REPORT_NAME)
    except OSError:
        pytest.skip("symlink creation is unavailable")

    result = SubmissionVerifier().verify(valid_handoff)

    _assert_failed(result, forbidden=(str(valid_handoff), _REPORT_NAME))


def test_special_object_is_rejected_without_opening_it(valid_handoff: Path) -> None:
    if not hasattr(os, "mkfifo"):
        pytest.skip("FIFO creation is unavailable")
    fifo = valid_handoff / "extra.fifo"
    try:
        os.mkfifo(fifo)
    except OSError:
        pytest.skip("FIFO creation is unavailable")

    result = SubmissionVerifier().verify(valid_handoff)

    _assert_failed(result, forbidden=(str(valid_handoff), "extra.fifo"))


def test_symlinked_submission_directory_is_rejected(valid_handoff: Path) -> None:
    link = valid_handoff.parent / "handoff-link"
    try:
        link.symlink_to(valid_handoff, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation is unavailable")

    result = SubmissionVerifier().verify(link)

    _assert_failed(result, forbidden=(str(link),))


def test_manifest_symlink_is_rejected(valid_handoff: Path, tmp_path: Path) -> None:
    original = valid_handoff / _MANIFEST_NAME
    replacement = tmp_path / "manifest-copy.json"
    shutil.copy2(original, replacement)
    original.unlink()
    try:
        original.symlink_to(replacement)
    except OSError:
        pytest.skip("symlink creation is unavailable")

    result = SubmissionVerifier().verify(valid_handoff)

    _assert_failed(result, forbidden=(str(valid_handoff), str(replacement)))


def _rewrite_archive_entry(archive_path: Path) -> None:
    replacement = archive_path.with_name("rewritten.zip")
    with (
        zipfile.ZipFile(archive_path) as source,
        zipfile.ZipFile(
            replacement,
            "w",
            compression=zipfile.ZIP_DEFLATED,
        ) as target,
    ):
        for info in source.infolist():
            data = source.read(info)
            if info.filename == "README.md":
                data = b"tampered\n"
            target.writestr(info, data)
    archive_path.unlink()
    replacement.rename(archive_path)


def test_nested_pack_manifest_mismatch_is_rejected(valid_handoff: Path) -> None:
    archive_path = valid_handoff / _ARCHIVE_NAME
    _rewrite_archive_entry(archive_path)
    _recommit_outer_manifest_for_current_archive(valid_handoff)
    _RecordingRealPackVerifier.calls = []

    result = SubmissionVerifier(pack_verifier=_RecordingRealPackVerifier).verify(valid_handoff)

    _assert_failed(result, forbidden=(str(valid_handoff), "README.md", "tampered"))
    assert result.errors == ("项目 ZIP 校验失败。",)
    assert _RecordingRealPackVerifier.calls == [(archive_path, True)]


@pytest.mark.parametrize("unsafe_name", ["../escape.txt", "/absolute.txt", r"dir\\file.txt"])
def test_unsafe_nested_pack_entry_is_rejected(
    valid_handoff: Path,
    unsafe_name: str,
) -> None:
    archive_path = valid_handoff / _ARCHIVE_NAME
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(unsafe_name, b"x")
    _recommit_outer_manifest_for_current_archive(valid_handoff)
    _RecordingRealPackVerifier.calls = []

    result = SubmissionVerifier(pack_verifier=_RecordingRealPackVerifier).verify(valid_handoff)

    _assert_failed(result, forbidden=(str(valid_handoff), unsafe_name))
    assert result.errors == ("项目 ZIP 校验失败。",)
    assert _RecordingRealPackVerifier.calls == [(archive_path, True)]


@pytest.mark.parametrize(
    ("verified", "contains_manifest"),
    [(False, True), (True, False)],
)
def test_outer_archive_summary_must_match_nested_pack_result(
    valid_handoff: Path,
    verified: bool,
    contains_manifest: bool,
) -> None:
    def mutate(payload: dict[str, Any]) -> None:
        payload["project_archive"] = {
            "verified": verified,
            "contains_manifest": contains_manifest,
        }

    _rewrite_manifest(valid_handoff, mutate)

    result = SubmissionVerifier().verify(valid_handoff)

    _assert_failed(result, forbidden=(str(valid_handoff),))


def test_submission_verification_error_has_only_safe_public_fields() -> None:
    error = SubmissionVerificationError("manifest_invalid")

    assert error.kind == "manifest_invalid"
    assert error.user_message == "提交清单无效。"
    assert str(error) == error.user_message
