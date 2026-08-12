from __future__ import annotations

import json
import os
import shlex
import stat
import subprocess
import zipfile
from pathlib import Path

import pytest

import csbox.check.build as build_module
import csbox.check.rules as rules_module
import csbox.core.safe_paths as safe_paths
import csbox.pack.service as pack_service_module
from csbox.check.build import SubprocessCommandRunner
from csbox.check.detectors import FileInventory
from csbox.check.models import CheckStatus
from csbox.check.service import CheckService
from csbox.core.events import TerminalSize
from csbox.lab.captures import CaptureStore
from csbox.lab.recorder import AsciicastV3Recorder
from csbox.lab.repository import SessionRepository, SessionRepositoryError
from csbox.pack.service import PackService, PackServiceError

SECRET = "CSBOX_SECRET_SENTINEL_task17_warning_value"


def test_check_scans_uncached_text_after_the_cache_budget_is_exhausted(
    tmp_path: Path,
) -> None:
    (tmp_path / "a-safe.txt").write_text("safe text fills cache\n", encoding="utf-8")
    secret_path = tmp_path / "z-secret.py"
    secret_path.write_text(f'token = "{SECRET}"\n', encoding="utf-8")
    inventory = FileInventory.build(
        tmp_path,
        scan_limit_bytes=1024,
        text_cache_limit_bytes=24,
    )

    report = CheckService().run(tmp_path, inventory=inventory)

    findings = [
        finding
        for finding in report.findings
        if finding.rule_id == "hard-coded-secret" and finding.status is CheckStatus.FAIL
    ]
    assert [finding.path for finding in findings] == [Path("z-secret.py")]
    assert SECRET not in report.model_dump_json()
    assert report.text_scan_stats["skipped_budget"] >= 1


def test_check_and_pack_stream_scan_secrets_beyond_the_text_cache_limit(
    tmp_path: Path,
) -> None:
    source = tmp_path / "project"
    source.mkdir()
    (source / "README.md").write_text("# project\n", encoding="utf-8")
    secret_path = source / "large-source.py"
    secret_path.write_text(
        "# harmless padding\n" * 20_000 + f'token = "{SECRET}"\n',
        encoding="utf-8",
    )

    report = CheckService().run(source)
    plan = PackService().plan(source, destination=tmp_path / "project.zip")

    assert secret_path.stat().st_size > 256 * 1024
    assert any(
        finding.rule_id == "hard-coded-secret"
        and finding.status is CheckStatus.FAIL
        and finding.path == Path("large-source.py")
        for finding in report.findings
    )
    assert any(item == "large-source.py:hard-coded-secret" for item in plan.rejected)
    assert SECRET not in report.model_dump_json()
    assert SECRET not in repr(plan)


def test_check_and_pack_stream_scan_secret_assignment_across_chunk_boundary(
    tmp_path: Path,
) -> None:
    source = tmp_path / "project"
    source.mkdir()
    (source / "README.md").write_text("# project\n", encoding="utf-8")
    whitespace = b" " * 600
    prefix = b"#" * ((1024 * 1024) - len(b"token") - len(whitespace))
    secret_path = source / "large-source.py"
    secret_path.write_bytes(
        prefix + b"token" + whitespace + b'= "CSBOX_SECRET_SENTINEL_task17_boundary"\n'
    )

    report = CheckService().run(source)
    plan = PackService().plan(source, destination=tmp_path / "project.zip")

    assert any(
        finding.rule_id == "hard-coded-secret"
        and finding.status is CheckStatus.FAIL
        and finding.path == Path("large-source.py")
        for finding in report.findings
    )
    assert any(item == "large-source.py:hard-coded-secret" for item in plan.rejected)


@pytest.mark.parametrize("prefix", [b"\x00", b"\xff"])
def test_check_and_pack_scan_ascii_secrets_in_non_text_files(
    tmp_path: Path,
    prefix: bytes,
) -> None:
    source = tmp_path / "project"
    source.mkdir()
    (source / "README.md").write_text("# project\n", encoding="utf-8")
    candidate = source / "wrapped-secret.dat"
    candidate.write_bytes(prefix + b'token = "CSBOX_SECRET_SENTINEL_task17_binary"\n')

    report = CheckService().run(source)
    plan = PackService().plan(source, destination=tmp_path / "project.zip")

    assert any(
        finding.rule_id == "hard-coded-secret"
        and finding.status is CheckStatus.FAIL
        and finding.path == Path("wrapped-secret.dat")
        for finding in report.findings
    )
    assert any(item == "wrapped-secret.dat:hard-coded-secret" for item in plan.rejected)


def test_build_runner_discards_child_output_and_uses_a_minimal_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    monkeypatch.setenv("CSBOX_VAR_BUILD_SECRET", SECRET)
    monkeypatch.setenv("NODE_OPTIONS", f"--require={SECRET}")
    monkeypatch.setenv("PATH", os.environ.get("PATH", ""))

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        captured["command"] = command
        captured.update(kwargs)
        return subprocess.CompletedProcess(command, 0, stdout=None, stderr=None)

    monkeypatch.setattr(build_module.subprocess, "run", fake_run)

    result = SubprocessCommandRunner().run(
        ("tool", "build"),
        cwd=tmp_path,
        timeout=3,
        max_output=32,
    )

    assert captured["command"] == ["tool", "build"]
    assert captured["shell"] is False
    assert captured["stdout"] is subprocess.DEVNULL
    assert captured["stderr"] is subprocess.DEVNULL
    assert "capture_output" not in captured
    assert "text" not in captured
    child_env = captured["env"]
    assert isinstance(child_env, dict)
    assert child_env.get("PATH") == os.environ.get("PATH", "")
    assert "CSBOX_VAR_BUILD_SECRET" not in child_env
    assert "NODE_OPTIONS" not in child_env
    assert SECRET not in child_env.values()
    assert result.returncode == 0
    assert result.stdout == result.stderr == ""


def test_git_status_uses_bounded_file_output_and_does_not_inherit_secret_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "CSBOX_SECRET_SENTINEL_task17_git_env"
    captured: dict[str, object] = {}
    monkeypatch.setenv("CSBOX_VAR_GIT_SECRET", secret)

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        captured["command"] = command
        captured.update(kwargs)
        return subprocess.CompletedProcess(command, 128)

    monkeypatch.setattr(rules_module.subprocess, "run", fake_run)

    CheckService().run(tmp_path)

    assert captured["shell"] is False
    assert captured["stderr"] is subprocess.DEVNULL
    output = captured["stdout"]
    assert hasattr(output, "fileno")
    child_env = captured["env"]
    assert isinstance(child_env, dict)
    assert "CSBOX_VAR_GIT_SECRET" not in child_env
    assert secret not in child_env.values()


@pytest.mark.skipif(os.name == "nt", reason="the real fsmonitor hook fixture uses POSIX exec")
def test_git_status_disables_repository_fsmonitor_hook(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    subprocess.run(
        ["git", "init", "--quiet", str(project)],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    marker = tmp_path / "fsmonitor-was-executed"
    hook = tmp_path / "untrusted-fsmonitor"
    hook.write_text(
        f"#!/bin/sh\nprintf invoked > {shlex.quote(str(marker))}\nprintf '1\\n'\n",
        encoding="utf-8",
    )
    hook.chmod(0o700)
    subprocess.run(
        ["git", "-C", str(project), "config", "core.fsmonitor", str(hook)],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    CheckService().run(project)

    assert not marker.exists()


def test_bounded_regular_reader_rejects_oversized_and_special_files(tmp_path: Path) -> None:
    reader = getattr(safe_paths, "read_regular_text", None)
    assert callable(reader), "Task 17 requires a shared bounded regular-file reader"

    oversized = tmp_path / "oversized.txt"
    oversized.write_text("中" * 20, encoding="utf-8")
    with pytest.raises(ValueError, match="limit|large|大小|上限"):
        reader(oversized, max_bytes=16)

    if not hasattr(os, "mkfifo"):
        pytest.skip("FIFO creation is unavailable")
    fifo = tmp_path / "input.pipe"
    os.mkfifo(fifo)
    with pytest.raises(ValueError, match="regular|普通文件"):
        reader(fifo, max_bytes=16)


def test_lab_repository_rejects_a_symlinked_parent_without_writing_outside(
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    linked_state = tmp_path / ".csbox"
    try:
        linked_state.symlink_to(outside, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"symlinks are unavailable: {error}")

    repository = SessionRepository.from_cwd(tmp_path)
    with pytest.raises((OSError, ValueError, SessionRepositoryError)):
        repository.create_running(
            "安全会话",
            shell="bash",
            shell_version="5",
            size=TerminalSize(columns=80, rows=24),
            cwd=tmp_path,
            session_id="task17-session",
        )

    assert not (outside / "sessions").exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits are not portable to Windows")
def test_lab_session_state_is_private_even_under_a_permissive_umask(tmp_path: Path) -> None:
    previous_umask = os.umask(0o022)
    try:
        repository = SessionRepository(tmp_path / ".csbox" / "sessions")
        paths = repository.create_running(
            "私有会话",
            shell="bash",
            shell_version="5",
            size=TerminalSize(columns=80, rows=24),
            cwd=tmp_path,
            session_id="private-session",
        )
        recorder = AsciicastV3Recorder(paths.cast, columns=80, rows=24)
        recorder.close()
    finally:
        os.umask(previous_umask)

    assert stat.S_IMODE(repository.root.stat().st_mode) == 0o700
    assert stat.S_IMODE(paths.root.stat().st_mode) == 0o700
    assert stat.S_IMODE(paths.metadata.stat().st_mode) == 0o600
    assert stat.S_IMODE(paths.cast.stat().st_mode) == 0o600


def test_capture_validation_warnings_never_echo_invalid_secret_values(tmp_path: Path) -> None:
    path = tmp_path / "captures.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "captures": [
                    {
                        "id": "capture-1",
                        "title": "invalid",
                        "createdAt": "2026-08-12T00:00:00Z",
                        "timestamp": SECRET,
                        "rows": 1,
                        "columns": 1,
                        "cwd": ".",
                        "snapshot": {
                            "rows": 1,
                            "columns": 1,
                            "cells": [[{"character": "", "width": 1}]],
                            "cursor": {"row": 0, "column": 0, "visible": True},
                            "relativeTime": 0,
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    loaded = CaptureStore(path).load()

    assert loaded.captures == ()
    assert loaded.warnings
    assert SECRET not in repr(loaded.warnings)


@pytest.mark.skipif(os.name == "nt", reason="Windows cannot create reserved path fixtures")
@pytest.mark.parametrize("name", ["CON", "report.", "file:stream"])
def test_pack_rejects_zip_paths_that_are_unsafe_on_windows(tmp_path: Path, name: str) -> None:
    source = tmp_path / "project"
    source.mkdir()
    (source / "README.md").write_text("# safe\n", encoding="utf-8")
    (source / name).write_text("safe\n", encoding="utf-8")
    destination = tmp_path / f"{name.replace(':', '-')}.zip"
    service = PackService()

    plan = service.plan(source, destination=destination)

    assert any(item.startswith(f"{name}:") for item in plan.rejected)
    with pytest.raises(PackServiceError, match="不安全|拒绝"):
        service.pack(source, destination=destination)


@pytest.mark.parametrize("name", ["CON", "report.", "file:stream"])
def test_portable_zip_path_rejects_native_windows_reserved_components(name: str) -> None:
    assert not pack_service_module._is_portable_zip_path(name)


def test_pack_verification_streams_each_entry_without_testzip_or_full_reads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "project"
    source.mkdir()
    (source / "README.md").write_text("# project\n", encoding="utf-8")
    (source / "large.bin").write_bytes(b"x" * (2 * 1024 * 1024))
    destination = tmp_path / "project.zip"
    original_read = zipfile.ZipExtFile.read
    read_sizes: list[int] = []

    def reject_testzip(self: zipfile.ZipFile) -> None:
        del self
        raise AssertionError("testzip would read every archive entry a second time")

    def bounded_read(self: zipfile.ZipExtFile, size: int = -1) -> bytes:
        assert 0 < size <= 1024 * 1024
        read_sizes.append(size)
        return original_read(self, size)

    monkeypatch.setattr(zipfile.ZipFile, "testzip", reject_testzip)
    monkeypatch.setattr(zipfile.ZipExtFile, "read", bounded_read)

    report = PackService().pack(
        source,
        destination=destination,
        include_manifest=True,
        verify=True,
    )

    assert report.verified is True
    assert read_sizes


def test_zip_name_conflict_validation_has_bounded_prefix_comparisons(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    comparisons = 0

    class CountingIdentity(str):
        def startswith(self, prefix: str | tuple[str, ...], *args: int) -> bool:
            nonlocal comparisons
            comparisons += 1
            return super().startswith(prefix, *args)

    original_identity = pack_service_module._path_identity_key
    monkeypatch.setattr(
        pack_service_module,
        "_path_identity_key",
        lambda value: CountingIdentity(original_identity(value)),
    )
    names = tuple(f"directory-{index:04d}/file.txt" for index in range(500))

    pack_service_module._validate_zip_names(names)

    assert comparisons <= len(names) * 4


def test_pack_generated_filename_respects_utf8_and_windows_component_budgets(
    tmp_path: Path,
) -> None:
    source = tmp_path / "project"
    source.mkdir()
    (source / "README.md").write_text("# project\n", encoding="utf-8")
    service = PackService(
        filename_template="{name}.zip",
        student_name="实验😀" * 100,
    )

    plan = service.plan(source)

    assert len(plan.destination.name.encode("utf-8")) <= 240
    assert len(plan.destination.name.encode("utf-16-le")) // 2 <= 240
    assert plan.destination.suffix == ".zip"


def test_pack_generated_filename_avoids_windows_reserved_basename(tmp_path: Path) -> None:
    source = tmp_path / "project"
    source.mkdir()
    (source / "README.md").write_text("# project\n", encoding="utf-8")
    service = PackService(filename_template="{name}.zip", student_name="CON")

    plan = service.plan(source)

    assert plan.destination.name != "CON.zip"
    assert pack_service_module._is_portable_zip_path(plan.destination.name)
