from __future__ import annotations

import os
import re
import shutil
import tempfile
import zipfile
from pathlib import Path

from csbox.check.service import CheckService
from csbox.config.loader import load_config
from csbox.pack.filters import PackFilter, PackSafetyError
from csbox.pack.models import PackReport


class PackServiceError(RuntimeError):
    """A safe package could not be produced."""


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

    def pack(
        self,
        root: Path | str,
        *,
        destination: Path | str | None = None,
        verify: bool = False,
        force: bool = False,
        include: tuple[str, ...] = (),
        exclude: tuple[str, ...] = (),
    ) -> PackReport:
        source_root = Path(root).resolve(strict=True)
        if not source_root.is_dir():
            raise PackServiceError(f"项目目录不存在：{source_root}")
        checker = self.check_service or CheckService()
        try:
            check_report = checker.run(source_root, build=False)
        except Exception as error:
            raise PackServiceError(f"项目检查失败，已拒绝打包：{error}") from error
        if getattr(check_report, "exit_code", 1) != 0:
            raise PackServiceError("项目检查未通过，已拒绝打包。")
        destination_path = self._destination(source_root, destination)
        try:
            pack_filter = PackFilter(source_root, include=include, exclude=exclude)
            candidates = list(pack_filter.candidates())
        except (OSError, ValueError, PackSafetyError) as error:
            if isinstance(error, PackSafetyError):
                raise PackServiceError(str(error)) from error
            raise PackServiceError(f"项目路径不安全：{error}") from error

        excluded = list(pack_filter.exclusions())
        filtered: list = []
        destination_resolved = destination_path.resolve(strict=False)
        for candidate in candidates:
            if candidate.absolute.resolve(strict=False) == destination_resolved:
                excluded.append(f"{candidate.relative}:output")
            else:
                filtered.append(candidate)
        candidates = filtered
        if destination_path.exists() and not force:
            raise PackServiceError(f"目标文件已存在，使用 --force 覆盖：{destination_path}")

        destination_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_archive: Path | None = None
        entries = tuple(candidate.relative.as_posix() for candidate in candidates)
        source_bytes = sum(candidate.size for candidate in candidates)
        try:
            with tempfile.TemporaryDirectory(prefix="csbox-pack-") as staging_name:
                staging = Path(staging_name)
                for candidate in candidates:
                    target = staging / candidate.relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(candidate.absolute, target)
                with tempfile.NamedTemporaryFile(
                    dir=destination_path.parent,
                    prefix=f".{destination_path.name}.",
                    suffix=".tmp",
                    delete=False,
                ) as temporary_file:
                    temporary_archive = Path(temporary_file.name)
                with zipfile.ZipFile(
                    temporary_archive,
                    mode="w",
                    compression=zipfile.ZIP_DEFLATED,
                ) as archive:
                    for relative in entries:
                        archive.write(staging / Path(relative), arcname=relative)
                if verify:
                    self._verify(temporary_archive, entries)
                os.replace(temporary_archive, destination_path)
                temporary_archive = None
        except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile, RuntimeError) as error:
            raise PackServiceError(f"打包失败：{error}") from error
        finally:
            if temporary_archive is not None:
                temporary_archive.unlink(missing_ok=True)
        return PackReport(
            source_root=source_root,
            destination=destination_path,
            entries=entries,
            excluded=tuple(sorted(excluded)),
            source_bytes=source_bytes,
            archive_bytes=destination_path.stat().st_size,
            verified=verify,
        )

    def _destination(self, root: Path, destination: Path | str | None) -> Path:
        if destination is None:
            return root / self._filename()
        candidate = Path(destination)
        if candidate.exists() and candidate.is_dir():
            return candidate / self._filename()
        return candidate

    def _filename(self) -> str:
        values = {
            "id": self.student_id or "student",
            "name": self.student_name or "project",
            "course": self.course_name or "course",
        }
        try:
            rendered = self.filename_template.format(**values)
        except (KeyError, ValueError) as error:
            raise PackServiceError("pack 文件名模板无效。") from error
        safe = _sanitize_filename(rendered)
        return safe if safe.casefold().endswith(".zip") else f"{safe}.zip"

    @staticmethod
    def _verify(path: Path, expected: tuple[str, ...]) -> None:
        with zipfile.ZipFile(path) as archive:
            if archive.testzip() is not None:
                raise PackServiceError("ZIP 校验发现损坏条目。")
            if tuple(archive.namelist()) != expected:
                raise PackServiceError("ZIP 条目与 staging 清单不一致。")


def _sanitize_filename(value: str) -> str:
    sanitized = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value).strip(" .")
    sanitized = re.sub(r"_+", "_", sanitized)
    if sanitized in {"", ".", ".."}:
        return "csbox-project"
    return sanitized[:180]


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
