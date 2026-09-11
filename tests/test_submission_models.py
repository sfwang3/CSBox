from pathlib import Path, PurePosixPath

import pytest
from pydantic import ValidationError

from csbox.pack.models import PackPlan
from csbox.submission.models import (
    SubmissionManifest,
    SubmissionPlan,
    manifest_digest,
    serialize_manifest,
    validate_portable_relative_path,
)


def _manifest(files: tuple[dict[str, object], ...]) -> SubmissionManifest:
    return SubmissionManifest(
        schema_version=1,
        csbox_version="0.7.0",
        generated_at="2026-09-11T12:00:00Z",
        evidence_set_id="set-1",
        check={
            "status": "WARN",
            "warning_count": 2,
            "build_requested": False,
            "deep_requested": False,
        },
        project_archive={"verified": True, "contains_manifest": True},
        files=files,
        manifest_sha256="c" * 64,
    )


def _pack_plan_payload(*, source_bytes: object = 1) -> dict[str, object]:
    return {
        "source_root": Path("project"),
        "destination": Path("project.zip"),
        "output_filename": "project.zip",
        "project_type": "python",
        "package_manager": None,
        "included": ("README.md",),
        "excluded": (),
        "rejected": (),
        "warnings": (),
        "source_bytes": source_bytes,
        "include_manifest": False,
        "verification_requested": False,
        "force": False,
        "output_exists": False,
        "include": (),
        "exclude": (),
    }


def _submission_plan_kwargs(pack_plan: object) -> dict[str, object]:
    return {
        "evidence_set_id": "set-1",
        "evidence_fingerprint": "evidence-fingerprint",
        "report_profile_fingerprint": "report-fingerprint",
        "pack_source_fingerprint": "pack-fingerprint",
        "destination": Path("deliverables"),
        "archive_filename": "project.zip",
        "check": {
            "status": "WARN",
            "warning_count": 0,
            "build_requested": False,
            "deep_requested": False,
        },
        "project_archive": {"verified": True, "contains_manifest": True},
        "pack_plan": pack_plan,
    }


def test_submission_manifest_accepts_only_schema_v1_and_safe_artifacts() -> None:

    manifest = SubmissionManifest(
        schema_version=1,
        csbox_version="0.7.0",
        generated_at="2026-09-11T12:00:00Z",
        evidence_set_id="set-1",
        check={
            "status": "WARN",
            "warning_count": 2,
            "build_requested": False,
            "deep_requested": False,
        },
        project_archive={"verified": True, "contains_manifest": True},
        files=(
            {"role": "report_docx", "path": "report.docx", "size": 4, "sha256": "a" * 64},
            {"role": "project_zip", "path": "student.zip", "size": 8, "sha256": "b" * 64},
        ),
        manifest_sha256="c" * 64,
    )
    assert manifest.files[0].path == "report.docx"


@pytest.mark.parametrize(
    "path",
    (
        "folder/file<name.txt",
        "folder/file>name.txt",
        "folder/file:name.txt",
        'folder/file"name.txt',
        "folder/file//name.txt",
        r"folder/file\name.txt",
        "folder/file|name.txt",
        "folder/file?name.txt",
        "folder/file*name.txt",
    ),
)
def test_portable_relative_path_rejects_windows_forbidden_characters(path: str) -> None:
    with pytest.raises(ValueError):
        validate_portable_relative_path(path)


def test_portable_relative_path_preserves_normalized_cjk_components() -> None:
    path = "课程资料/实验结果.txt"

    assert validate_portable_relative_path(path) == PurePosixPath(path)


def test_submission_manifest_canonicalizes_file_order_for_digest_and_serialization() -> None:
    files = (
        {"role": "report_docx", "path": "报告.docx", "size": 4, "sha256": "a" * 64},
        {"role": "project_zip", "path": "项目.zip", "size": 8, "sha256": "b" * 64},
    )
    canonical = _manifest(files)
    reversed_input = _manifest(tuple(reversed(files)))

    assert reversed_input.files == canonical.files
    assert manifest_digest(reversed_input) == manifest_digest(canonical)
    assert serialize_manifest(reversed_input) == serialize_manifest(canonical)


def test_submission_plan_rejects_coercion_in_nested_pack_plan() -> None:
    payload = _pack_plan_payload(source_bytes="1")

    with pytest.raises(ValidationError):
        SubmissionPlan(**_submission_plan_kwargs(payload))


def test_submission_plan_accepts_live_pack_plan_and_private_fingerprints() -> None:
    pack_plan = PackPlan(**_pack_plan_payload())
    pack_plan._source_fingerprint = "source-fingerprint"
    pack_plan._source_file_fingerprints = (("README.md", "file-fingerprint"),)

    submission_plan = SubmissionPlan(**_submission_plan_kwargs(pack_plan))

    assert submission_plan.pack_plan is pack_plan
    assert submission_plan.pack_plan._source_fingerprint == "source-fingerprint"
    assert submission_plan.pack_plan._source_file_fingerprints == (
        ("README.md", "file-fingerprint"),
    )
