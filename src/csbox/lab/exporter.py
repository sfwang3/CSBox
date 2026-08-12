from __future__ import annotations

import hashlib
import html
import json
import re
import secrets
import shutil
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path, PurePosixPath

from pydantic import ValidationError

from csbox.core.safe_paths import (
    atomic_copy_file,
    atomic_write_text,
    mkdir_exclusive,
    open_regular_binary,
    read_regular_text,
    restore_backup_or_preserve,
    safe_rename,
)
from csbox.lab.captures import CaptureStore
from csbox.lab.models import SessionMetadata, SessionPaths
from csbox.lab.renderer import RenderTheme, TerminalEvidenceRenderer

_COMPONENT_BUDGET = 240
_EVIDENCE_MANIFEST = ".csbox-generated-evidence.json"
_MAX_LAB_METADATA_BYTES = 4 * 1024 * 1024
_MAX_LAB_MANIFEST_BYTES = 4 * 1024 * 1024


class LabExportError(RuntimeError):
    """A lab session cannot be exported without risking user data."""


@dataclass(frozen=True, slots=True)
class LabExportResult:
    destination: Path
    evidence: tuple[Path, ...]
    markdown: Path
    cast: Path
    commands: Path | None
    warnings: tuple[str, ...] = ()


class LabExporter:
    """Export captures, their source cast, and stable Chinese Markdown."""

    def __init__(
        self,
        renderer: TerminalEvidenceRenderer,
        *,
        theme: RenderTheme | None = None,
    ) -> None:
        self.renderer = renderer
        self.theme = theme or RenderTheme.dark()

    def export(
        self,
        session: SessionPaths | Path | str,
        destination: Path | str,
        *,
        force: bool = False,
    ) -> LabExportResult:
        paths = session if isinstance(session, SessionPaths) else SessionPaths(session)
        output = Path(destination)
        _validate_session_sources(paths)
        if output.exists() and output.resolve() == paths.root.resolve():
            raise LabExportError("导出目录不能覆盖原始实验目录。")
        if output.is_symlink():
            raise LabExportError("导出目录不能是符号链接。")
        destination_was_existing = output.exists()
        if destination_was_existing:
            self._prepare_destination(output, force=force)
            _validate_existing_export_targets(output)
        previous_evidence, cleanup_warnings = (
            _previous_generated_evidence(output) if destination_was_existing and force else ((), ())
        )
        staging = _new_export_staging(output)
        working_output = staging
        try:
            self._prepare_destination(working_output, force=True)
            evidence_directory = working_output / "evidence"
            self._prepare_evidence_directory(evidence_directory)
            markdown_path = working_output / "evidence.md"
            cast_path = working_output / "session.cast"
            commands_path = working_output / "commands.txt"
            manifest_path = working_output / _EVIDENCE_MANIFEST
            for path in (markdown_path, cast_path, commands_path, manifest_path):
                _reject_symlink(path)

            capture_result = CaptureStore(paths.captures).load()
            session_start, metadata_warnings = _load_session_start(paths)
            rendered: list[Path] = []
            markdown_entries: list[str] = []
            commands: list[str] = []
            for index, capture in enumerate(capture_result.captures, start=1):
                title = _display_title(capture.title, index)
                slug = _safe_slug(capture.title, index)
                image_path = evidence_directory / f"{index:02d}-{slug}.png"
                _reject_symlink(image_path)
                self.renderer.render(capture.snapshot, image_path, self.theme)
                rendered.append(image_path)
                captured_at = (
                    session_start + timedelta(seconds=capture.timestamp)
                    if session_start is not None
                    else capture.created_at
                )
                markdown_entries.append(_markdown_entry(index, title, captured_at, image_path.name))
                command = _known_command(capture.command)
                if command is not None:
                    commands.append(command)

            markdown = "## 实验记录\n"
            if markdown_entries:
                markdown += "\n" + "\n\n".join(markdown_entries) + "\n"
            atomic_write_text(markdown_path, markdown)
            atomic_copy_file(paths.cast, cast_path, replace_existing=False)
            exported_commands: Path | None = None
            if commands:
                atomic_write_text(commands_path, "\n".join(commands) + "\n")
                exported_commands = commands_path
            _write_evidence_manifest(manifest_path, rendered, working_output)

            result = LabExportResult(
                destination=output,
                evidence=tuple(rendered),
                markdown=markdown_path,
                cast=cast_path,
                commands=exported_commands,
                warnings=(*capture_result.warnings, *metadata_warnings, *cleanup_warnings),
            )
            generated = tuple(
                (path, output / path.relative_to(staging))
                for path in (*rendered, markdown_path, cast_path, manifest_path)
            )
            if exported_commands is not None:
                generated += ((commands_path, output / "commands.txt"),)
            generated_destinations = {destination for _source, destination in generated}
            stale = tuple(path for path in previous_evidence if path not in generated_destinations)
            obsolete = ()
            if destination_was_existing and force and exported_commands is None:
                obsolete = (output / "commands.txt",)

            if destination_was_existing:
                try:
                    _publish_staged_export(staging, output, generated, (*stale, *obsolete))
                except (OSError, ValueError) as exc:
                    raise LabExportError(
                        "导出发布失败；并发冲突文件会保留为 .csbox-recovery-*.bak。"
                    ) from exc
            else:
                if output.exists() or output.is_symlink():
                    raise LabExportError("导出目录在发布前已被创建。")
                _validate_staged_export(generated)
                try:
                    safe_rename(staging, output, replace_existing=False)
                except (OSError, ValueError) as exc:
                    raise LabExportError("导出目录在发布前已被创建或不可安全使用。") from exc
            _remove_staging(staging)
            return _relocate_export_result(result, staging, output)
        except BaseException:
            _remove_staging(staging)
            raise

    @staticmethod
    def _prepare_destination(destination: Path, *, force: bool) -> None:
        if destination.is_symlink():
            raise LabExportError("导出目录不能是符号链接。")
        if destination.exists():
            if not destination.is_dir():
                raise LabExportError(f"导出路径不是目录：{destination}")
            if not force:
                raise LabExportError(f"导出目录已存在；如需覆盖请使用 force：{destination}")
            return
        mkdir_exclusive(destination)

    @staticmethod
    def _prepare_evidence_directory(destination: Path) -> None:
        if destination.is_symlink():
            raise LabExportError("evidence 目录不能是符号链接。")
        if destination.exists() and not destination.is_dir():
            raise LabExportError(f"evidence 路径不是目录：{destination}")
        mkdir_exclusive(destination)


def _markdown_entry(
    index: int,
    title: str,
    captured_at: datetime,
    image_name: str,
) -> str:
    return (
        f"### {index}. {title}\n\n"
        "时间：\n"
        f"{captured_at:%H:%M:%S}\n\n"
        "结果：\n\n"
        f"![实验记录]({_markdown_url(f'evidence/{image_name}')})"
    )


def _new_export_staging(output: Path) -> Path:
    for _ in range(8):
        staging = output.parent / f".csbox-lab-export-{secrets.token_hex(16)}.partial"
        try:
            mkdir_exclusive(staging)
        except FileExistsError:
            continue
        return staging
    raise LabExportError("无法创建安全的导出临时目录。")


def _remove_staging(staging: Path) -> None:
    if staging.is_symlink() or not staging.is_dir():
        return
    if any(staging.glob(".backup-*")):
        return
    shutil.rmtree(staging, ignore_errors=True)


def _relocate_export_result(
    result: LabExportResult,
    staging: Path,
    output: Path,
) -> LabExportResult:
    def relocate(path: Path) -> Path:
        return output / path.relative_to(staging)

    return LabExportResult(
        destination=output,
        evidence=tuple(relocate(path) for path in result.evidence),
        markdown=relocate(result.markdown),
        cast=relocate(result.cast),
        commands=None if result.commands is None else relocate(result.commands),
        warnings=result.warnings,
    )


def _validate_existing_export_targets(destination: Path) -> None:
    evidence_directory = destination / "evidence"
    if evidence_directory.is_symlink():
        raise LabExportError("evidence 目录不能是符号链接。")
    if evidence_directory.exists() and not evidence_directory.is_dir():
        raise LabExportError(f"evidence 路径不是目录：{evidence_directory}")
    for path in (
        destination / "evidence.md",
        destination / "session.cast",
        destination / "commands.txt",
        destination / _EVIDENCE_MANIFEST,
    ):
        _reject_symlink(path)
        if path.exists() and not path.is_file():
            raise LabExportError(f"导出目标不是文件：{path}")


def _validate_staged_export(generated: tuple[tuple[Path, Path], ...]) -> None:
    for source, _destination in generated:
        if source.is_symlink() or not source.is_file():
            raise LabExportError(f"导出临时文件无效：{source}")


def _publish_staged_export(
    staging: Path,
    output: Path,
    generated: tuple[tuple[Path, Path], ...],
    obsolete: tuple[Path, ...],
) -> None:
    """Publish generated files while preserving user-owned export files."""

    if output.is_symlink() or not output.is_dir():
        raise LabExportError("导出目录在发布前不可用。")
    _validate_staged_export(generated)
    evidence_directory = output / "evidence"
    if evidence_directory.is_symlink():
        raise LabExportError("evidence 目录不能是符号链接。")
    created_evidence_directory = False
    if evidence_directory.exists() and not evidence_directory.is_dir():
        raise LabExportError(f"evidence 路径不是目录：{evidence_directory}")
    if not evidence_directory.exists():
        mkdir_exclusive(evidence_directory)
        created_evidence_directory = True

    backups: dict[Path, Path] = {}
    operations = (*generated, *((None, path) for path in obsolete))
    try:
        for index, (source, destination) in enumerate(operations, start=1):
            _reject_symlink(destination)
            if destination.exists():
                if not destination.is_file():
                    raise LabExportError(f"导出目标不是文件：{destination}")
                backup = staging / f".backup-{index}"
                safe_rename(destination, backup, replace_existing=False)
                backups[destination] = backup
            if source is not None:
                safe_rename(source, destination, replace_existing=False)
    except BaseException:
        for destination, backup in reversed(tuple(backups.items())):
            if backup.exists():
                restore_backup_or_preserve(backup, destination)
        if (
            created_evidence_directory
            and evidence_directory.is_dir()
            and not any(evidence_directory.iterdir())
        ):
            evidence_directory.rmdir()
        raise
    for backup in backups.values():
        backup.unlink()


def _display_title(title: str, index: int) -> str:
    normalized = " ".join(title.split())
    return _escape_markdown_text(normalized or f"实验记录 {index}")


def _safe_slug(title: str, index: int) -> str:
    normalized = unicodedata.normalize("NFKC", title)
    characters: list[str] = []
    for character in normalized:
        if unicodedata.category(character).startswith("C"):
            continue
        if character in '<>:"/\\|?*' or character.isspace():
            characters.append("-")
        else:
            characters.append(character)
    slug = re.sub(r"-+", "-", "".join(characters)).strip(" .-_")
    while ".." in slug:
        slug = slug.replace("..", ".")
    if not slug:
        slug = f"实验记录-{index}"
    if slug.split(".", maxsplit=1)[0].upper() in {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        "COM1",
        "COM2",
        "COM3",
        "COM4",
        "COM5",
        "COM6",
        "COM7",
        "COM8",
        "COM9",
        "LPT1",
        "LPT2",
        "LPT3",
        "LPT4",
        "LPT5",
        "LPT6",
        "LPT7",
        "LPT8",
        "LPT9",
    }:
        slug = f"_{slug}"
    prefix = f"{index:02d}-"
    extension = ".png"
    byte_budget = _COMPONENT_BUDGET - len((prefix + extension).encode())
    windows_budget = _COMPONENT_BUDGET - _windows_units(prefix + extension)
    if len(slug.encode()) > byte_budget or _windows_units(slug) > windows_budget:
        digest = hashlib.sha256(normalized.encode()).hexdigest()[:8]
        suffix = f"-{digest}"
        byte_budget -= len(suffix.encode())
        windows_budget -= _windows_units(suffix)
        shortened: list[str] = []
        for character in slug:
            candidate = "".join(shortened) + character
            if len(candidate.encode()) > byte_budget or _windows_units(candidate) > windows_budget:
                break
            shortened.append(character)
        slug = "".join(shortened).rstrip(" .-_") + suffix
    return slug


def _known_command(command: str | None) -> str | None:
    if command is None or any(
        not character.isprintable() or unicodedata.category(character).startswith("C")
        for character in command
    ):
        return None
    stripped = command.strip()
    return stripped or None


def _reject_symlink(path: Path) -> None:
    if path.is_symlink():
        raise LabExportError(f"拒绝覆盖符号链接：{path}")


def _validate_session_sources(paths: SessionPaths) -> None:
    if paths.root.is_symlink():
        raise LabExportError("实验目录不能是符号链接。")
    try:
        root = paths.root.resolve(strict=True)
    except OSError as exc:
        raise LabExportError(f"找不到实验目录：{paths.root}") from exc
    if not root.is_dir():
        raise LabExportError(f"实验路径不是目录：{paths.root}")
    sidecars = (
        paths.cast,
        paths.captures,
        paths.captures.with_suffix(paths.captures.suffix + ".bak"),
        paths.metadata,
        paths.checkpoints,
    )
    for sidecar in sidecars:
        if sidecar.is_symlink():
            raise LabExportError(f"实验 sidecar 不能是符号链接：{sidecar.name}")
        if sidecar.exists() and not sidecar.resolve(strict=True).is_relative_to(root):
            raise LabExportError(f"实验 sidecar 越出实验目录：{sidecar.name}")
    if not paths.cast.is_file():
        raise LabExportError(f"找不到实验录制文件：{paths.cast}")


def _escape_markdown_text(value: str) -> str:
    escaped = re.sub(r"([\\`*_{}\[\]()#+|%])", r"\\\1", value)
    return html.escape(escaped, quote=False)


def _markdown_url(value: str) -> str:
    parts: list[str] = []
    for character in value:
        if ord(character) > 127 or character.isalnum() or character in "-._~/":
            parts.append(character)
        else:
            parts.extend(f"%{byte:02X}" for byte in character.encode())
    return "".join(parts)


def _windows_units(value: str) -> int:
    return len(value.encode("utf-16-le")) // 2


def _load_session_start(paths: SessionPaths) -> tuple[datetime | None, tuple[str, ...]]:
    if not paths.metadata.exists():
        return None, ("metadata.json 缺失，证据时间回退到 capture createdAt。",)
    try:
        metadata = SessionMetadata.model_validate_json(
            read_regular_text(paths.metadata, max_bytes=_MAX_LAB_METADATA_BYTES)
        )
    except (OSError, UnicodeError, ValidationError, ValueError):
        return None, ("metadata.json 无效，证据时间回退到 capture createdAt。",)
    return metadata.started_at, ()


def _previous_generated_evidence(
    destination: Path,
) -> tuple[tuple[Path, ...], tuple[str, ...]]:
    manifest = destination / _EVIDENCE_MANIFEST
    if not manifest.exists():
        return (), ()
    if manifest.is_symlink():
        raise LabExportError(f"历史 evidence manifest 不能是符号链接：{manifest.name}")
    try:
        document = json.loads(read_regular_text(manifest, max_bytes=_MAX_LAB_MANIFEST_BYTES))
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError, RecursionError):
        return (), ("旧 evidence manifest 无法读取，未清理历史 evidence。",)
    if not isinstance(document, dict) or document.get("version") != 2:
        return (), ("旧 evidence manifest 版本无效，未清理历史 evidence。",)
    files = document.get("files")
    if not isinstance(files, list) or any(not isinstance(item, dict) for item in files):
        return (), ("旧 evidence manifest 内容无效，未清理历史 evidence。",)
    generated: list[Path] = []
    evidence_directory = destination / "evidence"
    for item in files:
        relative_name = item.get("path")
        expected_digest = item.get("sha256")
        if (
            not isinstance(relative_name, str)
            or not isinstance(expected_digest, str)
            or not re.fullmatch(r"[0-9a-f]{64}", expected_digest)
        ):
            return (), ("旧 evidence manifest 内容无效，未清理历史 evidence。",)
        if "\\" in relative_name:
            return (), ("旧 evidence manifest 包含非法路径，未清理历史 evidence。",)
        relative = PurePosixPath(relative_name)
        if (
            len(relative.parts) != 2
            or relative.parts[0] != "evidence"
            or relative.parts[1] in {"", ".", ".."}
        ):
            return (), ("旧 evidence manifest 包含非法路径，未清理历史 evidence。",)
        candidate = destination.joinpath(*relative.parts)
        if candidate.parent != evidence_directory:
            return (), ("旧 evidence manifest 包含非法路径，未清理历史 evidence。",)
        if candidate.is_symlink():
            raise LabExportError(f"历史 evidence 不能是符号链接：{candidate.name}")
        try:
            current_digest = _file_sha256(candidate)
        except FileNotFoundError:
            continue
        except (OSError, ValueError):
            return (), ("旧 evidence 内容无法安全验证，未清理历史 evidence。",)
        if current_digest == expected_digest:
            generated.append(candidate)
    return tuple(generated), ()


def _write_evidence_manifest(
    manifest: Path,
    rendered: list[Path],
    destination: Path,
) -> None:
    relative_files = [
        {
            "path": path.relative_to(destination).as_posix(),
            "sha256": _file_sha256(path),
        }
        for path in rendered
    ]
    atomic_write_text(
        manifest,
        json.dumps({"version": 2, "files": relative_files}, ensure_ascii=False, indent=2) + "\n",
    )


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open_regular_binary(path) as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()
