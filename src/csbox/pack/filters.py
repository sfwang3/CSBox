from __future__ import annotations

import fnmatch
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath

from csbox.check.detectors import (
    PRUNED_DIRECTORIES,
    FileEntry,
    FileInventory,
)
from csbox.core.safe_paths import safe_relative_path


class PackSafetyError(RuntimeError):
    """The source contains a file that must not enter a delivery archive."""


DEFAULT_EXCLUDED_DIRECTORIES = frozenset(PRUNED_DIRECTORIES | {".cache", ".vscode"})
DEFAULT_EXCLUDED_FILES = frozenset({".env", ".coverage", ".DS_Store", "Thumbs.db"})
ARCHIVE_SUFFIXES = frozenset({".zip"})
PRIVATE_KEY_MARKERS = (
    "-----BEGIN PRIVATE KEY-----",
    "-----BEGIN RSA PRIVATE KEY-----",
    "-----BEGIN OPENSSH PRIVATE KEY-----",
    "-----BEGIN EC PRIVATE KEY-----",
)


@dataclass(frozen=True, slots=True)
class PackCandidate:
    relative: Path
    absolute: Path
    size: int
    entry: FileEntry | None = None


@dataclass(frozen=True, slots=True)
class PackSelection:
    candidates: tuple[PackCandidate, ...]
    excluded: tuple[str, ...]
    rejected: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PackFilter:
    root: Path
    include: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()
    excluded_directories: frozenset[str] = DEFAULT_EXCLUDED_DIRECTORIES
    inventory: FileInventory | None = None

    def __post_init__(self) -> None:
        resolved = Path(self.root).resolve(strict=True)
        if not resolved.is_dir():
            raise NotADirectoryError(resolved)
        current_inventory = self.inventory or FileInventory.build(resolved)
        if current_inventory.root != resolved:
            raise ValueError("inventory root does not match pack root")
        object.__setattr__(self, "root", resolved)
        object.__setattr__(self, "inventory", current_inventory)
        for pattern in (*self.include, *self.exclude):
            _validate_pattern(pattern)

    def select(self) -> PackSelection:
        assert self.inventory is not None
        excluded = [
            f"{item.relative.as_posix()}:{item.reason}" for item in self.inventory.excluded_entries
        ]
        excluded_paths = {item.relative.as_posix() for item in self.inventory.excluded_entries}
        directory_names = {name.casefold() for name in self.excluded_directories}
        for directory in self.inventory.directories:
            if (
                directory.name.casefold() in directory_names
                and directory.as_posix() not in excluded_paths
            ):
                excluded.append(f"{directory.as_posix()}:directory")
        rejected: list[str] = []
        candidates: list[PackCandidate] = []

        for entry in self.inventory.files:
            relative = entry.relative
            value = relative.as_posix()
            try:
                safe_relative_path(value)
            except ValueError:
                rejected.append(f"{value}:unsafe-path")
                continue

            if relative.suffix.casefold() in ARCHIVE_SUFFIXES:
                excluded.append(f"{value}:archive")
                continue

            sensitive_reason = self._sensitive_reason(entry)
            if sensitive_reason is not None:
                rejected.append(f"{value}:{sensitive_reason}")
                continue
            if any(parent.name.casefold() in directory_names for parent in relative.parents):
                continue
            if self._excluded(relative):
                excluded.append(f"{value}:{self._exclude_reason(relative)}")
                continue
            candidates.append(PackCandidate(relative, entry.absolute, entry.size, entry))

        candidates.sort(
            key=lambda item: (item.relative.as_posix().casefold(), item.relative.as_posix())
        )
        return PackSelection(
            candidates=tuple(candidates),
            excluded=tuple(sorted(set(excluded), key=lambda item: (item.casefold(), item))),
            rejected=tuple(sorted(set(rejected), key=lambda item: (item.casefold(), item))),
        )

    def candidates(self) -> tuple[PackCandidate, ...]:
        selection = self.select()
        if selection.rejected:
            raise PackSafetyError("拒绝打包敏感或不安全文件。")
        return selection.candidates

    def exclusions(self) -> tuple[str, ...]:
        return self.select().excluded

    def rejections(self) -> tuple[str, ...]:
        return self.select().rejected

    def _excluded(self, relative: Path) -> bool:
        value = relative.as_posix()
        if relative.name.casefold() in {name.casefold() for name in DEFAULT_EXCLUDED_FILES}:
            return True
        if relative.suffix.casefold() in ARCHIVE_SUFFIXES:
            return True
        if relative.suffix.casefold() == ".log":
            return True
        if self.include and not any(_matches(value, pattern) for pattern in self.include):
            return True
        return any(_matches(value, pattern) for pattern in self.exclude)

    def _exclude_reason(self, relative: Path) -> str:
        if relative.name.casefold() in {name.casefold() for name in DEFAULT_EXCLUDED_FILES}:
            return "pattern"
        if relative.suffix.casefold() in ARCHIVE_SUFFIXES:
            return "archive"
        if relative.suffix.casefold() == ".log":
            return "pattern"
        if self.include and not any(
            _matches(relative.as_posix(), pattern) for pattern in self.include
        ):
            return "include"
        return "pattern"

    def _sensitive_reason(self, entry: FileEntry) -> str | None:
        name = entry.relative.name.casefold()
        if name == ".env" or (name.startswith(".env.") and name != ".env.example"):
            return "env"

        likely_key = name.endswith((".pem", ".key", ".ppk")) or name in {
            "id_rsa",
            "id_ed25519",
            "id_ecdsa",
        }
        if likely_key:
            return "private-key"
        if self.inventory is not None and self.inventory.contains_markers(
            entry, PRIVATE_KEY_MARKERS
        ):
            return "private-key"
        return None


def _validate_pattern(pattern: str) -> None:
    if not pattern or "\x00" in pattern or Path(pattern).is_absolute():
        raise ValueError("pack patterns must be relative")
    windows = PureWindowsPath(pattern)
    if windows.is_absolute() or any(part == ".." for part in windows.parts):
        raise ValueError("pack patterns must not escape the source root")
    if any(part == ".." for part in PurePosixPath(pattern).parts):
        raise ValueError("pack patterns must not escape the source root")


def _matches(value: str, pattern: str) -> bool:
    normalized = pattern.replace("\\", "/")
    return fnmatch.fnmatchcase(value, normalized) or fnmatch.fnmatchcase(
        value,
        f"*/{normalized}",
    )


__all__ = [
    "DEFAULT_EXCLUDED_DIRECTORIES",
    "PackCandidate",
    "PackFilter",
    "PackSafetyError",
    "PackSelection",
]
