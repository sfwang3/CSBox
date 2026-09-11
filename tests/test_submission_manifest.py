from __future__ import annotations

import json
from copy import deepcopy

import pytest
from pydantic import ValidationError

from csbox.submission.models import SubmissionManifest


def _manifest_payload() -> dict[str, object]:
    return {
        "schema_version": 1,
        "csbox_version": "0.7.0rc1",
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
                "path": "report.docx",
                "size": 4,
                "sha256": "a" * 64,
            },
            {
                "role": "project_zip",
                "path": "student.zip",
                "size": 8,
                "sha256": "b" * 64,
            },
        ],
        "manifest_sha256": "c" * 64,
    }


def test_submission_and_pack_use_one_shared_portable_path_boundary() -> None:
    import csbox.core.safe_paths as safe_paths
    import csbox.pack.service as pack_service
    from csbox.submission import models as submission_models

    shared_validator = getattr(safe_paths, "validate_portable_relative_path", None)

    assert shared_validator is not None
    assert getattr(submission_models, "validate_portable_relative_path", None) is shared_validator
    assert getattr(pack_service, "validate_portable_relative_path", None) is shared_validator


@pytest.mark.parametrize(
    "path",
    (
        "../x",
        "/x",
        "C:/x",
        "C:x",
        r"a\b",
        "a//b",
        "./x",
        "x/./y",
        "CON.txt",
        "nested/PRN.log",
        "AUX.",
        "NUL ",
        "COM1.txt",
        "LPT9.data",
        "report.docx.",
        "report.docx ",
        "report\x00.docx",
        "report\x01.docx",
        "report\x1f.docx",
        "report\x7f.docx",
        "report<docx",
        "report>docx",
        "report:docx",
        'report"docx',
        "report|docx",
        "report?docx",
        "report*docx",
    ),
)
def test_manifest_rejects_non_portable_artifact_paths(path: str) -> None:
    payload = _manifest_payload()
    payload["files"][0]["path"] = path  # type: ignore[index]

    with pytest.raises(ValidationError):
        SubmissionManifest.model_validate(payload)


@pytest.mark.parametrize("path", ("报告.docx", "课程/实验1/结果-abc中文123.txt"))
def test_manifest_accepts_normalized_cjk_and_mixed_paths(path: str) -> None:
    payload = _manifest_payload()
    payload["files"][0]["path"] = path  # type: ignore[index]

    manifest = SubmissionManifest.model_validate(payload)

    assert manifest.files[0].path == path


def test_manifest_rejects_a_non_nfc_path_instead_of_silently_normalizing() -> None:
    payload = _manifest_payload()
    payload["files"][0]["path"] = "e\u0301.txt"  # type: ignore[index]

    with pytest.raises(ValidationError):
        SubmissionManifest.model_validate(payload)


@pytest.mark.parametrize(
    ("first_path", "second_path"),
    (
        ("report.docx", "report.docx"),
        ("report.docx", "REPORT.DOCX"),
        ("报告.docx", "报告.DOCX"),
    ),
)
def test_manifest_rejects_duplicate_or_casefold_colliding_paths(
    first_path: str, second_path: str
) -> None:
    payload = _manifest_payload()
    payload["files"][0]["path"] = first_path  # type: ignore[index]
    payload["files"][1]["path"] = second_path  # type: ignore[index]

    with pytest.raises(ValidationError):
        SubmissionManifest.model_validate(payload)


def test_manifest_rejects_duplicate_roles() -> None:
    payload = _manifest_payload()
    payload["files"][1]["role"] = "report_docx"  # type: ignore[index]

    with pytest.raises(ValidationError):
        SubmissionManifest.model_validate(payload)


@pytest.mark.parametrize(
    "mutate",
    (
        lambda payload: payload.update({"unexpected": True}),
        lambda payload: payload["check"].update({"unexpected": True}),  # type: ignore[union-attr]
        lambda payload: payload["files"][0].update({"unexpected": True}),  # type: ignore[index]
    ),
)
def test_manifest_rejects_unknown_fields_at_every_schema_level(mutate) -> None:
    payload = _manifest_payload()
    mutate(payload)

    with pytest.raises(ValidationError):
        SubmissionManifest.model_validate(payload)


def test_manifest_rejects_a_future_schema_version() -> None:
    payload = _manifest_payload()
    payload["schema_version"] = 2

    with pytest.raises(ValidationError):
        SubmissionManifest.model_validate(payload)


@pytest.mark.parametrize("size", (-1, True, 1.0, "1"))
def test_manifest_rejects_non_strict_or_invalid_file_sizes(size: object) -> None:
    payload = _manifest_payload()
    payload["files"][0]["size"] = size  # type: ignore[index]

    with pytest.raises(ValidationError):
        SubmissionManifest.model_validate(payload)


@pytest.mark.parametrize("sha256", ("", "A" * 64, "a" * 63, "a" * 65, b"a" * 64))
def test_manifest_rejects_invalid_file_hashes(sha256: object) -> None:
    payload = _manifest_payload()
    payload["files"][0]["sha256"] = sha256  # type: ignore[index]

    with pytest.raises(ValidationError):
        SubmissionManifest.model_validate(payload)


def test_manifest_rejects_invalid_utf8_json_bytes() -> None:
    payload = json.dumps(_manifest_payload(), ensure_ascii=False).encode("utf-8")
    invalid_utf8 = payload + b"\xff"

    with pytest.raises(ValidationError):
        SubmissionManifest.model_validate_json(invalid_utf8)


def test_manifest_keeps_canonical_artifact_role_order() -> None:
    payload = _manifest_payload()
    payload["files"] = list(reversed(payload["files"]))  # type: ignore[arg-type]

    manifest = SubmissionManifest.model_validate(deepcopy(payload))

    assert tuple(file.role.value for file in manifest.files) == ("report_docx", "project_zip")
