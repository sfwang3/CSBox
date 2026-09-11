from __future__ import annotations

import json
import shutil
import stat
import zipfile
from pathlib import Path

import pytest

import csbox.pack.verifier as verifier_module
from csbox.pack import PackArchiveVerification, PackArchiveVerifier
from csbox.pack.service import PackService, PackServiceError


def _write_zip(path: Path, entries: list[tuple[str, bytes, zipfile.ZipInfo | None]]) -> None:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data, info in entries:
            archive.writestr(info or name, data)


def _mark_first_entry_encrypted(path: Path) -> None:
    data = bytearray(path.read_bytes())
    for signature, flags_offset in ((b"PK\x03\x04", 6), (b"PK\x01\x02", 8)):
        offset = data.index(signature)
        flags = int.from_bytes(data[offset + flags_offset : offset + flags_offset + 2], "little")
        data[offset + flags_offset : offset + flags_offset + 2] = (flags | 0x1).to_bytes(
            2, "little"
        )
    path.write_bytes(data)


def _generated_archive(tmp_path: Path) -> tuple[Path, Path]:
    source = tmp_path / "源项目"
    source.mkdir()
    (source / "README.md").write_text("# demo\n", encoding="utf-8")
    (source / "src").mkdir()
    (source / "src" / "main.py").write_text("print('ok')\n", encoding="utf-8")
    archive = tmp_path / "交付.zip"
    PackService().pack(source, destination=archive, include_manifest=True, verify=True)
    return source, archive


def _rewrite_archive(
    original: Path,
    replacement: Path,
    *,
    replace_manifest: dict[str, object] | None = None,
    replace_manifest_bytes: bytes | None = None,
    replace_name: str | None = None,
    replacement_bytes: bytes = b"changed\n",
) -> None:
    with (
        zipfile.ZipFile(original) as source,
        zipfile.ZipFile(replacement, "w", compression=zipfile.ZIP_DEFLATED) as target,
    ):
        for info in source.infolist():
            data = None
            if info.filename == "manifest.json" and replace_manifest_bytes is not None:
                data = replace_manifest_bytes
            elif info.filename == "manifest.json" and replace_manifest is not None:
                data = (json.dumps(replace_manifest, ensure_ascii=False) + "\n").encode("utf-8")
            elif info.filename == replace_name:
                data = replacement_bytes
            if data is None:
                with source.open(info) as input_file, target.open(info, "w") as output_file:
                    shutil.copyfileobj(input_file, output_file, length=1024)
            else:
                target.writestr(info, data)


def _reorder_manifest_first(original: Path, replacement: Path) -> None:
    with (
        zipfile.ZipFile(original) as source,
        zipfile.ZipFile(replacement, "w", compression=zipfile.ZIP_DEFLATED) as target,
    ):
        infos = source.infolist()
        ordered = [next(info for info in infos if info.filename == "manifest.json")]
        ordered.extend(info for info in infos if info.filename != "manifest.json")
        for info in ordered:
            with source.open(info) as input_file, target.open(info, "w") as output_file:
                shutil.copyfileobj(input_file, output_file, length=1024)


def _assert_verify_failed(path: Path, *, require_manifest: bool = False) -> None:
    with pytest.raises(PackServiceError) as caught:
        PackArchiveVerifier.verify(path, require_manifest=require_manifest)
    assert caught.value.kind == "verify_failed"
    assert str(path) not in str(caught.value)


def test_generated_pack_archive_verifies_without_original_source_tree(tmp_path: Path) -> None:
    source, archive = _generated_archive(tmp_path)

    shutil.rmtree(source)
    result = PackArchiveVerifier.verify(archive, require_manifest=True)

    assert isinstance(result, PackArchiveVerification)
    assert result.verified is True
    assert result.contains_manifest is True
    assert result.entries == ("README.md", "src/main.py", "manifest.json")
    assert result.archive_bytes == archive.stat().st_size
    assert PackService().verify_existing(archive, require_manifest=True) == result


def test_archive_without_manifest_is_allowed_unless_required(tmp_path: Path) -> None:
    archive = tmp_path / "plain.zip"
    _write_zip(archive, [("main.py", b"print('ok')\n", None)])

    result = PackArchiveVerifier.verify(archive)

    assert result.verified is True
    assert result.contains_manifest is False
    assert result.entries == ("main.py",)

    _assert_verify_failed(archive, require_manifest=True)


def test_empty_archive_is_rejected_even_when_manifest_is_optional(tmp_path: Path) -> None:
    archive = tmp_path / "empty.zip"
    _write_zip(archive, [])

    _assert_verify_failed(archive)


def test_valid_present_manifest_verifies_with_default_options(tmp_path: Path) -> None:
    _source, archive = _generated_archive(tmp_path)

    result = PackArchiveVerifier.verify(archive)

    assert result.verified is True
    assert result.contains_manifest is True


def test_malformed_present_manifest_fails_with_default_options(tmp_path: Path) -> None:
    _source, original = _generated_archive(tmp_path)
    modified = tmp_path / "malformed-manifest.zip"
    _rewrite_archive(original, modified, replace_manifest_bytes=b"{not-json\n")

    _assert_verify_failed(modified)


def test_unknown_manifest_key_fails_with_default_options(tmp_path: Path) -> None:
    _source, original = _generated_archive(tmp_path)
    with zipfile.ZipFile(original) as archive:
        manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
    manifest["unexpected"] = "not allowed"
    modified = tmp_path / "unknown-manifest-key.zip"
    _rewrite_archive(original, modified, replace_manifest=manifest)

    _assert_verify_failed(modified)


def test_duplicate_manifest_key_fails_with_default_options(tmp_path: Path) -> None:
    _source, original = _generated_archive(tmp_path)
    modified = tmp_path / "duplicate-manifest-key.zip"
    _rewrite_archive(
        original,
        modified,
        replace_manifest_bytes=b'{"schema_version": 1, "schema_version": 1}\n',
    )

    _assert_verify_failed(modified)


@pytest.mark.parametrize("require_manifest", [False, True])
def test_modified_entry_bytes_fail_against_internal_manifest(
    tmp_path: Path, require_manifest: bool
) -> None:
    _source, original = _generated_archive(tmp_path)
    modified = tmp_path / "modified.zip"
    _rewrite_archive(original, modified, replace_name="README.md")

    _assert_verify_failed(modified, require_manifest=require_manifest)


@pytest.mark.parametrize(
    ("field", "value"),
    [("size", 999), ("sha256", "0" * 64)],
)
def test_wrong_internal_manifest_file_record_fails(
    tmp_path: Path, field: str, value: object
) -> None:
    _source, original = _generated_archive(tmp_path)
    with zipfile.ZipFile(original) as archive:
        manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
    manifest["files"][0][field] = value
    modified = tmp_path / f"wrong-{field}.zip"
    _rewrite_archive(original, modified, replace_manifest=manifest)

    _assert_verify_failed(modified, require_manifest=True)


@pytest.mark.parametrize("name", ["../escape.txt", "/absolute.txt", r"dir\\file.txt"])
def test_unsafe_zip_names_fail_closed(tmp_path: Path, name: str) -> None:
    archive = tmp_path / "unsafe-name.zip"
    _write_zip(archive, [(name, b"x", None)])

    _assert_verify_failed(archive)


@pytest.mark.parametrize(
    "names",
    [
        ["same.txt", "same.txt"],
        ["Case.txt", "case.txt"],
        ["é.txt", "e\u0301.txt"],
        ["folder", "folder/file.txt"],
    ],
)
def test_duplicate_case_nfc_and_ancestor_names_fail(tmp_path: Path, names: list[str]) -> None:
    archive = tmp_path / "conflict.zip"
    if names == ["same.txt", "same.txt"]:
        with pytest.warns(UserWarning, match=r"Duplicate name: 'same\.txt'"):
            _write_zip(archive, [(name, b"x", None) for name in names])
    else:
        _write_zip(archive, [(name, b"x", None) for name in names])

    _assert_verify_failed(archive)


def test_symlink_like_zip_metadata_is_rejected(tmp_path: Path) -> None:
    archive = tmp_path / "symlink.zip"
    info = zipfile.ZipInfo("link.txt")
    info.create_system = 3
    info.external_attr = (stat.S_IFLNK | 0o777) << 16
    _write_zip(archive, [("link.txt", b"outside.txt", info)])

    _assert_verify_failed(archive)


def test_unknown_zip_creator_metadata_is_rejected(tmp_path: Path) -> None:
    archive = tmp_path / "unknown-metadata.zip"
    info = zipfile.ZipInfo("file.txt")
    info.create_system = 99
    _write_zip(archive, [("file.txt", b"x", info)])

    _assert_verify_failed(archive)


def test_encrypted_zip_entry_is_rejected(tmp_path: Path) -> None:
    archive = tmp_path / "encrypted.zip"
    _write_zip(archive, [("secret.txt", b"secret", None)])
    _mark_first_entry_encrypted(archive)

    _assert_verify_failed(archive)


def test_directory_like_zip_metadata_is_rejected_even_without_a_trailing_slash(
    tmp_path: Path,
) -> None:
    archive = tmp_path / "directory-metadata.zip"
    info = zipfile.ZipInfo("directory")
    info.external_attr = 0x10
    _write_zip(archive, [("directory", b"x", info)])

    _assert_verify_failed(archive)


def test_oversized_manifest_is_rejected_before_unbounded_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(verifier_module, "_MAX_MANIFEST_BYTES", 64)
    archive = tmp_path / "large-manifest.zip"
    _write_zip(archive, [("manifest.json", b"{" + b"x" * 128, None)])

    _assert_verify_failed(archive, require_manifest=True)


def test_too_many_entries_are_rejected_before_hashing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(verifier_module, "_MAX_ZIP_ENTRIES", 2)
    archive = tmp_path / "many-entries.zip"
    _write_zip(archive, [(f"file-{index}.txt", b"x", None) for index in range(3)])

    _assert_verify_failed(archive)


def test_archive_byte_bound_is_checked_on_the_open_regular_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = tmp_path / "archive-size.zip"
    _write_zip(archive, [("file.txt", b"x", None)])
    monkeypatch.setattr(verifier_module, "_MAX_ARCHIVE_BYTES", archive.stat().st_size - 1)

    _assert_verify_failed(archive)


def test_total_uncompressed_bound_is_checked_before_entry_streaming(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(verifier_module, "_MAX_TOTAL_UNCOMPRESSED_BYTES", 1)
    archive = tmp_path / "uncompressed-size.zip"
    _write_zip(archive, [("file.txt", b"xx", None)])

    _assert_verify_failed(archive)


def test_compression_ratio_bound_is_checked_before_entry_streaming(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(verifier_module, "_MAX_COMPRESSION_RATIO", 1)
    archive = tmp_path / "compression-ratio.zip"
    _write_zip(archive, [("file.txt", b"x" * 128, None)])

    _assert_verify_failed(archive)


@pytest.mark.parametrize(
    ("name", "attribute", "limit"),
    [
        ("a/a/a/a.txt", "_MAX_ZIP_PATH_DEPTH", 3),
        ("long-name.txt", "_MAX_ZIP_NAME_BYTES", 8),
    ],
)
def test_deep_or_long_zip_names_are_bounded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    attribute: str,
    limit: int,
) -> None:
    monkeypatch.setattr(verifier_module, attribute, limit)
    archive = tmp_path / "bounded-name.zip"
    _write_zip(archive, [(name, b"x", None)])

    _assert_verify_failed(archive)


def test_manifest_not_requested_semantics_remain_valid(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "main.py").write_text("print('ok')\n", encoding="utf-8")
    archive = tmp_path / "not-requested.zip"
    PackService().pack(source, destination=archive, include_manifest=True)

    result = PackArchiveVerifier.verify(archive, require_manifest=True)

    assert result.verified is True
    assert result.contains_manifest is True


def test_required_manifest_must_remain_the_final_pack_entry(tmp_path: Path) -> None:
    _source, original = _generated_archive(tmp_path)
    reordered = tmp_path / "manifest-first.zip"
    _reorder_manifest_first(original, reordered)

    _assert_verify_failed(reordered, require_manifest=True)


def test_verifier_rejects_non_regular_archive_input(tmp_path: Path) -> None:
    source, archive = _generated_archive(tmp_path)
    del source
    link = tmp_path / "archive-link.zip"
    try:
        link.symlink_to(archive)
    except OSError:
        pytest.skip("symlink creation is unavailable")

    _assert_verify_failed(link)


def test_public_verifier_hashes_entries_in_bounded_chunks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _source, archive = _generated_archive(tmp_path)
    read_sizes: list[int] = []
    original_read = zipfile.ZipExtFile.read

    def bounded_read(self: object, size: int = -1) -> bytes:
        read_sizes.append(size)
        return original_read(self, size)

    monkeypatch.setattr(zipfile.ZipExtFile, "read", bounded_read)
    result = PackArchiveVerifier.verify(archive, require_manifest=True)

    assert result.verified is True
    assert read_sizes
    assert all(size == verifier_module._VERIFY_CHUNK_BYTES for size in read_sizes)
