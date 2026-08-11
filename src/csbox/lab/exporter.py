from __future__ import annotations

import hashlib
import html
import json
import re
import shutil
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path, PurePosixPath

from pydantic import ValidationError

from csbox.lab.captures import CaptureStore
from csbox.lab.models import SessionMetadata, SessionPaths
from csbox.lab.renderer import RenderTheme, TerminalEvidenceRenderer

_COMPONENT_BUDGET = 240
_EVIDENCE_MANIFEST = ".csbox-generated-evidence.json"


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
        self._prepare_destination(output, force=force)
        evidence_directory = output / "evidence"
        self._prepare_evidence_directory(evidence_directory)
        markdown_path = output / "evidence.md"
        cast_path = output / "session.cast"
        commands_path = output / "commands.txt"
        manifest_path = output / _EVIDENCE_MANIFEST
        for path in (markdown_path, cast_path, commands_path, manifest_path):
            _reject_symlink(path)
        previous_evidence, cleanup_warnings = (
            _previous_generated_evidence(output) if force else ((), ())
        )

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
        markdown_path.write_text(markdown, encoding="utf-8", newline="\n")
        shutil.copyfile(paths.cast, cast_path)
        exported_commands: Path | None = None
        if commands:
            commands_path.write_text("\n".join(commands) + "\n", encoding="utf-8", newline="\n")
            exported_commands = commands_path
        elif force and commands_path.is_file():
            commands_path.unlink()
        current_evidence = set(rendered)
        for stale in previous_evidence:
            if stale not in current_evidence and stale.is_file():
                stale.unlink()
        _write_evidence_manifest(manifest_path, rendered, output)

        return LabExportResult(
            destination=output,
            evidence=tuple(rendered),
            markdown=markdown_path,
            cast=cast_path,
            commands=exported_commands,
            warnings=(*capture_result.warnings, *metadata_warnings, *cleanup_warnings),
        )

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
        destination.mkdir(parents=True)

    @staticmethod
    def _prepare_evidence_directory(destination: Path) -> None:
        if destination.is_symlink():
            raise LabExportError("evidence 目录不能是符号链接。")
        if destination.exists() and not destination.is_dir():
            raise LabExportError(f"evidence 路径不是目录：{destination}")
        destination.mkdir(exist_ok=True)


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
        metadata = SessionMetadata.model_validate_json(paths.metadata.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValidationError, ValueError) as exc:
        return None, (f"metadata.json 无效，证据时间回退到 capture createdAt：{exc}",)
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
        document = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return (), (f"旧 evidence manifest 无法读取，未清理历史 evidence：{exc}",)
    if not isinstance(document, dict) or document.get("version") != 1:
        return (), ("旧 evidence manifest 版本无效，未清理历史 evidence。",)
    files = document.get("files")
    if not isinstance(files, list) or any(not isinstance(item, str) for item in files):
        return (), ("旧 evidence manifest 内容无效，未清理历史 evidence。",)
    generated: list[Path] = []
    evidence_directory = destination / "evidence"
    for relative_name in files:
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
        generated.append(candidate)
    return tuple(generated), ()


def _write_evidence_manifest(
    manifest: Path,
    rendered: list[Path],
    destination: Path,
) -> None:
    relative_files = [path.relative_to(destination).as_posix() for path in rendered]
    manifest.write_text(
        json.dumps({"version": 1, "files": relative_files}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
