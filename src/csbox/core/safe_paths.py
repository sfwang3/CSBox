from __future__ import annotations

import ctypes
import errno
import os
import secrets
import shutil
import stat
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path, PurePosixPath
from typing import BinaryIO, TextIO

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
_HAS_POSIX_NO_REPLACE_LINK = _HAS_POSIX_DIRECTORY_FDS and os.link in os.supports_dir_fd


def _temporary_name() -> str:
    """Return a short private staging name independent of user-controlled components."""

    return f".csbox-atomic-{secrets.token_hex(16)}.tmp"


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
        if _is_reparse_point(current):
            raise ValueError("refusing to write through a symlink parent directory")


def _is_reparse_point(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except OSError:
        return False
    return _is_reparse_metadata(metadata)


def _is_reparse_metadata(metadata: os.stat_result) -> bool:
    if stat.S_ISLNK(metadata.st_mode):
        return True
    if os.name == "nt":
        reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        return bool(getattr(metadata, "st_file_attributes", 0) & reparse_flag)
    return False


def _prepare_path_parent(original_parent: Path) -> Path:
    """Create a fallback parent one component at a time and reject reparse points."""
    absolute_parent = (
        original_parent if original_parent.is_absolute() else Path.cwd() / original_parent
    )
    current = Path(absolute_parent.anchor)
    for component in absolute_parent.parts[1:]:
        current /= component
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            parent = current.parent
            if _is_reparse_point(parent):
                raise ValueError("refusing to create a child below a symlink parent") from None
            with suppress(FileExistsError):
                current.mkdir()
            metadata = current.lstat()
        if _is_reparse_point(current):
            raise ValueError("refusing to write through a symlink parent directory")
        if not stat.S_ISDIR(metadata.st_mode):
            raise ValueError("refusing to use a non-directory or symlink parent")

    canonical_parent = absolute_parent.resolve(strict=True)
    _refuse_symlink_parent(absolute_parent)
    if absolute_parent.absolute() != canonical_parent:
        raise ValueError("refusing to write through a non-canonical parent directory")
    return canonical_parent


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
    temporary_name = _temporary_name()
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

    canonical_parent = _prepare_path_parent(destination.parent)
    canonical_destination = canonical_parent / destination.name
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=canonical_parent,
            prefix=".csbox-atomic-",
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


def _atomic_write_bytes_with_directory_fd(destination: Path, data: bytes) -> None:
    if destination.is_symlink():
        raise ValueError("refusing to write through a symlink destination")

    canonical_parent = _prepare_canonical_parent(destination.parent)
    parent_fd = _open_directory_fd(canonical_parent, create=False)
    temporary_name = _temporary_name()
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
        temporary_file = os.fdopen(temporary_fd, mode="wb")
        temporary_fd = None
        with temporary_file:
            temporary_file.write(data)
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


def _atomic_write_bytes_with_path_fallback(destination: Path, data: bytes) -> None:
    if destination.is_symlink():
        raise ValueError("refusing to write through a symlink destination")

    canonical_parent = _prepare_path_parent(destination.parent)
    canonical_destination = canonical_parent / destination.name
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=canonical_parent,
            prefix=".csbox-atomic-",
            suffix=".tmp",
            delete=False,
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
            temporary_file.write(data)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        if canonical_destination.is_symlink():
            raise ValueError("refusing to write through a symlink destination")
        os.replace(temporary_path, canonical_destination)
    except BaseException:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise


def atomic_write_bytes(destination: Path, data: bytes) -> None:
    """Atomically write bytes without following destination symlinks."""
    destination = Path(destination)
    if _HAS_POSIX_DIRECTORY_FDS:
        _atomic_write_bytes_with_directory_fd(destination, data)
    else:
        _atomic_write_bytes_with_path_fallback(destination, data)


@contextmanager
def open_regular_binary(path: Path | str) -> Iterator[BinaryIO]:
    """Open an existing regular file without following symlinks or blocking on FIFOs."""

    source = Path(path)
    descriptor: int | None = None
    parent_fd: int | None = None
    try:
        flags = os.O_RDONLY
        if hasattr(os, "O_NONBLOCK"):
            flags |= os.O_NONBLOCK
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW

        if _HAS_POSIX_DIRECTORY_FDS:
            parent_fd = _open_input_parent_fd(source.parent)
            descriptor = os.open(source.name, flags, dir_fd=parent_fd)
        else:
            _refuse_symlink_parent(source.parent)
            before = source.lstat()
            if _is_reparse_metadata(before) or not stat.S_ISREG(before.st_mode):
                raise ValueError("input is not a regular file")
            descriptor = os.open(source, flags)

        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise ValueError("input is not a regular file")
        if not _HAS_POSIX_DIRECTORY_FDS and (
            getattr(before, "st_dev", None) != getattr(opened, "st_dev", None)
            or getattr(before, "st_ino", None) != getattr(opened, "st_ino", None)
        ):
            raise ValueError("input file changed while opening")

        stream = os.fdopen(descriptor, "rb")
        descriptor = None
        with stream:
            yield stream
    except OSError as error:
        if error.errno in {errno.ELOOP, errno.ENXIO, errno.ENODEV}:
            raise ValueError("input is not a regular file") from error
        raise
    finally:
        if descriptor is not None:
            with suppress(OSError):
                os.close(descriptor)
        if parent_fd is not None:
            with suppress(OSError):
                os.close(parent_fd)


def read_regular_bytes(path: Path | str, *, max_bytes: int) -> bytes:
    """Read at most ``max_bytes`` from a regular file and reject oversized input."""

    if type(max_bytes) is not int or max_bytes <= 0:
        raise ValueError("max_bytes must be a positive integer")
    with open_regular_binary(path) as stream:
        data = stream.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise ValueError("input file exceeds the size limit")
    return data


def read_regular_text(
    path: Path | str,
    *,
    max_bytes: int,
    encoding: str = "utf-8",
) -> str:
    """Read bounded text from a regular file without following filesystem links."""

    text = read_regular_bytes(path, max_bytes=max_bytes).decode(encoding)
    # Structured project files should have the same logical text on every host;
    # do not let native Windows newline translation leak into parsers and CLI
    # diagnostics.
    return text.replace("\r\n", "\n").replace("\r", "\n")


def ensure_private_directory(directory: Path | str) -> None:
    """Create a directory through non-symlink parents and make its final mode private."""

    path = Path(directory)
    if _HAS_POSIX_DIRECTORY_FDS:
        absolute = path if path.is_absolute() else Path.cwd() / path
        flags = _directory_open_flags()
        directory_fd = os.open(absolute.anchor, flags)
        try:
            for component in absolute.parts[1:]:
                try:
                    child_fd = os.open(component, flags, dir_fd=directory_fd)
                except FileNotFoundError:
                    with suppress(FileExistsError):
                        os.mkdir(component, mode=0o700, dir_fd=directory_fd)
                    child_fd = os.open(component, flags, dir_fd=directory_fd)
                except OSError as error:
                    _raise_if_symlink_component(component, directory_fd, error)
                os.close(directory_fd)
                directory_fd = child_fd
            os.fchmod(directory_fd, 0o700)
        finally:
            os.close(directory_fd)
        return

    canonical = _prepare_path_parent(path)
    if os.name != "nt":
        canonical.chmod(0o700)


@contextmanager
def open_private_text_append(path: Path | str) -> Iterator[TextIO]:
    """Safely create or append a private UTF-8 regular file."""

    destination = Path(path)
    ensure_private_directory(destination.parent)
    descriptor: int | None = None
    parent_fd: int | None = None
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        if _HAS_POSIX_DIRECTORY_FDS:
            parent_fd = _open_existing_parent_fd(destination.parent)
            descriptor = os.open(destination.name, flags, 0o600, dir_fd=parent_fd)
        else:
            if destination.is_symlink():
                raise ValueError("refusing to append through a symlink destination")
            descriptor = os.open(destination, flags, 0o600)
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError("output is not a regular file")
        if os.name != "nt":
            os.fchmod(descriptor, 0o600)
        stream = os.fdopen(descriptor, "a", encoding="utf-8", newline="\n")
        descriptor = None
        with stream:
            yield stream
    finally:
        if descriptor is not None:
            with suppress(OSError):
                os.close(descriptor)
        if parent_fd is not None:
            with suppress(OSError):
                os.close(parent_fd)


def _check_publish_destination(
    destination_name: str,
    parent_fd: int,
    *,
    replace_existing: bool,
) -> None:
    try:
        metadata = os.stat(destination_name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    if _is_reparse_metadata(metadata):
        raise ValueError("refusing to replace a symlink destination")
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError("refusing to replace a non-regular destination")
    if not replace_existing:
        raise FileExistsError(destination_name)


def _check_publish_destination_path(destination: Path, *, replace_existing: bool) -> None:
    try:
        metadata = destination.lstat()
    except FileNotFoundError:
        return
    if _is_reparse_metadata(metadata):
        raise ValueError("refusing to replace a symlink destination")
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError("refusing to replace a non-regular destination")
    if not replace_existing:
        raise FileExistsError(destination)


def atomic_copy_file(source: Path, destination: Path, *, replace_existing: bool = True) -> None:
    """Copy a completed file into a destination with safe atomic publication."""
    source = Path(source)
    destination = Path(destination)
    try:
        source_metadata = source.lstat()
    except OSError:
        raise ValueError("temporary package is unavailable") from None
    if stat.S_ISLNK(source_metadata.st_mode) or not stat.S_ISREG(source_metadata.st_mode):
        raise ValueError("temporary package is not a regular file")

    if _HAS_POSIX_DIRECTORY_FDS:
        canonical_parent = _prepare_canonical_parent(destination.parent)
        parent_fd = _open_directory_fd(canonical_parent, create=False)
        temporary_name = _temporary_name()
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
            with open_regular_binary(source) as source_file:
                temporary_file = os.fdopen(temporary_fd, "wb")
                temporary_fd = None
                with temporary_file:
                    shutil.copyfileobj(source_file, temporary_file, length=1024 * 1024)
                    temporary_file.flush()
                    os.fsync(temporary_file.fileno())
            _check_publish_destination(
                destination.name,
                parent_fd,
                replace_existing=replace_existing,
            )
            if replace_existing:
                os.replace(
                    temporary_name,
                    destination.name,
                    src_dir_fd=parent_fd,
                    dst_dir_fd=parent_fd,
                )
            else:
                if not _HAS_POSIX_NO_REPLACE_LINK:
                    raise ValueError("safe no-replace publish is unavailable")
                os.link(
                    temporary_name,
                    destination.name,
                    src_dir_fd=parent_fd,
                    dst_dir_fd=parent_fd,
                )
                os.unlink(temporary_name, dir_fd=parent_fd)
        except BaseException:
            if temporary_fd is not None:
                os.close(temporary_fd)
            if temporary_created:
                with suppress(FileNotFoundError):
                    os.unlink(temporary_name, dir_fd=parent_fd)
            raise
        finally:
            os.close(parent_fd)
        return

    canonical_parent = _prepare_path_parent(destination.parent)
    canonical_destination = canonical_parent / destination.name
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=canonical_parent,
            prefix=".csbox-atomic-",
            suffix=".tmp",
            delete=False,
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
            with open_regular_binary(source) as source_file:
                shutil.copyfileobj(source_file, temporary_file, length=1024 * 1024)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        _check_publish_destination_path(canonical_destination, replace_existing=replace_existing)
        if replace_existing:
            os.replace(temporary_path, canonical_destination)
        elif os.name == "nt":
            # Windows os.rename refuses an existing destination, unlike POSIX.
            os.rename(temporary_path, canonical_destination)
        else:
            try:
                os.link(temporary_path, canonical_destination)
            except FileExistsError:
                raise
            else:
                temporary_path.unlink()
    except BaseException:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise


def _open_existing_parent_fd(original_parent: Path) -> int:
    """Open an existing parent directory without following symlinks."""
    absolute_parent = (
        original_parent if original_parent.is_absolute() else Path.cwd() / original_parent
    )
    canonical_parent = absolute_parent.resolve(strict=True)
    _refuse_symlink_parent(absolute_parent)
    if absolute_parent.absolute() != canonical_parent:
        raise ValueError("refusing to write through a non-canonical parent directory")
    return _open_directory_fd(canonical_parent, create=False)


def _open_input_parent_fd(original_parent: Path) -> int:
    """Open a read-only parent safely while allowing lexical ``..`` components."""

    absolute_parent = (
        original_parent if original_parent.is_absolute() else Path.cwd() / original_parent
    )
    _refuse_symlink_parent(absolute_parent)
    canonical_parent = absolute_parent.resolve(strict=True)
    return _open_directory_fd(canonical_parent, create=False)


def _rename_no_replace(
    source_name: str,
    destination_name: str,
    *,
    source_parent_fd: int,
    destination_parent_fd: int,
) -> None:
    if sys.platform.startswith("linux"):
        libc = ctypes.CDLL(None, use_errno=True)
        renameat2 = getattr(libc, "renameat2", None)
        if renameat2 is not None:
            renameat2.argtypes = (
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_uint,
            )
            renameat2.restype = ctypes.c_int
            result = renameat2(
                source_parent_fd,
                os.fsencode(source_name),
                destination_parent_fd,
                os.fsencode(destination_name),
                1,  # RENAME_NOREPLACE
            )
            if result == 0:
                return
            error_number = ctypes.get_errno()
            raise OSError(error_number, os.strerror(error_number), destination_name)

    metadata = os.stat(source_name, dir_fd=source_parent_fd, follow_symlinks=False)
    if stat.S_ISREG(metadata.st_mode) and _HAS_POSIX_NO_REPLACE_LINK:
        os.link(
            source_name,
            destination_name,
            src_dir_fd=source_parent_fd,
            dst_dir_fd=destination_parent_fd,
        )
        os.unlink(source_name, dir_fd=source_parent_fd)
        return
    raise ValueError("safe no-replace directory publication is unavailable")


def _safe_rename_with_directory_fds(
    source: Path,
    destination: Path,
    *,
    replace_existing: bool,
) -> None:
    source_parent_fd = _open_existing_parent_fd(source.parent)
    destination_parent_fd: int | None = None
    try:
        destination_parent_fd = _open_existing_parent_fd(destination.parent)
        if replace_existing and _destination_is_symlink(destination.name, destination_parent_fd):
            raise ValueError("refusing to replace a symlink destination")
        if replace_existing:
            os.rename(
                source.name,
                destination.name,
                src_dir_fd=source_parent_fd,
                dst_dir_fd=destination_parent_fd,
            )
        else:
            _rename_no_replace(
                source.name,
                destination.name,
                source_parent_fd=source_parent_fd,
                destination_parent_fd=destination_parent_fd,
            )
    finally:
        os.close(source_parent_fd)
        if destination_parent_fd is not None:
            os.close(destination_parent_fd)


def _safe_rename_with_path_fallback(
    source: Path,
    destination: Path,
    *,
    replace_existing: bool,
) -> None:
    if source.is_symlink() or destination.is_symlink():
        raise ValueError("refusing to rename through a symlink")
    source_parent = _prepare_path_parent(source.parent)
    destination_parent = _prepare_path_parent(destination.parent)
    canonical_source = source_parent / source.name
    canonical_destination = destination_parent / destination.name
    if replace_existing:
        try:
            destination_metadata = canonical_destination.lstat()
        except FileNotFoundError:
            pass
        else:
            if _is_reparse_metadata(destination_metadata):
                raise ValueError("refusing to replace a symlink destination")
        os.replace(canonical_source, canonical_destination)
        return
    if os.name == "nt":
        os.rename(canonical_source, canonical_destination)
        return
    metadata = canonical_source.lstat()
    if stat.S_ISREG(metadata.st_mode):
        os.link(canonical_source, canonical_destination)
        canonical_source.unlink()
        return
    raise ValueError("safe no-replace directory publication is unavailable")


def safe_rename(
    source: Path,
    destination: Path,
    *,
    replace_existing: bool = True,
) -> None:
    """Rename a file or directory while pinning both parents on POSIX."""
    source = Path(source)
    destination = Path(destination)
    if source.is_symlink() or destination.is_symlink():
        raise ValueError("refusing to rename through a symlink")
    if _HAS_POSIX_DIRECTORY_FDS:
        _safe_rename_with_directory_fds(
            source,
            destination,
            replace_existing=replace_existing,
        )
    else:
        _safe_rename_with_path_fallback(
            source,
            destination,
            replace_existing=replace_existing,
        )


def restore_backup_or_preserve(backup: Path, destination: Path) -> Path:
    """Restore an old destination without discarding a conflicting current file.

    A rollback must never overwrite a path another writer created after publication
    began.  When no-replace restoration finds such a path, move the current file to
    a private recovery name before restoring the old canonical destination.  If a
    second race prevents restoration, preserve the backup under another recovery
    name so no version is discarded.
    """

    backup = Path(backup)
    destination = Path(destination)
    try:
        safe_rename(backup, destination, replace_existing=False)
    except FileExistsError:
        try:
            _preserve_for_recovery(destination, destination)
            safe_rename(backup, destination, replace_existing=False)
        except (OSError, ValueError):
            return _preserve_for_recovery(backup, destination)
    except (OSError, ValueError):
        return _preserve_for_recovery(backup, destination)
    return destination


def _preserve_for_recovery(source: Path, destination: Path) -> Path:
    if source.is_symlink() or not source.is_file():
        raise ValueError("refusing to preserve a non-regular recovery source")
    for _ in range(8):
        recovery = destination.parent / f".csbox-recovery-{secrets.token_hex(16)}.bak"
        try:
            safe_rename(source, recovery, replace_existing=False)
        except FileExistsError:
            continue
        return recovery
    raise OSError("could not allocate a safe recovery path")


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

    canonical_parent = _prepare_path_parent(directory.parent)
    canonical_directory = canonical_parent / directory.name
    if canonical_directory.is_symlink():
        raise ValueError("refusing to create a symlink directory")
    canonical_directory.mkdir(mode=0o700)
