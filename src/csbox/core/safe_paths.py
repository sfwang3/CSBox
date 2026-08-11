from __future__ import annotations

import errno
import os
import secrets
import stat
import tempfile
from contextlib import suppress
from pathlib import Path, PurePosixPath

_HAS_POSIX_DIRECTORY_FDS = (
    os.name == "posix"
    and hasattr(os, "O_DIRECTORY")
    and hasattr(os, "O_NOFOLLOW")
    and os.open in os.supports_dir_fd
    and os.mkdir in os.supports_dir_fd
    and os.stat in os.supports_dir_fd
    and os.stat in os.supports_follow_symlinks
    and os.unlink in os.supports_dir_fd
    and os.rename in os.supports_dir_fd
)


def safe_relative_path(value: str) -> PurePosixPath:
    """Validate and normalize a user-controlled POSIX-relative path."""
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValueError("path must be a non-empty relative POSIX path")
    if "\\" in value or value.startswith("/") or value.startswith("//"):
        raise ValueError("path must use relative POSIX separators")
    if len(value) >= 2 and value[1] == ":" and value[0].isalpha():
        raise ValueError("drive-qualified paths are not allowed")

    parts = value.split("/")
    if parts[0] == "~" or ".." in parts:
        raise ValueError("home and parent paths are not allowed")
    return PurePosixPath(value)


def _refuse_symlink_parent(path: Path) -> None:
    """Reject paths whose existing parent chain contains a symlink."""
    absolute_path = path if path.is_absolute() else Path.cwd() / path
    current = Path(absolute_path.anchor)
    for part in absolute_path.parts[1:]:
        current /= part
        if current.is_symlink():
            raise ValueError("refusing to write through a symlink parent directory")


def _directory_open_flags() -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    return flags


def _raise_if_symlink_component(name: str, parent_fd: int, error: OSError) -> None:
    if error.errno not in {errno.ELOOP, errno.ENOTDIR}:
        raise error
    try:
        metadata = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except OSError:
        raise error from None
    if stat.S_ISLNK(metadata.st_mode):
        raise ValueError("refusing to write through a symlink parent directory") from error
    raise error


def _open_directory_fd(path: Path, *, create: bool) -> int:
    """Open an absolute directory by walking every component without following symlinks."""
    absolute_path = path if path.is_absolute() else Path.cwd() / path
    flags = _directory_open_flags()
    directory_fd = os.open(absolute_path.anchor, flags)
    try:
        for component in absolute_path.parts[1:]:
            try:
                child_fd = os.open(component, flags, dir_fd=directory_fd)
            except FileNotFoundError:
                if not create:
                    raise
                with suppress(FileExistsError):
                    os.mkdir(component, dir_fd=directory_fd)
                try:
                    child_fd = os.open(component, flags, dir_fd=directory_fd)
                except OSError as error:
                    _raise_if_symlink_component(component, directory_fd, error)
            except OSError as error:
                _raise_if_symlink_component(component, directory_fd, error)
            os.close(directory_fd)
            directory_fd = child_fd
        return directory_fd
    except BaseException:
        os.close(directory_fd)
        raise


def _prepare_canonical_parent(original_parent: Path) -> Path:
    preparation_fd = _open_directory_fd(original_parent, create=True)
    os.close(preparation_fd)
    canonical_parent = original_parent.resolve(strict=True)
    _refuse_symlink_parent(original_parent)
    if original_parent.absolute() != canonical_parent:
        raise ValueError("refusing to write through a non-canonical parent directory")
    return canonical_parent


def _destination_is_symlink(destination_name: str, parent_fd: int) -> bool:
    try:
        metadata = os.stat(destination_name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    return stat.S_ISLNK(metadata.st_mode)


def _atomic_write_text_with_directory_fd(destination: Path, text: str) -> None:
    if destination.is_symlink():
        raise ValueError("refusing to write through a symlink destination")

    canonical_parent = _prepare_canonical_parent(destination.parent)
    parent_fd = _open_directory_fd(canonical_parent, create=False)
    temporary_name = f".{destination.name}-{secrets.token_hex(16)}.tmp"
    temporary_fd: int | None = None
    temporary_created = False
    try:
        temporary_fd = os.open(
            temporary_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=parent_fd,
        )
        temporary_created = True
        temporary_file = os.fdopen(temporary_fd, mode="w", encoding="utf-8")
        temporary_fd = None
        with temporary_file:
            temporary_file.write(text)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        if _destination_is_symlink(destination.name, parent_fd):
            raise ValueError("refusing to write through a symlink destination")
        os.replace(
            temporary_name,
            destination.name,
            src_dir_fd=parent_fd,
            dst_dir_fd=parent_fd,
        )
    except BaseException:
        if temporary_fd is not None:
            os.close(temporary_fd)
        if temporary_created:
            with suppress(FileNotFoundError):
                os.unlink(temporary_name, dir_fd=parent_fd)
        raise
    finally:
        os.close(parent_fd)


def _atomic_write_text_with_path_fallback(destination: Path, text: str) -> None:
    """Compatibility fallback when openat-style APIs are unavailable.

    Windows and other Python platforms without usable directory FDs cannot pin the
    parent against a concurrent replacement. This path retains canonicalization and
    static symlink checks while preserving normal native behavior.
    """
    if destination.is_symlink():
        raise ValueError("refusing to write through a symlink destination")

    original_parent = destination.parent
    original_parent.mkdir(parents=True, exist_ok=True)
    canonical_parent = original_parent.resolve(strict=True)
    _refuse_symlink_parent(original_parent)
    if original_parent.absolute() != canonical_parent:
        raise ValueError("refusing to write through a non-canonical parent directory")
    canonical_destination = canonical_parent / destination.name
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=canonical_parent,
            prefix=f".{destination.name}-",
            suffix=".tmp",
            delete=False,
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
            temporary_file.write(text)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        if canonical_destination.is_symlink():
            raise ValueError("refusing to write through a symlink destination")
        os.replace(temporary_path, canonical_destination)
    except BaseException:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise


def atomic_write_text(destination: Path, text: str) -> None:
    """Atomically write UTF-8 text without following destination symlinks."""
    destination = Path(destination)
    if _HAS_POSIX_DIRECTORY_FDS:
        _atomic_write_text_with_directory_fd(destination, text)
    else:
        _atomic_write_text_with_path_fallback(destination, text)


def mkdir_exclusive(directory: Path) -> None:
    """Create one directory safely and fail if another writer already created it."""
    directory = Path(directory)
    if _HAS_POSIX_DIRECTORY_FDS:
        _mkdir_exclusive_with_directory_fd(directory)
    else:
        _mkdir_exclusive_with_path_fallback(directory)


def _mkdir_exclusive_with_directory_fd(directory: Path) -> None:
    if directory.is_symlink():
        raise ValueError("refusing to create a symlink directory")

    canonical_parent = _prepare_canonical_parent(directory.parent)
    parent_fd = _open_directory_fd(canonical_parent, create=False)
    try:
        try:
            os.mkdir(directory.name, mode=0o700, dir_fd=parent_fd)
        except FileExistsError as error:
            if _destination_is_symlink(directory.name, parent_fd):
                raise ValueError("refusing to create a symlink directory") from error
            raise
    finally:
        os.close(parent_fd)


def _mkdir_exclusive_with_path_fallback(directory: Path) -> None:
    """Compatibility fallback for platforms without directory file descriptors."""
    if directory.is_symlink():
        raise ValueError("refusing to create a symlink directory")

    original_parent = directory.parent
    original_parent.mkdir(parents=True, exist_ok=True)
    canonical_parent = original_parent.resolve(strict=True)
    _refuse_symlink_parent(original_parent)
    if original_parent.absolute() != canonical_parent:
        raise ValueError("refusing to create through a non-canonical parent directory")
    canonical_directory = canonical_parent / directory.name
    if canonical_directory.is_symlink():
        raise ValueError("refusing to create a symlink directory")
    canonical_directory.mkdir(mode=0o700)
