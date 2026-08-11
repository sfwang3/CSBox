from __future__ import annotations

import fnmatch
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath


class PackSafetyError(RuntimeError):
    """The source contains a file that must not enter a delivery archive."""


DEFAULT_EXCLUDED_DIRECTORIES = frozenset(
    {
        ".git",
        ".venv",
        "node_modules",
        "target",
        "build",
        "dist",
        "__pycache__",
        ".pytest_cache",
        ".ruff_cache",
        ".mypy_cache",
        ".idea",
        ".vscode",
        ".csbox",
        "cache",
        "caches",
        "log",
        "logs",
    }
)
DEFAULT_EXCLUDED_FILES = frozenset({".env"})
PRIVATE_KEY_MARKERS = (
    b"-----BEGIN PRIVATE KEY-----",
    b"-----BEGIN RSA PRIVATE KEY-----",
    b"-----BEGIN OPENSSH PRIVATE KEY-----",
    b"-----BEGIN EC PRIVATE KEY-----",
)


@dataclass(frozen=True, slots=True)
class PackCandidate:
    relative: Path
    absolute: Path
    size: int


@dataclass(frozen=True, slots=True)
class PackFilter:
    root: Path
    include: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()
    excluded_directories: frozenset[str] = DEFAULT_EXCLUDED_DIRECTORIES

    def __post_init__(self) -> None:
        resolved = Path(self.root).resolve(strict=True)
        if not resolved.is_dir():
            raise NotADirectoryError(resolved)
        object.__setattr__(self, "root", resolved)
        for pattern in (*self.include, *self.exclude):
            _validate_pattern(pattern)

    def candidates(self) -> tuple[PackCandidate, ...]:
        candidates: list[PackCandidate] = []
        pending = [self.root]
        while pending:
            directory = pending.pop()
            try:
                children = sorted(directory.iterdir(), key=lambda item: item.name.casefold())
            except OSError:
                continue
            for child in children:
                relative = child.relative_to(self.root)
                if child.is_symlink():
                    continue
                if child.is_dir():
                    if child.name.casefold() in {
                        name.casefold() for name in self.excluded_directories
                    }:
                        continue
                    pending.append(child)
                    continue
                if not child.is_file():
                    continue
                if self._is_sensitive(child, relative):
                    raise PackSafetyError(f"拒绝打包敏感文件：{relative}")
                if self._excluded(relative):
                    continue
                try:
                    size = child.stat().st_size
                except OSError:
                    continue
                candidates.append(PackCandidate(relative, child, size))
        candidates.sort(key=lambda item: item.relative.as_posix().casefold())
        return tuple(candidates)

    def exclusions(self) -> tuple[str, ...]:
        excluded: list[str] = []
        pending = [self.root]
        while pending:
            directory = pending.pop()
            try:
                children = sorted(directory.iterdir(), key=lambda item: item.name.casefold())
            except OSError:
                continue
            for child in children:
                relative = child.relative_to(self.root)
                if child.is_symlink():
                    excluded.append(f"{relative}:symlink")
                    continue
                if child.is_dir():
                    if child.name.casefold() in {
                        name.casefold() for name in self.excluded_directories
                    }:
                        excluded.append(f"{relative}:directory")
                        continue
                    pending.append(child)
                elif child.is_file() and self._excluded(relative):
                    excluded.append(f"{relative}:pattern")
        return tuple(sorted(excluded, key=str.casefold))

    def _excluded(self, relative: Path) -> bool:
        value = relative.as_posix()
        if relative.name.casefold() in {name.casefold() for name in DEFAULT_EXCLUDED_FILES}:
            return True
        if self.include and not any(_matches(value, pattern) for pattern in self.include):
            return True
        return any(_matches(value, pattern) for pattern in self.exclude)

    def _is_sensitive(self, path: Path, relative: Path) -> bool:
        del relative
        name = path.name.casefold()
        if name == ".env" or (name.startswith(".env.") and name != ".env.example"):
            return True
        likely_key = name.endswith((".pem", ".key", ".ppk")) or name in {
            "id_rsa",
            "id_ed25519",
            "id_ecdsa",
        }
        try:
            with path.open("rb") as stream:
                header = stream.read(512)
        except OSError:
            return False
        return (likely_key or header.startswith(b"-----BEGIN")) and any(
            marker in header for marker in PRIVATE_KEY_MARKERS
        )


def _validate_pattern(pattern: str) -> None:
    if not pattern or Path(pattern).is_absolute() or PurePosixPath(pattern).is_absolute():
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
]
