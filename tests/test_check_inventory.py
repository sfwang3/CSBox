from __future__ import annotations

import inspect
import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

import csbox.check.detectors as detectors
from csbox.check.detectors import FileInventory


def _metadata_with(metadata: os.stat_result, **overrides: int) -> SimpleNamespace:
    values = {
        name: getattr(metadata, name)
        for name in dir(metadata)
        if name.startswith("st_") and not callable(getattr(metadata, name))
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _windows_reparse_metadata(metadata: os.stat_result) -> SimpleNamespace:
    return _metadata_with(
        metadata,
        st_file_attributes=getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400),
    )


def test_text_cache_reads_one_file_once_and_reports_a_cache_hit(tmp_path: Path) -> None:
    source = tmp_path / "notes.txt"
    source.write_text("中文 note\n", encoding="utf-8")

    inventory = FileInventory.build(tmp_path)
    entry = inventory.files[0]

    assert inventory.text(entry) == "中文 note\n"
    assert inventory.text(entry) == "中文 note\n"
    assert hasattr(inventory, "text_scan_stats")
    stats = inventory.text_scan_stats

    assert stats.read_count == 1
    assert stats.cache_hits == 1
    assert stats.requested == 2
    assert stats.cached_entries == 1
    assert stats.cached_bytes == len("中文 note\n".encode())


def test_text_scan_normalizes_native_crlf_and_cr_newlines(tmp_path: Path) -> None:
    source = tmp_path / "notes.txt"
    source.write_bytes("中文 note\r\nsecond\rthird\n".encode())
    normalized = "中文 note\nsecond\nthird\n"
    cache_budget = len(normalized.encode("utf-8"))

    inventory = FileInventory.build(tmp_path, text_cache_limit_bytes=cache_budget)
    entry = inventory.files[0]

    assert inventory.text(entry) == normalized
    assert inventory.text(entry) == normalized
    stats = inventory.text_scan_stats
    assert stats.cached_bytes == cache_budget
    assert stats.cache_hits == 1
    assert stats.skipped_budget == 0


def test_windows_metadata_fallback_rejects_same_size_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "notes.txt"
    source.write_bytes(b"safe\n")
    inventory = FileInventory.build(tmp_path)
    entry = inventory.files[0]
    replacement = b"evil\n"
    assert len(replacement) == entry.size
    original_metadata = source.stat()

    monkeypatch.setattr(detectors.os, "name", "nt")
    with (
        pytest.raises(ValueError, match="changed during copy"),
        inventory.open_entry(entry) as stream,
    ):
        assert stream.read() == b"safe\n"
        entry.absolute.write_bytes(replacement)
        os.utime(
            source,
            ns=(original_metadata.st_atime_ns, original_metadata.st_mtime_ns + 1_000_000_000),
        )


def test_windows_descriptor_metadata_rejects_mutation_when_path_snapshot_is_stale(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "notes.txt"
    source.write_bytes(b"safe\n")
    inventory = FileInventory.build(tmp_path)
    entry = inventory.files[0]
    original_stat = detectors.os.stat
    initial_path_metadata = original_stat(source, follow_symlinks=False)
    path_type = type(source)

    def stale_path_stat(path: object, *, follow_symlinks: bool = True) -> os.stat_result:
        if path_type(path) == source:
            return initial_path_metadata
        return original_stat(path, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(detectors.os, "name", "nt")
    monkeypatch.setattr(detectors.os, "stat", stale_path_stat)
    with (
        pytest.raises(ValueError, match="changed during copy"),
        inventory.open_entry(entry) as stream,
    ):
        assert stream.read() == b"safe\n"
        source.write_bytes(b"evil\n")
        os.utime(
            source,
            ns=(initial_path_metadata.st_atime_ns, initial_path_metadata.st_mtime_ns + 1_000_000),
        )


def test_windows_metadata_does_not_compare_path_and_descriptor_representations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "notes.txt"
    source.write_bytes(b"safe\n")
    inventory = FileInventory.build(tmp_path)
    entry = inventory.files[0]
    original_fstat = detectors.os.fstat

    def divergent_fstat(file_descriptor: int) -> SimpleNamespace:
        metadata = original_fstat(file_descriptor)
        return _metadata_with(
            metadata,
            st_dev=metadata.st_dev + 1,
            st_ino=metadata.st_ino + 1,
            st_mtime_ns=metadata.st_mtime_ns + 1,
            st_ctime_ns=metadata.st_ctime_ns + 1,
        )

    monkeypatch.setattr(detectors.os, "name", "nt")
    monkeypatch.setattr(detectors.os, "fstat", divergent_fstat)

    with inventory.open_entry(entry) as stream:
        assert stream.read() == b"safe\n"


def test_windows_metadata_fallback_rejects_same_size_replacement_before_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "notes.txt"
    source.write_bytes(b"safe\n")
    replacement = tmp_path / "replacement.txt"
    replacement.write_bytes(b"evil\n")
    inventory = FileInventory.build(tmp_path)
    entry = inventory.entry("notes.txt")
    assert entry is not None
    assert replacement.stat().st_size == entry.size
    os.replace(replacement, source)

    monkeypatch.setattr(detectors.os, "name", "nt")
    with (
        pytest.raises(ValueError, match="changed before copy"),
        inventory.open_entry(entry),
    ):
        pytest.fail("the replacement must not be exposed to the caller")


def test_windows_inventory_uses_fresh_path_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "notes.txt"
    source.write_bytes(b"safe\n")
    original_stat = detectors.os.stat
    path_type = type(source)
    calls: list[tuple[Path, bool]] = []

    def recording_stat(path: object, *, follow_symlinks: bool = True) -> os.stat_result:
        calls.append((path_type(path), follow_symlinks))
        return original_stat(path, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(detectors.os, "name", "nt")
    monkeypatch.setattr(detectors.os, "stat", recording_stat)
    monkeypatch.setattr(detectors, "Path", path_type)

    inventory = FileInventory.build(tmp_path)

    assert inventory.entry("notes.txt") is not None
    assert (source, False) in calls


def test_windows_inventory_excludes_reparse_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "notes.txt"
    source.write_bytes(b"safe\n")
    original_stat = detectors.os.stat
    path_type = type(source)

    def reparse_stat(path: object, *, follow_symlinks: bool = True) -> object:
        metadata = original_stat(path, follow_symlinks=follow_symlinks)
        return _windows_reparse_metadata(metadata) if path_type(path) == source else metadata

    monkeypatch.setattr(detectors.os, "name", "nt")
    monkeypatch.setattr(detectors.os, "stat", reparse_stat)
    monkeypatch.setattr(detectors, "Path", path_type)

    inventory = FileInventory.build(tmp_path)

    assert inventory.files == ()
    assert [(item.relative.as_posix(), item.reason) for item in inventory.excluded_entries] == [
        ("notes.txt", "reparse")
    ]


def test_windows_open_rejects_reparse_parent_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    nested = tmp_path / "nested"
    nested.mkdir()
    source = nested / "notes.txt"
    source.write_bytes(b"safe\n")
    inventory = FileInventory.build(tmp_path)
    entry = inventory.files[0]
    original_lstat = Path.lstat

    def reparse_lstat(path: Path) -> object:
        metadata = original_lstat(path)
        return _windows_reparse_metadata(metadata) if path == nested else metadata

    monkeypatch.setattr(detectors.os, "name", "nt")
    monkeypatch.setattr(Path, "lstat", reparse_lstat)

    with (
        pytest.raises(ValueError, match="symlink parent"),
        inventory.open_entry(entry),
    ):
        pytest.fail("a reparse parent must not be opened")


def test_large_file_is_skipped_without_reading_and_without_cache_content(tmp_path: Path) -> None:
    large = tmp_path / "large.txt"
    large.write_bytes(b"x" * 17)

    inventory = FileInventory.build(tmp_path, scan_limit_bytes=16)
    entry = inventory.files[0]

    assert inventory.text(entry) is None
    assert hasattr(inventory, "text_scan_stats")
    stats = inventory.text_scan_stats

    assert stats.read_count == 0
    assert stats.bytes_read == 0
    assert stats.skipped_large == 1
    assert stats.cached_bytes == 0


def test_nul_and_invalid_utf8_are_not_treated_as_text(tmp_path: Path) -> None:
    (tmp_path / "binary.dat").write_bytes(b"prefix\x00suffix")
    (tmp_path / "invalid.dat").write_bytes(b"\xff\xfe\xfd")

    inventory = FileInventory.build(tmp_path)

    assert all(inventory.text(entry) is None for entry in inventory.files)
    assert hasattr(inventory, "text_scan_stats")
    stats = inventory.text_scan_stats

    assert stats.skipped_nul == 1
    assert stats.skipped_binary == 1
    assert stats.skipped_decode == 1
    assert stats.read_count == 2


def test_symlinks_broken_links_and_special_files_are_not_inventory_files(tmp_path: Path) -> None:
    real = tmp_path / "real.txt"
    real.write_text("safe\n", encoding="utf-8")
    (tmp_path / "link.txt").symlink_to(real)
    (tmp_path / "broken.txt").symlink_to(tmp_path / "missing.txt")
    linked_directory = tmp_path / "linked-dir"
    linked_directory.symlink_to(tmp_path, target_is_directory=True)

    fifo = tmp_path / "named-pipe"
    if hasattr(os, "mkfifo"):
        os.mkfifo(fifo)

    inventory = FileInventory.build(tmp_path)
    relative_files = {entry.relative.as_posix() for entry in inventory.files}

    assert relative_files == {"real.txt"}
    assert "linked-dir" not in {path.as_posix() for path in inventory.directories}


def test_inventory_uses_one_scandir_for_a_ten_thousand_file_root(
    tmp_path: Path, monkeypatch
) -> None:
    for index in range(10_000):
        (tmp_path / f"file-{index:05d}.txt").write_text("x", encoding="utf-8")

    original_scandir = detectors.os.scandir
    scanned_directories: list[Path] = []

    def counting_scandir(path):
        scanned_directories.append(path if isinstance(path, int) else Path(path))
        return original_scandir(path)

    monkeypatch.setattr(detectors.os, "scandir", counting_scandir)
    assert "text_cache_limit_bytes" in inspect.signature(FileInventory.build).parameters
    inventory = FileInventory.build(tmp_path, scan_limit_bytes=8, text_cache_limit_bytes=1024)

    assert len(inventory.files) == 10_000
    assert len(scanned_directories) == 1

    for entry in inventory.files[:128]:
        inventory.text(entry)
        inventory.text(entry)

    stats = inventory.text_scan_stats
    assert stats.cached_bytes <= 1024
    assert stats.read_count <= stats.cached_entries
    assert stats.cache_hits == min(128, stats.cached_entries)
    assert stats.skipped_budget >= 0


def test_inventory_does_not_read_an_entry_that_is_replaced_by_a_symlink(
    tmp_path: Path,
) -> None:
    entry_path = tmp_path / "config.txt"
    entry_path.write_text("safe\n", encoding="utf-8")
    outside = tmp_path.parent / "outside-secret.txt"
    outside.write_text("CSBOX_SECRET_SENTINEL_outside\n", encoding="utf-8")

    inventory = FileInventory.build(tmp_path)
    entry_path.unlink()
    entry_path.symlink_to(outside)

    assert inventory.text(inventory.files[0]) is None


@pytest.mark.skipif(
    not (os.name == "posix" and hasattr(os, "O_DIRECTORY") and hasattr(os, "O_NOFOLLOW")),
    reason="pinned directory descriptors are a POSIX capability",
)
def test_inventory_reads_text_from_pinned_parent_directory(tmp_path: Path, monkeypatch) -> None:
    nested = tmp_path / "nested"
    nested.mkdir()
    entry_path = nested / "config.txt"
    entry_path.write_text("safe\n", encoding="utf-8")
    outside = tmp_path.parent / "outside-inventory-text"
    outside.mkdir()
    (outside / "config.txt").write_text("CSBOX_SECRET_SENTINEL_directory-race\n", encoding="utf-8")

    inventory = FileInventory.build(tmp_path)
    entry = inventory.entry(Path("nested/config.txt"))
    assert entry is not None
    original_open = detectors.os.open
    replaced = False

    def replacing_open(path, flags, *args, **kwargs):
        nonlocal replaced
        is_absolute_file_open = not isinstance(path, int) and Path(path) == entry.absolute
        is_pinned_file_open = path == "config.txt" and kwargs.get("dir_fd") is not None
        if (is_absolute_file_open or is_pinned_file_open) and not replaced:
            replaced = True
            nested.rename(tmp_path / "nested-real")
            nested.symlink_to(outside, target_is_directory=True)
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(detectors.os, "open", replacing_open)

    assert inventory.text(entry) == "safe\n"
    assert replaced is True


def test_inventory_opens_text_nonblocking_before_regular_file_check(
    tmp_path: Path, monkeypatch
) -> None:
    source = tmp_path / "stream.txt"
    source.write_text("safe\n", encoding="utf-8")
    inventory = FileInventory.build(tmp_path)
    entry = inventory.files[0]
    original_open = detectors.os.open
    observed_flags: list[int] = []

    def recording_open(path, flags, *args, **kwargs):
        is_absolute_file_open = not isinstance(path, int) and Path(path) == entry.absolute
        is_pinned_file_open = path == "stream.txt" and kwargs.get("dir_fd") is not None
        if is_absolute_file_open or is_pinned_file_open:
            observed_flags.append(flags)
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(detectors.os, "open", recording_open)
    assert inventory.text(entry) == "safe\n"
    assert observed_flags
    if hasattr(os, "O_NONBLOCK"):
        assert observed_flags[0] & os.O_NONBLOCK


@pytest.mark.skipif(
    os.name == "nt",
    reason="directory-descriptor replacement race is a POSIX-specific contract",
)
def test_inventory_does_not_follow_directory_replaced_by_a_symlink(
    tmp_path: Path, monkeypatch
) -> None:
    nested = tmp_path / "nested"
    nested.mkdir()
    (nested / "local.txt").write_text("local\n", encoding="utf-8")
    outside = tmp_path.parent / "outside-directory"
    outside.mkdir()
    (outside / "escaped.txt").write_text("CSBOX_SECRET_SENTINEL_escape\n", encoding="utf-8")

    original_open = detectors.os.open
    replaced = False

    def replacing_open(path, *args, **kwargs):
        nonlocal replaced
        if not isinstance(path, int) and Path(path) == nested and not replaced:
            replaced = True
            nested.rename(tmp_path / "nested-real")
            nested.symlink_to(outside, target_is_directory=True)
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(detectors.os, "open", replacing_open)

    inventory = FileInventory.build(tmp_path)

    assert replaced is True
    assert all("escaped.txt" not in entry.relative.as_posix() for entry in inventory.files)


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO is not available on this platform")
def test_inventory_does_not_block_on_fifo(tmp_path: Path) -> None:
    fifo = tmp_path / "stream"
    os.mkfifo(fifo)

    inventory = FileInventory.build(tmp_path)

    assert inventory.files == ()
