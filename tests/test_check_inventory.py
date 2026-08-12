from __future__ import annotations

import inspect
import os
from pathlib import Path

import pytest

import csbox.check.detectors as detectors
from csbox.check.detectors import FileInventory


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

    inventory = FileInventory.build(tmp_path)

    assert inventory.text(inventory.files[0]) == "中文 note\nsecond\nthird\n"


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
