from __future__ import annotations

import json
import os
import stat
import tomllib
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO, ClassVar, Protocol

from csbox.check.models import DetectedProject

MAX_TEXT_SCAN_BYTES = 256 * 1024
DEFAULT_TEXT_CACHE_LIMIT_BYTES = 8 * 1024 * 1024
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
    file_type: str = "regular"
    is_text_candidate: bool = True
    device: int | None = None
    inode: int | None = None
    mtime_ns: int | None = None
    ctime_ns: int | None = None

    @property
    def is_regular(self) -> bool:
        return self.file_type == "regular"


@dataclass(frozen=True, slots=True)
class InventoryExclusion:
    relative: Path
    reason: str
    file_type: str


@dataclass(frozen=True, slots=True)
class TextScanStats:
    requested: int = 0
    cache_hits: int = 0
    read_count: int = 0
    bytes_read: int = 0
    scanned: int = 0
    cached_entries: int = 0
    cached_bytes: int = 0
    skipped_large: int = 0
    skipped_budget: int = 0
    skipped_nul: int = 0
    skipped_binary: int = 0
    skipped_decode: int = 0
    skipped_sensitive: int = 0
    skipped_unreadable: int = 0
    skipped_special: int = 0

    @property
    def files_read(self) -> int:
        return self.read_count

    @property
    def cache_hit_count(self) -> int:
        return self.cache_hits

    def as_dict(self) -> dict[str, int]:
        return {
            "requested": self.requested,
            "cache_hits": self.cache_hits,
            "read_count": self.read_count,
            "bytes_read": self.bytes_read,
            "scanned": self.scanned,
            "cached_entries": self.cached_entries,
            "cached_bytes": self.cached_bytes,
            "skipped_large": self.skipped_large,
            "skipped_budget": self.skipped_budget,
            "skipped_nul": self.skipped_nul,
            "skipped_binary": self.skipped_binary,
            "skipped_decode": self.skipped_decode,
            "skipped_sensitive": self.skipped_sensitive,
            "skipped_unreadable": self.skipped_unreadable,
            "skipped_special": self.skipped_special,
        }


@dataclass(slots=True)
class _TextScanCounters:
    requested: int = 0
    cache_hits: int = 0
    read_count: int = 0
    bytes_read: int = 0
    scanned: int = 0
    skipped_large: int = 0
    skipped_budget: int = 0
    skipped_nul: int = 0
    skipped_binary: int = 0
    skipped_decode: int = 0
    skipped_sensitive: int = 0
    skipped_unreadable: int = 0
    skipped_special: int = 0

    def snapshot(self, *, cached_entries: int, cached_bytes: int) -> TextScanStats:
        return TextScanStats(
            requested=self.requested,
            cache_hits=self.cache_hits,
            read_count=self.read_count,
            bytes_read=self.bytes_read,
            scanned=self.scanned,
            cached_entries=cached_entries,
            cached_bytes=cached_bytes,
            skipped_large=self.skipped_large,
            skipped_budget=self.skipped_budget,
            skipped_nul=self.skipped_nul,
            skipped_binary=self.skipped_binary,
            skipped_decode=self.skipped_decode,
            skipped_sensitive=self.skipped_sensitive,
            skipped_unreadable=self.skipped_unreadable,
            skipped_special=self.skipped_special,
        )


def _iter_directory_entries(directory: Path) -> Iterator[os.DirEntry[str]]:
    """Yield entries from a directory opened without following its final symlink."""
    file_descriptor: int | None = None
    if os.name == "posix" and hasattr(os, "O_DIRECTORY") and hasattr(os, "O_NOFOLLOW"):
        try:
            flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            if hasattr(os, "O_CLOEXEC"):
                flags |= os.O_CLOEXEC
            file_descriptor = os.open(directory, flags)
            with os.scandir(file_descriptor) as iterator:
                yield from sorted(iterator, key=lambda item: (item.name.casefold(), item.name))
        except OSError:
            return
        finally:
            if file_descriptor is not None:
                with suppress(OSError):
                    os.close(file_descriptor)
        return

    try:
        if directory.is_symlink():
            return
        with os.scandir(directory) as iterator:
            yield from sorted(iterator, key=lambda item: (item.name.casefold(), item.name))
    except OSError:
        return


@dataclass(frozen=True, slots=True)
class FileInventory:
    root: Path
    files: tuple[FileEntry, ...]
    directories: tuple[Path, ...]
    scan_limit_bytes: int = MAX_TEXT_SCAN_BYTES
    text_cache_limit_bytes: int = DEFAULT_TEXT_CACHE_LIMIT_BYTES
    excluded_entries: tuple[InventoryExclusion, ...] = ()
    _text_cache: dict[Path, str | None] = field(default_factory=dict, init=False, repr=False)
    _text_cache_bytes: int = field(default=0, init=False, repr=False)
    _stats: _TextScanCounters = field(default_factory=_TextScanCounters, init=False, repr=False)
    _entries_by_relative: dict[Path, FileEntry] = field(
        default_factory=dict, init=False, repr=False
    )
    _files_by_name: dict[str, tuple[FileEntry, ...]] = field(
        default_factory=dict, init=False, repr=False
    )
    _files_by_parent: dict[Path, tuple[FileEntry, ...]] = field(
        default_factory=dict, init=False, repr=False
    )
    _directories_by_name: dict[str, tuple[Path, ...]] = field(
        default_factory=dict, init=False, repr=False
    )

    def __post_init__(self) -> None:
        if self.scan_limit_bytes < 1:
            raise ValueError("scan_limit_bytes must be positive")
        if self.text_cache_limit_bytes < 1:
            raise ValueError("text_cache_limit_bytes must be positive")
        object.__setattr__(
            self,
            "_entries_by_relative",
            {entry.relative: entry for entry in self.files},
        )
        files_by_name: dict[str, list[FileEntry]] = {}
        for entry in self.files:
            files_by_name.setdefault(entry.relative.name.casefold(), []).append(entry)
        object.__setattr__(
            self,
            "_files_by_name",
            {name: tuple(entries) for name, entries in files_by_name.items()},
        )
        files_by_parent: dict[Path, list[FileEntry]] = {}
        for entry in self.files:
            files_by_parent.setdefault(entry.relative.parent, []).append(entry)
        object.__setattr__(
            self,
            "_files_by_parent",
            {parent: tuple(entries) for parent, entries in files_by_parent.items()},
        )
        directories_by_name: dict[str, list[Path]] = {}
        for path in self.directories:
            directories_by_name.setdefault(path.name.casefold(), []).append(path)
        object.__setattr__(
            self,
            "_directories_by_name",
            {name: tuple(paths) for name, paths in directories_by_name.items()},
        )

    @classmethod
    def build(
        cls,
        root: Path | str,
        *,
        scan_limit_bytes: int = MAX_TEXT_SCAN_BYTES,
        text_cache_limit_bytes: int = DEFAULT_TEXT_CACHE_LIMIT_BYTES,
    ) -> FileInventory:
        resolved_root = Path(root).resolve(strict=True)
        if not resolved_root.is_dir():
            raise NotADirectoryError(resolved_root)
        if scan_limit_bytes < 1:
            raise ValueError("scan_limit_bytes must be positive")
        if text_cache_limit_bytes < 1:
            raise ValueError("text_cache_limit_bytes must be positive")

        files: list[FileEntry] = []
        directories: list[Path] = []
        excluded_entries: list[InventoryExclusion] = []
        pending = [resolved_root]
        while pending:
            directory = pending.pop()
            for child in _iter_directory_entries(directory):
                child_path = directory / child.name
                try:
                    relative = child_path.relative_to(resolved_root)
                    if child.is_symlink():
                        excluded_entries.append(InventoryExclusion(relative, "symlink", "symlink"))
                        continue
                    if child.is_dir(follow_symlinks=False):
                        directories.append(relative)
                        if child.name.casefold() not in PRUNED_DIRECTORIES:
                            pending.append(child_path)
                        else:
                            excluded_entries.append(
                                InventoryExclusion(relative, "directory", "directory")
                            )
                        continue
                    if not child.is_file(follow_symlinks=False):
                        excluded_entries.append(InventoryExclusion(relative, "special", "special"))
                        continue
                    metadata = child.stat(follow_symlinks=False)
                    if not stat.S_ISREG(metadata.st_mode):
                        continue
                    files.append(
                        FileEntry(
                            relative=relative,
                            absolute=child_path,
                            size=metadata.st_size,
                            is_text_candidate=(
                                metadata.st_size <= scan_limit_bytes
                                and child.name.casefold() != ".env"
                            ),
                            device=metadata.st_dev or None,
                            inode=metadata.st_ino or None,
                            mtime_ns=getattr(metadata, "st_mtime_ns", None),
                            ctime_ns=getattr(metadata, "st_ctime_ns", None),
                        )
                    )
                except (OSError, ValueError):
                    continue

        files.sort(key=lambda item: (item.relative.as_posix().casefold(), item.relative.as_posix()))
        directories.sort(key=lambda item: (item.as_posix().casefold(), item.as_posix()))
        excluded_entries.sort(
            key=lambda item: (
                item.relative.as_posix().casefold(),
                item.relative.as_posix(),
                item.reason,
            )
        )
        return cls(
            root=resolved_root,
            files=tuple(files),
            directories=tuple(directories),
            excluded_entries=tuple(excluded_entries),
            scan_limit_bytes=scan_limit_bytes,
            text_cache_limit_bytes=text_cache_limit_bytes,
        )

    @property
    def text_scan_stats(self) -> TextScanStats:
        return self._stats.snapshot(
            cached_entries=len(self._text_cache),
            cached_bytes=self._text_cache_bytes,
        )

    def has_file(self, name: str) -> bool:
        return bool(self._files_by_name.get(name.casefold()))

    def has_root_file(self, name: str) -> bool:
        normalized = name.casefold()
        return any(
            entry.relative.parent == Path(".") for entry in self._files_by_name.get(normalized, ())
        )

    def has_directory(self, name: str) -> bool:
        return bool(self._directories_by_name.get(name.casefold()))

    def files_named(self, name: str) -> tuple[FileEntry, ...]:
        return self._files_by_name.get(name.casefold(), ())

    def files_in_directory(self, relative: Path | str) -> tuple[FileEntry, ...]:
        return self._files_by_parent.get(Path(relative), ())

    def entry(self, relative: Path | str) -> FileEntry | None:
        return self._entries_by_relative.get(Path(relative))

    def text(self, entry: FileEntry) -> str | None:
        self._stats.requested += 1
        cached = self._text_cache.get(entry.relative, _MISSING)
        if cached is not _MISSING:
            self._stats.cache_hits += 1
            return cached

        if not self._is_owned_entry(entry):
            self._stats.skipped_unreadable += 1
            self._text_cache[entry.relative] = None
            return None
        if entry.size > self.scan_limit_bytes:
            self._stats.skipped_large += 1
            self._text_cache[entry.relative] = None
            return None
        if entry.relative.name.casefold() == ".env":
            self._stats.skipped_sensitive += 1
            self._text_cache[entry.relative] = None
            return None
        if self._path_has_symlink(entry.relative):
            self._stats.skipped_unreadable += 1
            self._text_cache[entry.relative] = None
            return None

        file_descriptor: int | None = None
        try:
            flags = os.O_RDONLY
            if hasattr(os, "O_NONBLOCK"):
                flags |= os.O_NONBLOCK
            if hasattr(os, "O_CLOEXEC"):
                flags |= os.O_CLOEXEC
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            file_descriptor = self._open_entry(entry.relative, flags)
            metadata = os.fstat(file_descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                self._stats.skipped_special += 1
                self._text_cache[entry.relative] = None
                return None
            with os.fdopen(file_descriptor, "rb") as stream:
                file_descriptor = None
                data = stream.read(self.scan_limit_bytes + 1)
            self._stats.read_count += 1
            self._stats.bytes_read += len(data)
        except (OSError, ValueError):
            self._stats.skipped_unreadable += 1
            self._text_cache[entry.relative] = None
            return None
        finally:
            if file_descriptor is not None:
                with suppress(OSError):
                    os.close(file_descriptor)

        if os.name != "posix" and not self._entry_is_stable(entry, metadata):
            self._stats.skipped_unreadable += 1
            self._text_cache[entry.relative] = None
            return None

        if len(data) > self.scan_limit_bytes:
            self._stats.skipped_large += 1
            self._text_cache[entry.relative] = None
            return None
        self._stats.scanned += 1
        if b"\x00" in data:
            self._stats.skipped_nul += 1
            self._text_cache[entry.relative] = None
            return None
        try:
            content = data.decode("utf-8")
        except UnicodeDecodeError:
            self._stats.skipped_binary += 1
            self._stats.skipped_decode += 1
            self._text_cache[entry.relative] = None
            return None
        if self._text_cache_bytes + len(data) > self.text_cache_limit_bytes:
            self._stats.skipped_budget += 1
            # The budget bounds retained memory, not security coverage.  Return
            # this successfully scanned text without caching it so later rules
            # can safely re-read it instead of receiving a false cache miss.
            return content
        self._text_cache[entry.relative] = content
        object.__setattr__(self, "_text_cache_bytes", self._text_cache_bytes + len(data))
        return content

    def _open_entry(self, relative: Path, flags: int) -> int:
        if os.name != "posix" or not (hasattr(os, "O_DIRECTORY") and hasattr(os, "O_NOFOLLOW")):
            return os.open(self.root.joinpath(*relative.parts), flags)

        directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        if hasattr(os, "O_CLOEXEC"):
            directory_flags |= os.O_CLOEXEC
        parent_fd = os.open(self.root, directory_flags)
        try:
            parts = relative.parts
            for part in parts[:-1]:
                next_fd = os.open(part, directory_flags, dir_fd=parent_fd)
                os.close(parent_fd)
                parent_fd = next_fd
            return os.open(parts[-1], flags, dir_fd=parent_fd)
        finally:
            with suppress(OSError):
                os.close(parent_fd)

    def text_files(self) -> Iterator[tuple[FileEntry, str]]:
        for entry in self.files:
            content = self.text(entry)
            if content is not None:
                yield entry, content

    def contains_markers(self, entry: FileEntry, markers: tuple[str, ...]) -> bool:
        """Search an owned file for ASCII markers without retaining its contents."""
        if not markers or not self._is_owned_entry(entry):
            return False
        encoded = tuple(marker.encode("ascii") for marker in markers)
        if entry.size <= self.scan_limit_bytes:
            content = self.text(entry)
            if content is not None:
                return any(marker in content for marker in markers)

        overlap = b""
        window = max(len(marker) for marker in encoded) - 1
        try:
            with self.open_entry(entry) as stream:
                while chunk := stream.read(1024 * 1024):
                    data = overlap + chunk
                    if any(marker in data for marker in encoded):
                        return True
                    overlap = data[-window:] if window else b""
        except (OSError, ValueError):
            return False
        return False

    @contextmanager
    def open_entry(self, entry: FileEntry) -> Iterator[BinaryIO]:
        """Open an inventory file without following a replacement symlink."""
        if not self._is_owned_entry(entry):
            raise ValueError("inventory entry is not owned by this root")
        if self._path_has_symlink(entry.relative):
            raise ValueError("inventory entry has a symlink parent")

        flags = os.O_RDONLY
        if hasattr(os, "O_NONBLOCK"):
            flags |= os.O_NONBLOCK
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        file_descriptor = self._open_entry(entry.relative, flags)
        try:
            if self._path_has_symlink(entry.relative):
                raise ValueError("inventory entry was replaced by a symlink")
            opened = os.fstat(file_descriptor)
            if not stat.S_ISREG(opened.st_mode):
                raise ValueError("inventory entry is not a regular file")
            if not self._matches_entry_metadata(entry, opened):
                raise ValueError("inventory entry changed before copy")
            with os.fdopen(file_descriptor, "rb", closefd=False) as stream:
                yield stream
            if self._path_has_symlink(entry.relative):
                raise ValueError("inventory entry was replaced by a symlink")
            current = os.fstat(file_descriptor)
            if not self._matches_entry_metadata(entry, current):
                raise ValueError("inventory entry changed during copy")
        finally:
            with suppress(OSError):
                os.close(file_descriptor)

    def _is_owned_entry(self, entry: FileEntry) -> bool:
        known = self._entries_by_relative.get(entry.relative)
        if known != entry or entry.relative.is_absolute() or ".." in entry.relative.parts:
            return False
        return entry.absolute == self.root.joinpath(*entry.relative.parts)

    def _path_has_symlink(self, relative: Path) -> bool:
        current = self.root
        for part in relative.parts:
            current /= part
            try:
                if current.is_symlink():
                    return True
            except OSError:
                return True
        return False

    def _entry_is_stable(self, entry: FileEntry, opened: os.stat_result) -> bool:
        if self._path_has_symlink(entry.relative):
            return False
        try:
            current = os.stat(entry.absolute, follow_symlinks=False)
        except OSError:
            return False
        if not stat.S_ISREG(current.st_mode):
            return False
        if entry.device is not None and current.st_dev != entry.device:
            return False
        if entry.inode is not None and current.st_ino != entry.inode:
            return False
        if entry.mtime_ns is not None and current.st_mtime_ns != entry.mtime_ns:
            return False
        if entry.ctime_ns is not None and current.st_ctime_ns != entry.ctime_ns:
            return False
        return current.st_dev == opened.st_dev and current.st_ino == opened.st_ino

    @staticmethod
    def _matches_entry_metadata(entry: FileEntry, metadata: os.stat_result) -> bool:
        if entry.device is not None and metadata.st_dev != entry.device:
            return False
        if entry.inode is not None and metadata.st_ino != entry.inode:
            return False
        if entry.mtime_ns is not None and metadata.st_mtime_ns != entry.mtime_ns:
            return False
        if entry.ctime_ns is not None and metadata.st_ctime_ns != entry.ctime_ns:
            return False
        return metadata.st_size == entry.size


_MISSING = object()


class ProjectDetector(Protocol):
    detector_id: ClassVar[str]

    def detect(self, root: Path) -> DetectedProject | None: ...

    def detect_inventory(self, inventory: FileInventory) -> tuple[DetectedProject, ...]: ...


def _project_root(inventory: FileInventory, entry: FileEntry) -> Path:
    return inventory.root / entry.relative.parent


def _root_project(
    detector: ProjectDetector,
    root: Path,
) -> DetectedProject | None:
    inventory = FileInventory.build(root)
    for project in detector.detect_inventory(inventory):
        if project.root == inventory.root:
            return project
    return None


def _marker_groups(
    inventory: FileInventory,
    names: tuple[str, ...],
) -> dict[Path, list[FileEntry]]:
    normalized = {name.casefold() for name in names}
    groups: dict[Path, list[FileEntry]] = {}
    for entry in inventory.files:
        if entry.relative.name.casefold() in normalized:
            groups.setdefault(entry.relative.parent, []).append(entry)
    return groups


def _select_marker(entries: list[FileEntry], priority: tuple[str, ...]) -> FileEntry:
    rank = {name.casefold(): index for index, name in enumerate(priority)}
    return min(
        entries,
        key=lambda entry: (
            rank.get(entry.relative.name.casefold(), len(rank)),
            entry.relative.name,
        ),
    )


def _sibling_names(inventory: FileInventory, parent: Path) -> set[str]:
    return {entry.relative.name.casefold() for entry in inventory.files_in_directory(parent)}


def _manager_from_manifest(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    name = value.split("@", 1)[0].casefold()
    return name if name in {"npm", "pnpm", "yarn"} else None


def _node_manager(inventory: FileInventory, entry: FileEntry) -> str | None:
    sibling_names = _sibling_names(inventory, entry.relative.parent)
    lock_managers = {
        name
        for name, marker in (
            ("npm", "package-lock.json"),
            ("npm", "npm-shrinkwrap.json"),
            ("pnpm", "pnpm-lock.yaml"),
            ("yarn", "yarn.lock"),
        )
        if marker in sibling_names
    }
    manifest_manager: str | None = None
    content = inventory.text(entry)
    if content is not None:
        try:
            package = json.loads(content)
        except (json.JSONDecodeError, TypeError, RecursionError):
            package = None
        if isinstance(package, dict):
            manifest_manager = _manager_from_manifest(package.get("packageManager"))
    if len(lock_managers) == 1:
        manager = next(iter(lock_managers))
        return manager if manifest_manager in {None, manager} else None
    if len(lock_managers) > 1:
        return manifest_manager if manifest_manager in lock_managers else None
    return manifest_manager


def _python_manager(inventory: FileInventory, parent: Path) -> str | None:
    sibling_names = _sibling_names(inventory, parent)
    lock_managers = {
        manager
        for manager, marker in (("uv", "uv.lock"), ("poetry", "poetry.lock"))
        if marker in sibling_names
    }
    manifest_manager: str | None = None
    pyproject = next(
        (
            entry
            for entry in inventory.files_in_directory(parent)
            if entry.relative.name.casefold() == "pyproject.toml"
        ),
        None,
    )
    if pyproject is not None:
        content = inventory.text(pyproject)
        if content is not None:
            try:
                document = tomllib.loads(content)
            except (tomllib.TOMLDecodeError, TypeError, RecursionError):
                document = {}
            tools = document.get("tool") if isinstance(document, dict) else None
            if isinstance(tools, dict):
                if isinstance(tools.get("poetry"), dict):
                    manifest_manager = "poetry"
                elif isinstance(tools.get("uv"), dict):
                    manifest_manager = "uv"
    if len(lock_managers) == 1:
        manager = next(iter(lock_managers))
        return manager if manifest_manager in {None, manager} else None
    if len(lock_managers) > 1:
        return manifest_manager if manifest_manager in lock_managers else None
    return manifest_manager


class NodeDetector:
    detector_id = "node"

    def detect(self, root: Path) -> DetectedProject | None:
        return _root_project(self, Path(root))

    def detect_inventory(self, inventory: FileInventory) -> tuple[DetectedProject, ...]:
        projects: list[DetectedProject] = []
        for entry in inventory.files_named("package.json"):
            projects.append(
                DetectedProject(
                    kind=self.detector_id,
                    root=_project_root(inventory, entry),
                    marker=entry.relative.name,
                    package_manager=_node_manager(inventory, entry),
                )
            )
        return tuple(projects)


class MavenDetector:
    detector_id = "maven"

    def detect(self, root: Path) -> DetectedProject | None:
        return _root_project(self, Path(root))

    def detect_inventory(self, inventory: FileInventory) -> tuple[DetectedProject, ...]:
        groups = _marker_groups(inventory, ("pom.xml",))
        return tuple(
            DetectedProject(
                kind=self.detector_id,
                root=inventory.root / parent,
                marker=_select_marker(entries, ("pom.xml",)).relative.name,
            )
            for parent, entries in sorted(groups.items(), key=_path_sort_key)
        )


class GradleDetector:
    detector_id = "gradle"
    marker_priority = ("build.gradle", "build.gradle.kts", "settings.gradle", "settings.gradle.kts")

    def detect(self, root: Path) -> DetectedProject | None:
        return _root_project(self, Path(root))

    def detect_inventory(self, inventory: FileInventory) -> tuple[DetectedProject, ...]:
        groups = _marker_groups(inventory, self.marker_priority)
        return tuple(
            DetectedProject(
                kind=self.detector_id,
                root=inventory.root / parent,
                marker=_select_marker(entries, self.marker_priority).relative.name,
            )
            for parent, entries in sorted(groups.items(), key=_path_sort_key)
        )


class PythonDetector:
    detector_id = "python"
    marker_priority = ("pyproject.toml", "setup.py", "requirements.txt", "uv.lock", "poetry.lock")

    def detect(self, root: Path) -> DetectedProject | None:
        return _root_project(self, Path(root))

    def detect_inventory(self, inventory: FileInventory) -> tuple[DetectedProject, ...]:
        groups = _marker_groups(inventory, self.marker_priority)
        return tuple(
            DetectedProject(
                kind=self.detector_id,
                root=inventory.root / parent,
                marker=_select_marker(entries, self.marker_priority).relative.name,
                package_manager=_python_manager(inventory, parent),
            )
            for parent, entries in sorted(groups.items(), key=_path_sort_key)
        )


def _path_sort_key(item: tuple[Path, object]) -> tuple[str, str]:
    path = item[0].as_posix()
    return path.casefold(), path


DEFAULT_DETECTORS: tuple[ProjectDetector, ...] = (
    NodeDetector(),
    MavenDetector(),
    GradleDetector(),
    PythonDetector(),
)


def detect_projects(
    root: Path | str,
    detectors: tuple[ProjectDetector, ...] = DEFAULT_DETECTORS,
    *,
    inventory: FileInventory | None = None,
) -> tuple[DetectedProject, ...]:
    resolved_root = Path(root).resolve(strict=True)
    current_inventory = inventory or FileInventory.build(resolved_root)
    if current_inventory.root != resolved_root:
        raise ValueError("inventory root does not match project root")
    projects: list[tuple[int, DetectedProject]] = []
    for detector_index, detector in enumerate(detectors):
        detect_inventory = getattr(detector, "detect_inventory", None)
        if callable(detect_inventory):
            detected = detect_inventory(current_inventory)
        else:
            detected_one = detector.detect(resolved_root)
            detected = () if detected_one is None else (detected_one,)
        projects.extend((detector_index, project) for project in detected)

    unique: dict[tuple[str, Path], tuple[int, DetectedProject]] = {}
    for detector_index, project in projects:
        key = (project.kind, project.root)
        unique.setdefault(key, (detector_index, project))
    ordered = sorted(
        unique.values(),
        key=lambda item: (
            _relative_project_key(resolved_root, item[1].root),
            item[0],
            item[1].marker.casefold(),
            item[1].marker,
        ),
    )
    return tuple(project for _, project in ordered)


def _relative_project_key(root: Path, project: Path) -> tuple[str, str]:
    try:
        relative = project.relative_to(root).as_posix()
    except ValueError:
        relative = project.name
    return relative.casefold(), relative


def node_has_build_script(
    project: DetectedProject,
    inventory: FileInventory | None = None,
) -> bool:
    if inventory is not None:
        try:
            relative_marker = Path(project.root).relative_to(inventory.root) / project.marker
        except ValueError:
            return False
        entry = inventory.entry(relative_marker)
        return entry is not None and _package_has_build_script(inventory.text(entry))

    path = project.root / project.marker
    if path.is_symlink():
        return False
    file_descriptor: int | None = None
    try:
        metadata = path.stat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > MAX_TEXT_SCAN_BYTES:
            return False
        flags = os.O_RDONLY
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        file_descriptor = os.open(path, flags)
        with os.fdopen(file_descriptor, "rb") as stream:
            file_descriptor = None
            data = stream.read(MAX_TEXT_SCAN_BYTES + 1)
        if len(data) > MAX_TEXT_SCAN_BYTES or b"\x00" in data:
            return False
        content = data.decode("utf-8")
    except (OSError, UnicodeError):
        return False
    finally:
        if file_descriptor is not None:
            with suppress(OSError):
                os.close(file_descriptor)
    return _package_has_build_script(content)


def _package_has_build_script(content: str | None) -> bool:
    if content is None:
        return False
    try:
        package = json.loads(content)
    except (UnicodeError, json.JSONDecodeError, TypeError, RecursionError):
        return False
    scripts = package.get("scripts") if isinstance(package, dict) else None
    return isinstance(scripts, dict) and isinstance(scripts.get("build"), str)


__all__ = [
    "DEFAULT_DETECTORS",
    "DEFAULT_TEXT_CACHE_LIMIT_BYTES",
    "FileEntry",
    "FileInventory",
    "GradleDetector",
    "MavenDetector",
    "MAX_TEXT_SCAN_BYTES",
    "InventoryExclusion",
    "PRUNED_DIRECTORIES",
    "PythonDetector",
    "ProjectDetector",
    "TextScanStats",
    "NodeDetector",
    "detect_projects",
    "node_has_build_script",
]
