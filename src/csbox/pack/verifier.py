from __future__ import annotations

import hashlib
import json
import os
import stat
import unicodedata
import zipfile
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from csbox.core.safe_paths import open_regular_binary, validate_portable_relative_path
from csbox.pack.models import PackArchiveVerification

# These limits leave room for normal course projects while bounding the amount of
# archive metadata, decompressed data, and parser input accepted from an archive.
_MAX_ARCHIVE_BYTES = 512 * 1024 * 1024
_MAX_ZIP_ENTRIES = 100_000
_MAX_TOTAL_UNCOMPRESSED_BYTES = 2 * 1024 * 1024 * 1024
_MAX_COMPRESSION_RATIO = 10_000
_MAX_ZIP_PATH_DEPTH = 64
_MAX_ZIP_NAME_BYTES = 4096
_MAX_ZIP_COMPONENT_BYTES = 255
_MAX_MANIFEST_BYTES = 8 * 1024 * 1024
_VERIFY_CHUNK_BYTES = 1024 * 1024

_DOS_DIRECTORY = 0x10
_DOS_REPARSE_POINT = 0x400
_ZIP_ENCRYPTED = 0x1
_MANIFEST_KEYS = frozenset(
    {
        "schema_version",
        "csbox_version",
        "generated_at",
        "project_type",
        "package_manager",
        "source_bytes",
        "files",
        "excluded",
        "rejected",
        "verification",
    }
)
_VERIFICATION_KEYS = frozenset({"requested", "status"})
_FILE_RECORD_KEYS = frozenset({"path", "size", "sha256"})


class _ArchiveRejected(ValueError):
    """An archive failed a public, deliberately redacted verification rule."""


class _DuplicateJsonKey(ValueError):
    pass


class PackArchiveVerifier:
    """Verify a Pack ZIP from the archive itself, without its source tree."""

    @staticmethod
    def verify(
        path: Path | str,
        *,
        require_manifest: bool = False,
    ) -> PackArchiveVerification:
        return _verify_archive(path, require_manifest=require_manifest)


def _verify_archive(
    path: Path | str,
    *,
    require_manifest: bool = False,
    expected_entries: tuple[str, ...] | None = None,
    expected_manifest: dict[str, object] | None = None,
) -> PackArchiveVerification:
    """Run one verifier implementation for public and generated-archive checks."""

    try:
        result = _verify_archive_contents(
            path,
            require_manifest=require_manifest,
            expected_entries=expected_entries,
            expected_manifest=expected_manifest,
        )
    except _ArchiveRejected as error:
        raise _verification_error(str(error)) from None
    except Exception:
        # Do not carry paths, filenames, parser diagnostics, or library details
        # across this boundary.  Callers receive one stable safe error shape.
        raise _verification_error("ZIP 校验失败。") from None
    return result


def _verification_error(message: str) -> RuntimeError:
    # PackServiceError is intentionally imported lazily so the reusable verifier
    # can be imported while pack.service is still initializing.
    from csbox.pack.service import PackServiceError

    return PackServiceError(message, kind="verify_failed")


def _verify_archive_contents(
    path: Path | str,
    *,
    require_manifest: bool,
    expected_entries: tuple[str, ...] | None,
    expected_manifest: dict[str, object] | None,
) -> PackArchiveVerification:
    with open_regular_binary(path) as input_file:
        archive_bytes = _stream_size(input_file)
        if archive_bytes <= 0:
            raise _ArchiveRejected("ZIP 文件大小无效。")
        if archive_bytes > _MAX_ARCHIVE_BYTES:
            raise _ArchiveRejected("ZIP 文件超过校验大小上限。")

        with zipfile.ZipFile(input_file) as archive:
            infos = tuple(archive.infolist())
            if not infos:
                raise _ArchiveRejected("ZIP 文件不得为空。")
            if len(infos) > _MAX_ZIP_ENTRIES:
                raise _ArchiveRejected("ZIP 条目数量超过校验上限。")
            names = tuple(info.filename for info in infos)
            _validate_zip_infos(infos)
            if expected_entries is not None and names != expected_entries:
                raise _ArchiveRejected("ZIP 条目与打包计划不一致。")

            manifest_present = "manifest.json" in names
            if require_manifest and not manifest_present:
                raise _ArchiveRejected("ZIP 缺少必需的 manifest。")
            if manifest_present and names[-1] != "manifest.json":
                raise _ArchiveRejected("manifest 条目位置无效。")

            manifest_bytes = bytearray()
            records: dict[str, tuple[int, str]] = {}
            actual_total = 0
            for info in infos:
                if info.filename == "manifest.json" and info.file_size > _MAX_MANIFEST_BYTES:
                    raise _ArchiveRejected("manifest 超过校验大小上限。")
                digest = hashlib.sha256()
                actual_size = 0
                with archive.open(info, mode="r") as stream:
                    while chunk := stream.read(_VERIFY_CHUNK_BYTES):
                        actual_size += len(chunk)
                        actual_total += len(chunk)
                        if actual_total > _MAX_TOTAL_UNCOMPRESSED_BYTES:
                            raise _ArchiveRejected("ZIP 解压内容超过校验大小上限。")
                        digest.update(chunk)
                        if info.filename == "manifest.json":
                            if len(manifest_bytes) + len(chunk) > _MAX_MANIFEST_BYTES:
                                raise _ArchiveRejected("manifest 超过校验大小上限。")
                            manifest_bytes.extend(chunk)
                if actual_size != info.file_size:
                    raise _ArchiveRejected("ZIP 条目大小校验失败。")
                records[info.filename] = (actual_size, digest.hexdigest())

            if _stream_size(input_file) != archive_bytes:
                raise _ArchiveRejected("ZIP 文件在校验期间发生变化。")

            if manifest_present:
                manifest = _load_manifest(bytes(manifest_bytes))
                _validate_manifest(manifest, names, records)
                if expected_manifest is not None and manifest != expected_manifest:
                    raise _ArchiveRejected("manifest 校验失败。")
            elif expected_manifest is not None:
                # The generated-archive compatibility path always asks for a
                # manifest when it supplies an expected payload.
                raise _ArchiveRejected("ZIP 缺少必需的 manifest。")

    return PackArchiveVerification(
        verified=True,
        contains_manifest=manifest_present,
        entries=names,
        archive_bytes=archive_bytes,
    )


def _stream_size(stream: Any) -> int:
    return int(os.fstat(stream.fileno()).st_size)


def _validate_zip_infos(infos: tuple[zipfile.ZipInfo, ...]) -> None:
    identities: list[str] = []
    total_uncompressed = 0
    for info in infos:
        name = info.filename
        if info.is_dir():
            raise _ArchiveRejected("ZIP 条目路径不安全。")
        identities.append(_validate_zip_name(name))

        _validate_zip_metadata(info)
        if info.file_size < 0 or info.compress_size < 0:
            raise _ArchiveRejected("ZIP 条目大小校验失败。")
        total_uncompressed += info.file_size
        if total_uncompressed > _MAX_TOTAL_UNCOMPRESSED_BYTES:
            raise _ArchiveRejected("ZIP 解压内容超过校验大小上限。")
        if info.file_size and info.compress_size == 0:
            raise _ArchiveRejected("ZIP 条目压缩比超过校验上限。")
        if info.compress_size and info.file_size > info.compress_size * _MAX_COMPRESSION_RATIO:
            raise _ArchiveRejected("ZIP 条目压缩比超过校验上限。")
    identities.sort(key=lambda identity: tuple(identity.split("/")))
    for previous, current in zip(identities, identities[1:], strict=False):
        if current == previous or current.startswith(f"{previous}/"):
            raise _ArchiveRejected("ZIP 条目存在路径冲突。")


def _validate_zip_name(
    name: object,
    *,
    identity_key: Callable[[str], str] | None = None,
) -> str:
    if not isinstance(name, str) or not name or name.endswith("/"):
        raise _ArchiveRejected("ZIP 条目路径不安全。")
    try:
        name_bytes = name.encode("utf-8")
        parts = name.split("/")
        component_too_long = any(
            len(part.encode("utf-8")) > _MAX_ZIP_COMPONENT_BYTES for part in parts
        )
    except UnicodeEncodeError:
        raise _ArchiveRejected("ZIP 条目路径不安全。") from None
    if len(name_bytes) > _MAX_ZIP_NAME_BYTES:
        raise _ArchiveRejected("ZIP 条目路径超过校验长度上限。")
    if len(parts) > _MAX_ZIP_PATH_DEPTH:
        raise _ArchiveRejected("ZIP 条目路径层级超过校验上限。")
    if component_too_long:
        raise _ArchiveRejected("ZIP 条目路径超过校验长度上限。")
    try:
        normalized = validate_portable_relative_path(name)
    except (TypeError, UnicodeError, ValueError):
        raise _ArchiveRejected("ZIP 条目路径不安全。") from None
    if normalized.as_posix() != name:
        raise _ArchiveRejected("ZIP 条目存在路径冲突。")
    identity_function = _path_identity_key if identity_key is None else identity_key
    return identity_function(name)


def _validate_zip_names(
    names: tuple[str, ...], *, identity_key: Callable[[str], str] | None = None
) -> None:
    identities = [_validate_zip_name(name, identity_key=identity_key) for name in names]
    identities.sort(key=lambda identity: tuple(identity.split("/")))
    for previous, current in zip(identities, identities[1:], strict=False):
        if current == previous or current.startswith(f"{previous}/"):
            raise _ArchiveRejected("ZIP 条目存在路径冲突。")


def _validate_zip_metadata(info: zipfile.ZipInfo) -> None:
    if info.flag_bits & _ZIP_ENCRYPTED:
        raise _ArchiveRejected("ZIP 条目加密或元数据不安全。")

    external_attributes = info.external_attr
    if info.create_system not in {0, 3}:
        raise _ArchiveRejected("ZIP 条目加密或元数据不安全。")
    dos_attributes = external_attributes & 0xFFFF
    if dos_attributes & (_DOS_DIRECTORY | _DOS_REPARSE_POINT):
        raise _ArchiveRejected("ZIP 条目加密或元数据不安全。")

    unix_mode = (external_attributes >> 16) & 0xFFFF
    file_type = stat.S_IFMT(unix_mode)
    if file_type not in {0, stat.S_IFREG}:
        raise _ArchiveRejected("ZIP 条目加密或元数据不安全。")
    if info.internal_attr != 0:
        raise _ArchiveRejected("ZIP 条目加密或元数据不安全。")


def _path_identity_key(value: str) -> str:
    return unicodedata.normalize("NFC", value).casefold()


def _load_manifest(raw: bytes) -> dict[str, object]:
    try:
        parsed = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_object_pairs_without_duplicates,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeError, ValueError):
        raise _ArchiveRejected("manifest 校验失败。") from None
    if not isinstance(parsed, dict):
        raise _ArchiveRejected("manifest 校验失败。")
    return parsed


def _object_pairs_without_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonKey(key)
        result[key] = value
    return result


def _validate_manifest(
    manifest: dict[str, object],
    names: tuple[str, ...],
    records: dict[str, tuple[int, str]],
) -> None:
    if set(manifest) != _MANIFEST_KEYS:
        raise _ArchiveRejected("manifest 结构无效。")
    if type(manifest.get("schema_version")) is not int or manifest["schema_version"] != 1:
        raise _ArchiveRejected("manifest 结构无效。")
    if not _nonempty_string(manifest.get("csbox_version")):
        raise _ArchiveRejected("manifest 结构无效。")
    if not _canonical_timestamp(manifest.get("generated_at")):
        raise _ArchiveRejected("manifest 结构无效。")
    if not _nonempty_string(manifest.get("project_type")):
        raise _ArchiveRejected("manifest 结构无效。")
    package_manager = manifest.get("package_manager")
    if package_manager is not None and not isinstance(package_manager, str):
        raise _ArchiveRejected("manifest 结构无效。")
    source_bytes = manifest.get("source_bytes")
    if type(source_bytes) is not int or source_bytes < 0:
        raise _ArchiveRejected("manifest 结构无效。")
    if source_bytes > _MAX_TOTAL_UNCOMPRESSED_BYTES:
        raise _ArchiveRejected("manifest 结构无效。")

    excluded = manifest.get("excluded")
    rejected = manifest.get("rejected")
    if not _string_list(excluded) or not _string_list(rejected):
        raise _ArchiveRejected("manifest 结构无效。")

    verification = manifest.get("verification")
    if not isinstance(verification, dict) or set(verification) != _VERIFICATION_KEYS:
        raise _ArchiveRejected("manifest 结构无效。")
    requested = verification.get("requested")
    status = verification.get("status")
    if type(requested) is not bool or status != ("verified" if requested else "not_requested"):
        raise _ArchiveRejected("manifest 结构无效。")

    files = manifest.get("files")
    if not isinstance(files, list):
        raise _ArchiveRejected("manifest 文件清单无效。")
    expected_names = tuple(name for name in names if name != "manifest.json")
    if len(files) != len(expected_names):
        raise _ArchiveRejected("manifest 与 ZIP 条目不一致。")

    seen: set[str] = set()
    listed_names: list[str] = []
    for item in files:
        if not isinstance(item, dict) or set(item) != _FILE_RECORD_KEYS:
            raise _ArchiveRejected("manifest 文件清单无效。")
        name = item.get("path")
        if not isinstance(name, str) or name == "manifest.json":
            raise _ArchiveRejected("manifest 文件路径无效。")
        try:
            normalized = validate_portable_relative_path(name)
        except (TypeError, UnicodeError, ValueError):
            raise _ArchiveRejected("manifest 文件路径无效。") from None
        if normalized.as_posix() != name:
            raise _ArchiveRejected("manifest 文件路径无效。")
        identity = _path_identity_key(name)
        if identity in seen:
            raise _ArchiveRejected("manifest 文件清单存在路径冲突。")
        seen.add(identity)
        listed_names.append(name)
        size = item.get("size")
        digest = item.get("sha256")
        if type(size) is not int or size < 0:
            raise _ArchiveRejected("manifest 文件清单无效。")
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise _ArchiveRejected("manifest 文件清单无效。")
        actual = records.get(name)
        if actual is None or size != actual[0] or digest != actual[1]:
            raise _ArchiveRejected("manifest 文件摘要不一致。")

    if tuple(listed_names) != expected_names:
        raise _ArchiveRejected("manifest 与 ZIP 条目不一致。")
    if source_bytes != sum(records[name][0] for name in expected_names):
        raise _ArchiveRejected("manifest 项目大小不一致。")


def _nonempty_string(value: object) -> bool:
    return isinstance(value, str) and bool(value)


def _string_list(value: object) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def _canonical_timestamp(value: object) -> bool:
    if not isinstance(value, str) or not value.endswith("Z"):
        return False
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        return False
    if parsed.tzinfo is None or parsed.utcoffset() is None or parsed.microsecond:
        return False
    return parsed.isoformat(timespec="seconds").replace("+00:00", "Z") == value


def _reject_json_constant(value: str) -> None:
    raise ValueError(value)


__all__ = ["PackArchiveVerification", "PackArchiveVerifier"]
