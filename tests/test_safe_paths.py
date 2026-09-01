from __future__ import annotations

import os
from pathlib import Path, PurePosixPath

import pytest

from csbox.core.safe_paths import (
    atomic_copy_file,
    atomic_create_text,
    atomic_write_text,
    mkdir_exclusive,
    safe_relative_path,
    safe_rename,
)

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
requires_posix_directory_fds = pytest.mark.skipif(
    not _HAS_POSIX_DIRECTORY_FDS,
    reason="requires POSIX directory-fd and O_NOFOLLOW support",
)


@pytest.mark.parametrize(
    "value",
    [
        "/tmp/实验一",
        "C:\\Users\\测试用户\\桌面\\实验一",
        "\\\\server\\share\\实验一",
        "~/课程实验/计算机网络/实验一",
        "实验一\\证据/截图.png",
        "实验一/../秘密.txt",
        "../秘密.txt",
    ],
)
def test_safe_relative_path_rejects_absolute_drive_unc_mixed_and_parent_paths(
    value: str,
) -> None:
    with pytest.raises(ValueError):
        safe_relative_path(value)


def test_safe_relative_path_returns_posix_path_for_a_nested_relative_path() -> None:
    assert safe_relative_path("课程/计算机网络/实验一") == PurePosixPath("课程/计算机网络/实验一")


def test_atomic_write_text_refuses_a_symlink_destination(tmp_path: Path) -> None:
    target = tmp_path / "target.txt"
    target.write_text("original", encoding="utf-8")
    destination = tmp_path / "link.txt"
    destination.symlink_to(target)

    with pytest.raises(ValueError):
        atomic_write_text(destination, "replacement")

    assert target.read_text(encoding="utf-8") == "original"


def test_atomic_write_text_refuses_a_symlink_parent_directory(tmp_path: Path) -> None:
    redirected_directory = tmp_path / "redirected"
    redirected_directory.mkdir()
    symlink_parent = tmp_path / "linked-output"
    symlink_parent.symlink_to(redirected_directory, target_is_directory=True)

    with pytest.raises(ValueError, match="symlink parent"):
        atomic_write_text(symlink_parent / "output.txt", "replacement")

    assert not (redirected_directory / "output.txt").exists()


def test_safe_rename_refuses_a_symlink_parent_directory(tmp_path: Path) -> None:
    source = tmp_path / "source.txt"
    source.write_text("safe", encoding="utf-8")
    redirected_directory = tmp_path / "redirected"
    redirected_directory.mkdir()
    symlink_parent = tmp_path / "linked-output"
    symlink_parent.symlink_to(redirected_directory, target_is_directory=True)

    with pytest.raises(ValueError, match="symlink parent"):
        safe_rename(source, symlink_parent / "output.txt")

    assert source.read_text(encoding="utf-8") == "safe"
    assert not (redirected_directory / "output.txt").exists()


def test_safe_rename_no_replace_preserves_an_existing_destination(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "generated.txt").write_text("generated", encoding="utf-8")
    destination = tmp_path / "destination"
    destination.mkdir()

    with pytest.raises(FileExistsError):
        safe_rename(source, destination, replace_existing=False)

    assert (source / "generated.txt").read_text(encoding="utf-8") == "generated"
    assert list(destination.iterdir()) == []


def test_atomic_write_text_rechecks_parent_chain_after_canonicalization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    staging_directory = tmp_path / "staging"
    staging_directory.mkdir()
    nested_directory = tmp_path / "nested"
    nested_directory.mkdir()
    redirected_directory = tmp_path / "redirected"
    redirected_directory.mkdir()
    destination = staging_directory / ".." / "nested" / "output.txt"
    original_parent = destination.parent
    real_resolve = Path.resolve

    def resolve(path: Path, *, strict: bool = False) -> Path:
        resolved = real_resolve(path, strict=strict)
        if path == original_parent:
            staging_directory.rmdir()
            staging_directory.symlink_to(redirected_directory, target_is_directory=True)
        return resolved

    monkeypatch.setattr(Path, "resolve", resolve)

    with pytest.raises(ValueError, match="symlink parent"):
        atomic_write_text(destination, "replacement")

    assert not (nested_directory / "output.txt").exists()
    assert not (redirected_directory / "output.txt").exists()


def test_atomic_write_text_path_fallback_preserves_normal_nested_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import csbox.core.safe_paths as safe_paths

    monkeypatch.setattr(safe_paths, "_HAS_POSIX_DIRECTORY_FDS", False)
    destination = tmp_path / "nested" / "output.txt"

    atomic_write_text(destination, "计算机网络实验")

    assert destination.read_text(encoding="utf-8") == "计算机网络实验"


def test_atomic_create_text_path_fallback_does_not_replace_existing_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import csbox.core.safe_paths as safe_paths

    monkeypatch.setattr(safe_paths, "_HAS_POSIX_DIRECTORY_FDS", False)
    destination = tmp_path / "nested" / "output.txt"

    atomic_create_text(destination, "首次创建")
    with pytest.raises(FileExistsError):
        atomic_create_text(destination, "不得覆盖")

    assert destination.read_text(encoding="utf-8") == "首次创建"


@requires_posix_directory_fds
def test_atomic_create_text_treats_published_destination_as_success_if_cleanup_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import csbox.core.safe_paths as safe_paths

    destination = tmp_path / "output.txt"
    real_unlink = safe_paths.os.unlink
    failed = False

    def fail_temporary_cleanup(path: object, *args: object, **kwargs: object) -> object:
        nonlocal failed
        if not failed and isinstance(path, str) and path.startswith(".csbox-atomic-"):
            failed = True
            raise OSError("simulated cleanup failure")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(safe_paths.os, "unlink", fail_temporary_cleanup)

    atomic_create_text(destination, "已发布")

    assert failed is True
    assert destination.read_text(encoding="utf-8") == "已发布"


def test_atomic_create_text_path_fallback_treats_published_destination_as_success_if_cleanup_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import csbox.core.safe_paths as safe_paths

    monkeypatch.setattr(safe_paths, "_HAS_POSIX_DIRECTORY_FDS", False)
    destination = tmp_path / "output.txt"

    if os.name == "nt":
        atomic_create_text(destination, "已发布")
        with pytest.raises(FileExistsError):
            atomic_create_text(destination, "不得覆盖")
        assert destination.read_text(encoding="utf-8") == "已发布"
        return

    real_unlink = safe_paths.Path.unlink
    failed = False

    def fail_temporary_cleanup(path: Path, *, missing_ok: bool = False) -> None:
        nonlocal failed
        if not failed and path.name.startswith(".csbox-atomic-"):
            failed = True
            raise OSError("simulated cleanup failure")
        real_unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(safe_paths.Path, "unlink", fail_temporary_cleanup)

    atomic_create_text(destination, "已发布")

    assert failed is True
    assert destination.read_text(encoding="utf-8") == "已发布"


def test_bounded_regular_reader_accepts_a_relative_parent_input_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from csbox.core.safe_paths import read_regular_text

    source = tmp_path / "scenario.toml"
    source.write_text('name = "上级目录"\n', encoding="utf-8")
    child = tmp_path / "child"
    child.mkdir()
    monkeypatch.chdir(child)

    assert read_regular_text(Path("../scenario.toml"), max_bytes=1024) == 'name = "上级目录"\n'


def test_mkdir_fallback_rejects_symlink_parents_before_creating_outside(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import csbox.core.safe_paths as safe_paths

    monkeypatch.setattr(safe_paths, "_HAS_POSIX_DIRECTORY_FDS", False)
    outside = tmp_path / "outside"
    outside.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="symlink parent"):
        mkdir_exclusive(linked / "nested" / "output")

    assert list(outside.iterdir()) == []


def test_safe_rename_path_fallback_does_not_require_posix_directory_flags(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import csbox.core.safe_paths as safe_paths

    source = tmp_path / "source.txt"
    source.write_text("safe", encoding="utf-8")
    destination = tmp_path / "destination.txt"
    monkeypatch.setattr(safe_paths, "_HAS_POSIX_DIRECTORY_FDS", False)
    monkeypatch.delattr(safe_paths.os, "O_DIRECTORY", raising=False)

    safe_rename(source, destination, replace_existing=False)

    assert destination.read_text(encoding="utf-8") == "safe"


def test_safe_rename_windows_fallback_can_restore_an_existing_regular_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import csbox.core.safe_paths as safe_paths

    source = tmp_path / "backup.txt"
    source.write_text("original", encoding="utf-8")
    destination = tmp_path / "published.txt"
    destination.write_text("partial", encoding="utf-8")
    monkeypatch.setattr(safe_paths, "_HAS_POSIX_DIRECTORY_FDS", False)
    if os.name != "nt":
        from pathlib import PosixPath

        monkeypatch.setattr(safe_paths.os, "name", "nt")
        monkeypatch.setattr(safe_paths, "Path", PosixPath)

    def windows_rename(source_path: object, destination_path: object) -> None:
        del source_path, destination_path
        raise FileExistsError("Windows rename does not replace")

    monkeypatch.setattr(safe_paths.os, "rename", windows_rename)

    safe_paths.safe_rename(source, destination)

    assert destination.read_text(encoding="utf-8") == "original"
    assert not source.exists()


def test_atomic_copy_path_fallback_refuses_a_symlink_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import csbox.core.safe_paths as safe_paths

    monkeypatch.setattr(safe_paths, "_HAS_POSIX_DIRECTORY_FDS", False)
    source = tmp_path / "source.zip"
    source.write_bytes(b"archive")
    redirected = tmp_path / "redirected"
    redirected.mkdir()
    linked_parent = tmp_path / "linked-output"
    linked_parent.symlink_to(redirected, target_is_directory=True)

    with pytest.raises(ValueError, match="symlink parent"):
        atomic_copy_file(source, linked_parent / "output.zip")

    assert not (redirected / "output.zip").exists()


def test_atomic_copy_path_fallback_does_not_replace_a_raced_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import csbox.core.safe_paths as safe_paths

    monkeypatch.setattr(safe_paths, "_HAS_POSIX_DIRECTORY_FDS", False)
    source = tmp_path / "source.zip"
    source.write_bytes(b"new archive")
    destination = tmp_path / "output.zip"
    if os.name == "nt":
        original_rename = safe_paths.os.rename

        def race_destination(source_path: object, destination_path: object) -> None:
            destination.write_bytes(b"raced archive")
            original_rename(source_path, destination_path)

        monkeypatch.setattr(safe_paths.os, "rename", race_destination)
    else:
        original_link = safe_paths.os.link

        def race_destination(*args: object, **kwargs: object) -> None:
            destination.write_bytes(b"raced archive")
            original_link(*args, **kwargs)

        monkeypatch.setattr(safe_paths.os, "link", race_destination)

    with pytest.raises(FileExistsError):
        atomic_copy_file(source, destination, replace_existing=False)

    assert destination.read_bytes() == b"raced archive"


def test_atomic_copy_path_fallback_uses_windows_no_replace_rename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import csbox.core.safe_paths as safe_paths

    monkeypatch.setattr(safe_paths, "_HAS_POSIX_DIRECTORY_FDS", False)
    if os.name != "nt":
        from pathlib import PosixPath

        monkeypatch.setattr(safe_paths.os, "name", "nt")
        monkeypatch.setattr(safe_paths, "Path", PosixPath)
        path_type = PosixPath
    else:
        path_type = Path
    source = path_type(tmp_path) / "source.zip"
    source.write_bytes(b"archive")
    destination = path_type(tmp_path) / "output.zip"
    rename_calls: list[tuple[object, object]] = []
    original_rename = safe_paths.os.rename

    def record_rename(source_path: object, destination_path: object) -> None:
        rename_calls.append((source_path, destination_path))
        original_rename(source_path, destination_path)

    monkeypatch.setattr(safe_paths.os, "rename", record_rename)

    atomic_copy_file(source, destination, replace_existing=False)

    assert destination.read_bytes() == b"archive"
    assert len(rename_calls) == 1


@requires_posix_directory_fds
def test_atomic_write_text_uses_one_directory_fd_for_temporary_file_and_replace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import csbox.core.safe_paths as safe_paths

    open_calls: list[tuple[str | bytes | Path, int, int | None]] = []
    replace_calls: list[tuple[str | Path, str | Path, int | None, int | None]] = []
    real_open = safe_paths.os.open
    real_replace = safe_paths.os.replace

    def open_file(
        path: str | bytes | Path, flags: int, mode: int = 0o777, *, dir_fd: int | None = None
    ) -> int:
        open_calls.append((path, flags, dir_fd))
        return real_open(path, flags, mode, dir_fd=dir_fd)

    def replace(
        source: str | Path,
        destination: str | Path,
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
    ) -> None:
        replace_calls.append((source, destination, src_dir_fd, dst_dir_fd))
        real_replace(
            source,
            destination,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
        )

    monkeypatch.setattr(safe_paths.os, "open", open_file)
    monkeypatch.setattr(safe_paths.os, "replace", replace)
    destination = tmp_path / "nested" / "output.txt"

    atomic_write_text(destination, "计算机网络实验")

    assert destination.read_text(encoding="utf-8") == "计算机网络实验"
    directory_calls = [call for call in open_calls if call[1] & os.O_DIRECTORY]
    assert directory_calls
    assert all(flags & os.O_NOFOLLOW for _, flags, _ in directory_calls)
    root_calls = [call for call in directory_calls if call[2] is None]
    component_calls = [call for call in directory_calls if call[2] is not None]
    assert root_calls
    assert all(Path(path) == Path(Path(path).anchor) for path, _, _ in root_calls)
    assert component_calls
    assert all(Path(path).parent == Path(".") for path, _, _ in component_calls)
    temporary_calls = [call for call in open_calls if call[1] & os.O_CREAT]
    assert len(temporary_calls) == 1
    temporary_name, temporary_flags, temporary_dir_fd = temporary_calls[0]
    assert Path(temporary_name).parent == Path(".")
    assert temporary_flags & os.O_EXCL
    assert temporary_flags & os.O_NOFOLLOW
    assert temporary_dir_fd is not None
    assert replace_calls == [(temporary_name, destination.name, temporary_dir_fd, temporary_dir_fd)]


@requires_posix_directory_fds
def test_atomic_write_text_rejects_an_ancestor_swapped_to_a_symlink_while_opening(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import csbox.core.safe_paths as safe_paths

    parent = tmp_path / "parent"
    parent.mkdir()
    nested = parent / "nested"
    nested.mkdir()
    detached = parent / "detached"
    redirected = tmp_path / "redirected"
    redirected.mkdir()
    destination = nested / "output.txt"
    real_open = safe_paths.os.open

    def open_file(
        path: str | bytes | Path, flags: int, mode: int = 0o777, *, dir_fd: int | None = None
    ) -> int:
        if path == nested.name and dir_fd is not None and flags & os.O_DIRECTORY:
            nested.rename(detached)
            nested.symlink_to(redirected, target_is_directory=True)
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(safe_paths.os, "open", open_file)

    with pytest.raises(ValueError, match="symlink parent"):
        atomic_write_text(destination, "do-not-leak")

    assert not (detached / destination.name).exists()
    assert not (redirected / destination.name).exists()
    assert list(detached.iterdir()) == []
    assert list(redirected.iterdir()) == []


@requires_posix_directory_fds
def test_atomic_write_text_keeps_replace_and_cleanup_bound_to_open_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import csbox.core.safe_paths as safe_paths

    parent = tmp_path / "parent"
    parent.mkdir()
    detached = tmp_path / "detached"
    redirected = tmp_path / "redirected"
    redirected.mkdir()
    destination = parent / "output.txt"
    real_fsync = safe_paths.os.fsync

    def fsync(file_descriptor: int) -> None:
        real_fsync(file_descriptor)
        parent.rename(detached)
        parent.symlink_to(redirected, target_is_directory=True)

    monkeypatch.setattr(safe_paths.os, "fsync", fsync)

    atomic_write_text(destination, "do-not-leak")

    assert (detached / destination.name).read_text(encoding="utf-8") == "do-not-leak"
    assert list(redirected.iterdir()) == []
    assert [path.name for path in detached.iterdir()] == [destination.name]


@requires_posix_directory_fds
def test_atomic_write_text_rejects_a_destination_symlink_appearing_before_replace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import csbox.core.safe_paths as safe_paths

    destination = tmp_path / "output.txt"
    protected = tmp_path / "protected.txt"
    protected.write_text("protected", encoding="utf-8")
    real_fsync = safe_paths.os.fsync

    def fsync(file_descriptor: int) -> None:
        real_fsync(file_descriptor)
        destination.symlink_to(protected)

    monkeypatch.setattr(safe_paths.os, "fsync", fsync)

    with pytest.raises(ValueError, match="symlink destination"):
        atomic_write_text(destination, "do-not-leak")

    assert destination.is_symlink()
    assert protected.read_text(encoding="utf-8") == "protected"
    assert sorted(path.name for path in tmp_path.iterdir()) == ["output.txt", "protected.txt"]


@requires_posix_directory_fds
def test_atomic_write_text_does_not_unlink_a_colliding_temporary_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import csbox.core.safe_paths as safe_paths

    destination = tmp_path / "output.txt"
    temporary_name = ".csbox-atomic-fixed.tmp"
    existing_file = tmp_path / temporary_name
    existing_file.write_text("belongs-to-someone-else", encoding="utf-8")
    monkeypatch.setattr(safe_paths.secrets, "token_hex", lambda length: "fixed")

    with pytest.raises(FileExistsError):
        atomic_write_text(destination, "do-not-leak")

    assert existing_file.read_text(encoding="utf-8") == "belongs-to-someone-else"
    assert not destination.exists()
