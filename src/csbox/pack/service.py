from __future__ import annotations

import hashlib
import inspect
import json
import os
import re
import shutil
import stat
import tempfile
import unicodedata
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from csbox import __version__
from csbox.check.detectors import FileInventory
from csbox.check.service import CheckService
from csbox.config.loader import load_config
from csbox.core.safe_paths import atomic_copy_file, safe_relative_path
from csbox.pack.filters import PackCandidate, PackFilter, PackSafetyError, PackSelection
from csbox.pack.models import PackPlan, PackReport


class PackServiceError(RuntimeError):
    """A safe package could not be produced."""


_SAFE_REJECTION_CATEGORIES = frozenset({"env", "private-key", "hard-coded-secret"})
_VERIFY_CHUNK_BYTES = 1024 * 1024
_MAX_MANIFEST_BYTES = 8 * 1024 * 1024
_OUTPUT_COMPONENT_BUDGET = 236


@dataclass(frozen=True, slots=True)
class _PlanContext:
    plan: PackPlan
    inventory: FileInventory
    candidates: tuple[PackCandidate, ...]


class PackService:
    def __init__(
        self,
        *,
        check_service: object | None = None,
        filename_template: str = "{id}-{name}-{course}.zip",
        student_id: str | None = None,
        student_name: str | None = None,
        course_name: str | None = None,
    ) -> None:
        self.check_service = check_service
        self.filename_template = filename_template
        self.student_id = student_id
        self.student_name = student_name
        self.course_name = course_name

    def plan(
        self,
        root: Path | str,
        *,
        destination: Path | str | None = None,
        verify: bool = False,
        force: bool = False,
        include_manifest: bool = False,
        include: tuple[str, ...] = (),
        exclude: tuple[str, ...] = (),
    ) -> PackPlan:
        return self._build_plan(
            root,
            destination=destination,
            verify=verify,
            force=force,
            include_manifest=include_manifest,
            include=include,
            exclude=exclude,
        ).plan

    def pack(
        self,
        root: Path | str,
        *,
        destination: Path | str | None = None,
        verify: bool = False,
        force: bool = False,
        include_manifest: bool = False,
        include: tuple[str, ...] = (),
        exclude: tuple[str, ...] = (),
    ) -> PackReport:
        context = self._build_plan(
            root,
            destination=destination,
            verify=verify,
            force=force,
            include_manifest=include_manifest,
            include=include,
            exclude=exclude,
        )
        return self._publish(context)

    def pack_plan(
        self,
        plan: PackPlan,
        *,
        verify: bool | None = None,
        force: bool | None = None,
    ) -> PackReport:
        """Revalidate a displayed plan before publishing its archive."""
        current = self._build_plan(
            plan.source_root,
            destination=plan.destination,
            verify=plan.verification_requested if verify is None else verify,
            force=plan.force if force is None else force,
            include_manifest=plan.include_manifest,
            include=plan.include,
            exclude=plan.exclude,
        )
        if not _same_plan(current.plan, plan):
            raise PackServiceError("打包计划已变化，请重新预览。")
        return self._publish(current)

    def _build_plan(
        self,
        root: Path | str,
        *,
        destination: Path | str | None,
        verify: bool,
        force: bool,
        include_manifest: bool,
        include: tuple[str, ...],
        exclude: tuple[str, ...],
    ) -> _PlanContext:
        try:
            source_root = Path(root).resolve(strict=True)
            if not source_root.is_dir():
                raise ValueError("source is not a directory")
            destination_path = self._destination(source_root, destination)
            self._inspect_destination(destination_path)
            output_directory = self._output_directory(source_root, destination)
            inventory = FileInventory.build(source_root, scan_limit_bytes=256 * 1024)
            checker = self.check_service or CheckService()
            check_report = self._run_check(checker, source_root, inventory)
            pack_filter = PackFilter(
                source_root,
                include=include,
                exclude=exclude,
                inventory=inventory,
            )
            selection = pack_filter.select()
        except PackServiceError:
            raise
        except (OSError, RuntimeError, ValueError, PackSafetyError):
            raise PackServiceError("无法安全建立打包计划。") from None

        candidates, extra_excluded, path_rejected = self._prepare_candidates(
            selection,
            destination=destination_path,
            include_manifest=include_manifest,
            output_directory=output_directory,
        )
        excluded = list(selection.excluded)
        excluded.extend(extra_excluded)
        rejected = list(selection.rejected)
        rejected.extend(path_rejected)
        rejected.extend(self._check_rejections(check_report, source_root))
        if getattr(check_report, "exit_code", 1) != 0 and not rejected:
            rejected.append("project:check-failed")

        project_type, package_manager = self._project_metadata(check_report, source_root)
        included = [candidate.relative.as_posix() for candidate in candidates]
        if include_manifest:
            included.append("manifest.json")
        normalized_included = tuple(included)
        plan = PackPlan(
            source_root=source_root,
            destination=destination_path,
            output_filename=destination_path.name,
            project_type=project_type,
            package_manager=package_manager,
            included=normalized_included,
            excluded=tuple(sorted(set(excluded), key=_stable_text_key)),
            rejected=tuple(sorted(set(rejected), key=_stable_text_key)),
            source_bytes=sum(candidate.size for candidate in candidates),
            include_manifest=include_manifest,
            verification_requested=verify,
            force=force,
            output_exists=_path_exists(destination_path),
            include=tuple(include),
            exclude=tuple(exclude),
            source_fingerprint=_source_fingerprint(inventory, candidates),
        )
        return _PlanContext(plan=plan, inventory=inventory, candidates=tuple(candidates))

    @staticmethod
    def _run_check(checker: object, root: Path, inventory: FileInventory) -> object:
        run = checker.run
        try:
            parameters = inspect.signature(run).parameters.values()
        except (TypeError, ValueError):
            parameters = ()
        accepts_inventory = any(
            parameter.name == "inventory" or parameter.kind is inspect.Parameter.VAR_KEYWORD
            for parameter in parameters
        )
        if accepts_inventory:
            return run(root, build=False, inventory=inventory)
        return run(root, build=False)

    def _prepare_candidates(
        self,
        selection: PackSelection,
        *,
        destination: Path,
        include_manifest: bool,
        output_directory: Path | None,
    ) -> tuple[list[PackCandidate], list[str], list[str]]:
        candidates = list(selection.candidates)
        excluded: list[str] = []
        rejected: list[str] = []
        destination_key = _lexical_path_key(destination)
        destination_parent_key = _lexical_path_key(destination.parent)
        publish_temp_prefix = f".{destination.name}-".casefold()
        kept: list[PackCandidate] = []
        for candidate in candidates:
            if not _is_portable_zip_path(candidate.relative.as_posix()):
                rejected.append(f"{candidate.relative.as_posix()}:path-unsafe")
                continue
            if _lexical_path_key(candidate.absolute) == destination_key:
                excluded.append(f"{candidate.relative.as_posix()}:output")
                continue
            if (
                _lexical_path_key(candidate.absolute.parent) == destination_parent_key
                and candidate.relative.name.casefold().startswith(publish_temp_prefix)
                and candidate.relative.name.casefold().endswith(".tmp")
            ):
                excluded.append(f"{candidate.relative.as_posix()}:output")
                continue
            if output_directory is not None and _is_relative_to(
                candidate.absolute, output_directory
            ):
                excluded.append(f"{candidate.relative.as_posix()}:output")
                continue
            kept.append(candidate)
        candidates = kept

        if include_manifest:
            kept = []
            for candidate in candidates:
                if candidate.relative.parent == Path(".") and (
                    candidate.relative.name.casefold() == "manifest.json"
                ):
                    excluded.append(f"{candidate.relative.as_posix()}:manifest")
                    continue
                if candidate.relative.parts and _path_identity_key(
                    candidate.relative.parts[0]
                ) == _path_identity_key("manifest.json"):
                    rejected.append(f"{candidate.relative.as_posix()}:path-conflict")
                    continue
                kept.append(candidate)
            candidates = kept

        conflicting_indices = _conflicting_candidate_indices(candidates)
        if conflicting_indices:
            rejected.extend(
                f"{candidate.relative.as_posix()}:path-conflict"
                for index, candidate in enumerate(candidates)
                if index in conflicting_indices
            )
            candidates = [
                candidate
                for index, candidate in enumerate(candidates)
                if index not in conflicting_indices
            ]
        candidates.sort(
            key=lambda item: (item.relative.as_posix().casefold(), item.relative.as_posix())
        )
        return candidates, excluded, rejected

    @staticmethod
    def _output_directory(source_root: Path, destination: Path | str | None) -> Path | None:
        if destination is None:
            return None
        candidate = Path(destination)
        try:
            metadata = candidate.lstat()
        except (FileNotFoundError, OSError):
            return None
        if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
            return None
        resolved = candidate.resolve(strict=True)
        return None if resolved == source_root else resolved

    @staticmethod
    def _check_rejections(report: object, root: Path) -> list[str]:
        rejected: list[str] = []
        for finding in getattr(report, "findings", ()):
            raw_status = getattr(finding, "status", None)
            status = getattr(raw_status, "value", raw_status)
            if status != "FAIL":
                continue
            raw_category = getattr(finding, "category", None)
            category = (
                raw_category if raw_category in _SAFE_REJECTION_CATEGORIES else "check-failed"
            )
            relative = _safe_finding_path(root, getattr(finding, "path", None))
            if relative is None:
                rejected.append(f"project:{category}")
            else:
                rejected.append(f"{relative}:{category}")
        return rejected

    @staticmethod
    def _project_metadata(report: object, root: Path) -> tuple[str, str | None]:
        for project in getattr(report, "projects", ()):
            if Path(getattr(project, "root", "")).resolve(strict=False) == root:
                return str(getattr(project, "kind", "unknown")), getattr(
                    project, "package_manager", None
                )
        return "unknown", None

    def _destination(self, root: Path, destination: Path | str | None) -> Path:
        if destination is None:
            return root / self._filename()
        candidate = Path(destination)
        try:
            metadata = candidate.lstat()
        except FileNotFoundError:
            return candidate
        except OSError:
            raise PackServiceError("输出路径不可用。") from None
        if stat.S_ISLNK(metadata.st_mode):
            raise PackServiceError("输出路径不可安全使用。")
        if stat.S_ISDIR(metadata.st_mode):
            return candidate / self._filename()
        if not stat.S_ISREG(metadata.st_mode):
            raise PackServiceError("输出路径不可安全使用。")
        return candidate

    @staticmethod
    def _inspect_destination(destination: Path) -> None:
        current = destination.absolute()
        while current != current.parent:
            if current.is_symlink():
                raise PackServiceError("输出路径不可安全使用。")
            current = current.parent
        _path_exists(destination)
        try:
            metadata = destination.lstat()
        except FileNotFoundError:
            return
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise PackServiceError("输出路径不可安全使用。")

    def _publish(self, context: _PlanContext) -> PackReport:
        plan = context.plan
        if plan.rejected:
            raise PackServiceError("项目包含敏感或不安全文件，已拒绝打包。")
        if plan.output_exists and not plan.force:
            raise PackServiceError("目标文件已存在，使用 --force 覆盖。")

        temporary_archive: Path | None = None
        try:
            with tempfile.TemporaryDirectory(prefix="csbox-pack-") as staging_name:
                staging = Path(staging_name)
                file_records: list[dict[str, object]] = []
                for candidate in context.candidates:
                    target = staging.joinpath(*candidate.relative.parts)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    digest, size = self._copy_candidate(context.inventory, candidate, target)
                    file_records.append(
                        {
                            "path": candidate.relative.as_posix(),
                            "size": size,
                            "sha256": digest,
                        }
                    )

                manifest_payload: dict[str, object] | None = None
                manifest_path: Path | None = None
                if plan.include_manifest:
                    manifest_payload = _manifest_payload(
                        plan,
                        file_records,
                    )
                    manifest_path = staging / "manifest.json"
                    manifest_path.write_bytes(_strict_json_bytes(manifest_payload))

                temporary_archive = staging / ".archive.zip"
                entries = tuple(plan.included)
                with zipfile.ZipFile(
                    temporary_archive,
                    mode="w",
                    compression=zipfile.ZIP_DEFLATED,
                    compresslevel=9,
                    allowZip64=True,
                ) as archive:
                    for candidate in context.candidates:
                        self._write_archive_file(
                            archive,
                            staging / candidate.relative,
                            candidate.relative.as_posix(),
                        )
                    if manifest_path is not None:
                        self._write_archive_file(archive, manifest_path, "manifest.json")

                if plan.verification_requested:
                    self._verify(temporary_archive, entries, manifest_payload)
                atomic_copy_file(
                    temporary_archive,
                    plan.destination,
                    replace_existing=plan.force,
                )
                temporary_archive = None
        except PackServiceError:
            raise
        except FileExistsError:
            raise PackServiceError("目标文件已存在，使用 --force 覆盖。") from None
        except (OSError, RuntimeError, ValueError, zipfile.BadZipFile, zipfile.LargeZipFile):
            raise PackServiceError("打包失败，未发布输出文件。") from None

        try:
            archive_bytes = plan.destination.stat().st_size
        except OSError:
            raise PackServiceError("打包失败，未找到已发布输出文件。") from None
        return PackReport(
            source_root=plan.source_root,
            destination=plan.destination,
            entries=plan.included,
            excluded=plan.excluded,
            rejected=plan.rejected,
            source_bytes=plan.source_bytes,
            archive_bytes=archive_bytes,
            verified=plan.verification_requested,
            verification_status=("verified" if plan.verification_requested else "not_requested"),
            project_type=plan.project_type,
            package_manager=plan.package_manager,
            include_manifest=plan.include_manifest,
        )

    @staticmethod
    def _copy_candidate(
        inventory: FileInventory,
        candidate: PackCandidate,
        target: Path,
    ) -> tuple[str, int]:
        entry = candidate.entry or inventory.entry(candidate.relative)
        if entry is None:
            raise PackServiceError("打包失败，源文件清单已变化。")
        digest = hashlib.sha256()
        size = 0
        try:
            with inventory.open_entry(entry) as source, target.open("xb") as output:
                while chunk := source.read(1024 * 1024):
                    output.write(chunk)
                    digest.update(chunk)
                    size += len(chunk)
        except (OSError, ValueError):
            raise PackServiceError("打包失败，源文件在复制时发生变化。") from None
        if size != candidate.size:
            raise PackServiceError("打包失败，源文件在复制时发生变化。")
        return digest.hexdigest(), size

    @staticmethod
    def _write_archive_file(archive: zipfile.ZipFile, source: Path, name: str) -> None:
        try:
            safe_relative_path(name)
        except ValueError:
            raise PackServiceError("ZIP 条目路径不安全。") from None
        info = _zip_info(name)
        try:
            with source.open("rb") as input_file, archive.open(info, mode="w") as output:
                shutil.copyfileobj(input_file, output, length=1024 * 1024)
        except (OSError, RuntimeError, ValueError):
            raise PackServiceError("打包失败，无法写入 ZIP。") from None

    def _filename(self) -> str:
        values = {
            "id": self.student_id or "student",
            "name": self.student_name or "project",
            "course": self.course_name or "course",
        }
        try:
            rendered = self.filename_template.format(**values)
        except (KeyError, ValueError):
            raise PackServiceError("pack 文件名模板无效。") from None
        safe = _sanitize_filename(rendered)
        return safe if safe.casefold().endswith(".zip") else f"{safe}.zip"

    @staticmethod
    def _verify(
        path: Path,
        expected: tuple[str, ...],
        manifest_payload: dict[str, object] | None = None,
    ) -> None:
        try:
            with zipfile.ZipFile(path) as archive:
                names = tuple(archive.namelist())
                _validate_zip_names(names)
                if names != expected:
                    raise PackServiceError("ZIP 条目与打包计划不一致。")
                records: dict[str, tuple[int, str]] = {}
                raw_manifest = bytearray()
                for name in names:
                    digest = hashlib.sha256()
                    size = 0
                    with archive.open(name) as stream:
                        while chunk := stream.read(_VERIFY_CHUNK_BYTES):
                            size += len(chunk)
                            digest.update(chunk)
                            if name == "manifest.json":
                                if len(raw_manifest) + len(chunk) > _MAX_MANIFEST_BYTES:
                                    raise PackServiceError("manifest 超过校验大小上限。")
                                raw_manifest.extend(chunk)
                    records[name] = (size, digest.hexdigest())
                if manifest_payload is None:
                    return
                actual_manifest = json.loads(
                    bytes(raw_manifest).decode("utf-8"),
                    parse_constant=_reject_json_constant,
                )
                if actual_manifest != manifest_payload:
                    raise PackServiceError("manifest 校验失败。")
                files = actual_manifest.get("files") if isinstance(actual_manifest, dict) else None
                if not isinstance(files, list):
                    raise PackServiceError("manifest 文件清单无效。")
                expected_files = expected[:-1]
                if [item.get("path") for item in files if isinstance(item, dict)] != list(
                    expected_files
                ):
                    raise PackServiceError("manifest 与 ZIP 条目不一致。")
                for item in files:
                    if not isinstance(item, dict):
                        raise PackServiceError("manifest 文件清单无效。")
                    name = item.get("path")
                    if not isinstance(name, str):
                        raise PackServiceError("manifest 文件路径无效。")
                    record = records.get(name)
                    if (
                        record is None
                        or item.get("size") != record[0]
                        or item.get("sha256") != record[1]
                    ):
                        raise PackServiceError("manifest 文件摘要不一致。")
        except (OSError, UnicodeError, json.JSONDecodeError, zipfile.BadZipFile):
            raise PackServiceError("ZIP 校验失败。") from None


def _manifest_payload(plan: PackPlan, files: list[dict[str, object]]) -> dict[str, object]:
    return {
        "schema_version": 1,
        "csbox_version": __version__,
        "generated_at": datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "project_type": plan.project_type,
        "package_manager": plan.package_manager,
        "source_bytes": plan.source_bytes,
        "files": files,
        "excluded": list(plan.excluded),
        "rejected": list(plan.rejected),
        "verification": {
            "requested": plan.verification_requested,
            "status": "verified" if plan.verification_requested else "not_requested",
        },
    }


def _strict_json_bytes(payload: dict[str, object]) -> bytes:
    return (
        json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _zip_info(name: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = 3
    info.create_version = 20
    info.extract_version = 20
    info.external_attr = 0o100644 << 16
    info.internal_attr = 0
    return info


def _validate_zip_names(names: tuple[str, ...]) -> None:
    identities: list[str] = []
    for name in names:
        if not name or name.endswith("/"):
            raise PackServiceError("ZIP 条目路径不安全。")
        try:
            normalized = safe_relative_path(name)
        except ValueError:
            raise PackServiceError("ZIP 条目路径不安全。") from None
        identity = _path_identity_key(name)
        if normalized.as_posix() != name or not _is_portable_zip_path(name):
            raise PackServiceError("ZIP 条目存在路径冲突。")
        identities.append(identity)
    identities.sort(key=lambda identity: tuple(identity.split("/")))
    for previous, current in zip(identities, identities[1:], strict=False):
        if current == previous or current.startswith(f"{previous}/"):
            raise PackServiceError("ZIP 条目存在路径冲突。")


_WINDOWS_RESERVED_BASENAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{index}" for index in range(1, 10)}
    | {f"LPT{index}" for index in range(1, 10)}
)
_WINDOWS_INVALID_PATH_CHARACTERS = frozenset('<>:"\\|?*')


def _is_portable_zip_path(name: str) -> bool:
    for component in name.split("/"):
        if (
            not component
            or component.endswith((" ", "."))
            or any(
                ord(character) < 32 or character in _WINDOWS_INVALID_PATH_CHARACTERS
                for character in component
            )
        ):
            return False
        basename = component.split(".", maxsplit=1)[0].upper()
        if basename in _WINDOWS_RESERVED_BASENAMES:
            return False
    return True


def _same_plan(left: PackPlan, right: PackPlan) -> bool:
    fields = (
        "source_root",
        "destination",
        "output_filename",
        "project_type",
        "package_manager",
        "included",
        "excluded",
        "rejected",
        "source_bytes",
        "source_fingerprint",
        "include_manifest",
        "output_exists",
        "include",
        "exclude",
    )
    return all(getattr(left, field) == getattr(right, field) for field in fields)


def _safe_finding_path(root: Path, value: object) -> str | None:
    if value is None:
        return None
    path = Path(str(value))
    if path.is_absolute():
        try:
            path = path.resolve(strict=False).relative_to(root)
        except ValueError:
            return None
    try:
        return safe_relative_path(path.as_posix()).as_posix()
    except ValueError:
        return None


def _path_exists(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    except OSError:
        raise PackServiceError("输出路径不可用。") from None
    return True


def _lexical_path_key(path: Path) -> str:
    return _path_identity_key(os.path.normcase(os.path.abspath(os.fspath(path))))


def _path_identity_key(value: str) -> str:
    return unicodedata.normalize("NFC", value).casefold()


def _conflicting_candidate_indices(candidates: list[PackCandidate]) -> set[int]:
    """Return candidates whose normalized ZIP paths duplicate or contain one another."""
    ordered = sorted(
        enumerate(candidates),
        key=lambda item: (
            _path_identity_key(item[1].relative.as_posix()),
            item[1].relative.as_posix(),
        ),
    )
    seen: dict[str, list[int]] = {}
    conflicts: set[int] = set()
    for index, candidate in ordered:
        identity = _path_identity_key(candidate.relative.as_posix())
        parts = identity.split("/")
        prefix = []
        for part in parts:
            prefix.append(part)
            for previous_index in seen.get("/".join(prefix), ()):
                conflicts.add(index)
                conflicts.add(previous_index)
        seen.setdefault(identity, []).append(index)
    return conflicts


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        Path(path).resolve(strict=False).relative_to(root)
    except ValueError:
        return False
    return True


def _stable_text_key(value: str) -> tuple[str, str]:
    return value.casefold(), value


def _source_fingerprint(
    inventory: FileInventory,
    candidates: list[PackCandidate],
) -> str:
    digest = hashlib.sha256()
    for candidate in candidates:
        entry = candidate.entry or inventory.entry(candidate.relative)
        if entry is None:
            raise PackServiceError("打包失败，源文件清单已变化。")
        fields = (
            candidate.relative.as_posix(),
            str(entry.size),
            str(entry.device),
            str(entry.inode),
            str(entry.mtime_ns),
            str(entry.ctime_ns),
        )
        digest.update("\x00".join(fields).encode("utf-8"))
        digest.update(b"\x00")
    return digest.hexdigest()


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant: {value}")


def _sanitize_filename(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value)
    sanitized = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", normalized).strip(" .")
    sanitized = re.sub(r"_+", "_", sanitized)
    if sanitized in {"", ".", ".."}:
        return "csbox-project"
    basename = sanitized.split(".", maxsplit=1)[0].upper()
    if basename in _WINDOWS_RESERVED_BASENAMES:
        sanitized = f"_{sanitized}"
    if (
        len(sanitized.encode("utf-8")) <= _OUTPUT_COMPONENT_BUDGET
        and _windows_units(sanitized) <= _OUTPUT_COMPONENT_BUDGET
    ):
        return sanitized

    extension = ".zip" if sanitized.casefold().endswith(".zip") else ""
    stem = sanitized[: -len(extension)] if extension else sanitized
    digest_suffix = f"-{hashlib.sha256(normalized.encode()).hexdigest()[:8]}{extension}"
    byte_budget = _OUTPUT_COMPONENT_BUDGET - len(digest_suffix.encode("utf-8"))
    windows_budget = _OUTPUT_COMPONENT_BUDGET - _windows_units(digest_suffix)
    shortened: list[str] = []
    byte_count = 0
    windows_count = 0
    for character in stem:
        character_bytes = len(character.encode("utf-8"))
        character_windows_units = _windows_units(character)
        if (
            byte_count + character_bytes > byte_budget
            or windows_count + character_windows_units > windows_budget
        ):
            break
        shortened.append(character)
        byte_count += character_bytes
        windows_count += character_windows_units
    prefix = "".join(shortened).rstrip(" ._") or "csbox-project"
    return f"{prefix}{digest_suffix}"


def _windows_units(value: str) -> int:
    return len(value.encode("utf-16-le")) // 2


def create_pack_service(cwd: Path | str | None = None) -> PackService:
    project_dir = Path.cwd() if cwd is None else Path(cwd)
    config = load_config(project_dir)
    return PackService(
        check_service=CheckService(config=config),
        filename_template=config.pack.filename,
        student_id=config.student.id,
        student_name=config.student.name,
        course_name=config.course.name,
    )


__all__ = ["PackService", "PackServiceError", "create_pack_service"]
