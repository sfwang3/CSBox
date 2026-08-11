from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar, Protocol

from csbox.check.models import DetectedProject

MAX_TEXT_SCAN_BYTES = 256 * 1024
PRUNED_DIRECTORIES = frozenset(
    {
        ".git",
        ".venv",
        "node_modules",
        "target",
        "build",
        "dist",
        ".idea",
        "__pycache__",
        ".pytest_cache",
        ".ruff_cache",
        ".mypy_cache",
        ".tox",
        ".csbox",
        "cache",
        "caches",
        "log",
        "logs",
    }
)


@dataclass(frozen=True, slots=True)
class FileEntry:
    relative: Path
    absolute: Path
    size: int


@dataclass(frozen=True, slots=True)
class FileInventory:
    root: Path
    files: tuple[FileEntry, ...]
    directories: tuple[Path, ...]
    scan_limit_bytes: int = MAX_TEXT_SCAN_BYTES

    @classmethod
    def build(
        cls,
        root: Path | str,
        *,
        scan_limit_bytes: int = MAX_TEXT_SCAN_BYTES,
    ) -> FileInventory:
        resolved_root = Path(root).resolve(strict=True)
        if not resolved_root.is_dir():
            raise NotADirectoryError(resolved_root)
        files: list[FileEntry] = []
        directories: list[Path] = []
        pending = [resolved_root]
        while pending:
            directory = pending.pop()
            try:
                children = sorted(os.scandir(directory), key=lambda item: item.name.casefold())
            except OSError:
                continue
            for child in children:
                child_path = Path(child.path)
                relative = child_path.relative_to(resolved_root)
                if child.is_symlink():
                    continue
                if child.is_dir(follow_symlinks=False):
                    directories.append(relative)
                    if child.name.casefold() not in PRUNED_DIRECTORIES:
                        pending.append(child_path)
                    continue
                if not child.is_file(follow_symlinks=False):
                    continue
                try:
                    size = child.stat(follow_symlinks=False).st_size
                except OSError:
                    continue
                files.append(FileEntry(relative, child_path, size))
        files.sort(key=lambda item: item.relative.as_posix().casefold())
        directories.sort(key=lambda item: item.as_posix().casefold())
        return cls(resolved_root, tuple(files), tuple(directories), scan_limit_bytes)

    def has_file(self, name: str) -> bool:
        normalized = name.casefold()
        return any(entry.relative.name.casefold() == normalized for entry in self.files)

    def has_root_file(self, name: str) -> bool:
        normalized = name.casefold()
        return any(
            entry.relative.parent == Path(".") and entry.relative.name.casefold() == normalized
            for entry in self.files
        )

    def has_directory(self, name: str) -> bool:
        normalized = name.casefold()
        return any(path.name.casefold() == normalized for path in self.directories)

    def text(self, entry: FileEntry) -> str | None:
        if entry.size > self.scan_limit_bytes or entry.relative.name == ".env":
            return None
        try:
            data = entry.absolute.read_bytes()
        except (OSError, UnicodeError):
            return None
        if b"\x00" in data:
            return None
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            return None

    def text_files(self):
        for entry in self.files:
            content = self.text(entry)
            if content is not None:
                yield entry, content


class ProjectDetector(Protocol):
    detector_id: ClassVar[str]

    def detect(self, root: Path) -> DetectedProject | None: ...


class NodeDetector:
    detector_id = "node"

    def detect(self, root: Path) -> DetectedProject | None:
        marker = root / "package.json"
        return (
            DetectedProject(kind=self.detector_id, root=root, marker=marker.name)
            if marker.is_file()
            else None
        )


class MavenDetector:
    detector_id = "maven"

    def detect(self, root: Path) -> DetectedProject | None:
        marker = root / "pom.xml"
        return (
            DetectedProject(kind=self.detector_id, root=root, marker=marker.name)
            if marker.is_file()
            else None
        )


class GradleDetector:
    detector_id = "gradle"

    def detect(self, root: Path) -> DetectedProject | None:
        for name in ("build.gradle", "build.gradle.kts", "settings.gradle", "settings.gradle.kts"):
            if (root / name).is_file():
                return DetectedProject(kind=self.detector_id, root=root, marker=name)
        return None


class PythonDetector:
    detector_id = "python"

    def detect(self, root: Path) -> DetectedProject | None:
        for name in ("pyproject.toml", "setup.py", "requirements.txt"):
            if (root / name).is_file():
                return DetectedProject(kind=self.detector_id, root=root, marker=name)
        return None


DEFAULT_DETECTORS: tuple[ProjectDetector, ...] = (
    NodeDetector(),
    MavenDetector(),
    GradleDetector(),
    PythonDetector(),
)


def detect_projects(
    root: Path | str,
    detectors: tuple[ProjectDetector, ...] = DEFAULT_DETECTORS,
) -> tuple[DetectedProject, ...]:
    resolved_root = Path(root).resolve()
    projects = [project for detector in detectors if (project := detector.detect(resolved_root))]
    return tuple(projects)


def node_has_build_script(project: DetectedProject) -> bool:
    try:
        package = json.loads((project.root / project.marker).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    scripts = package.get("scripts") if isinstance(package, dict) else None
    return isinstance(scripts, dict) and isinstance(scripts.get("build"), str)


__all__ = [
    "DEFAULT_DETECTORS",
    "FileEntry",
    "FileInventory",
    "GradleDetector",
    "MavenDetector",
    "NodeDetector",
    "PRUNED_DIRECTORIES",
    "PythonDetector",
    "detect_projects",
    "node_has_build_script",
]
