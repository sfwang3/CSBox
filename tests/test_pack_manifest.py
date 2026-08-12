from __future__ import annotations

import hashlib
import importlib
import json
import zipfile
from contextlib import contextmanager
from pathlib import Path

import pytest
from typer.testing import CliRunner

import csbox.core.safe_paths as safe_paths_module
import csbox.pack.service as pack_service_module
from csbox.cli.main import app
from csbox.pack.service import PackService, PackServiceError

runner = CliRunner()
cli_module = importlib.import_module("csbox.cli.main")


def _project(root: Path) -> None:
    (root / "README.md").write_text("# 课程项目\n", encoding="utf-8")
    (root / "pyproject.toml").write_text(
        "[project]\nname = 'cjk-demo'\nversion = '0.1.0'\n",
        encoding="utf-8",
    )
    (root / "src" / "课程").mkdir(parents=True)
    (root / "src" / "课程" / "主程序.py").write_text("print('安全')\n", encoding="utf-8")


def test_plan_is_a_stable_dry_run_boundary_and_never_publishes(tmp_path: Path) -> None:
    source = tmp_path / "中文项目"
    source.mkdir()
    _project(source)
    (source / "node_modules" / "pkg").mkdir(parents=True)
    (source / "node_modules" / "pkg" / "index.js").write_text("ignored", encoding="utf-8")
    (source / "旧日志.log").write_text("ignored", encoding="utf-8")
    (source / ".env.example").write_text("TOKEN=replace-me\n", encoding="utf-8")
    secret = "CSBOX_SECRET_SENTINEL_pack_plan"
    (source / ".env").write_text(f"TOKEN={secret}\n", encoding="utf-8")
    destination = tmp_path / "deliverables" / "提交.zip"

    plan = PackService().plan(source, destination=destination)

    assert plan.source_root == source.resolve()
    assert plan.project_type == "python"
    assert plan.included == (
        ".env.example",
        "pyproject.toml",
        "README.md",
        "src/课程/主程序.py",
    )
    assert "node_modules:directory" in plan.excluded
    assert "旧日志.log:pattern" in plan.excluded
    assert any(item.endswith(":env") for item in plan.rejected)
    assert secret not in repr(plan)
    assert not destination.exists()


def test_dry_run_plain_and_json_are_stable_and_do_not_leave_staging(tmp_path: Path) -> None:
    source = tmp_path / "project"
    source.mkdir()
    _project(source)
    destination = tmp_path / "output.zip"

    plain_args = ["pack", str(source), "--output", str(destination), "--dry-run", "--plain"]
    first_plain = runner.invoke(app, plain_args)
    second_plain = runner.invoke(app, plain_args)
    json_result = runner.invoke(
        app,
        ["pack", str(source), "--output", str(destination), "--dry-run", "--json"],
    )

    assert first_plain.exit_code == 0
    assert second_plain.exit_code == 0
    assert first_plain.stdout == second_plain.stdout
    assert "included" in first_plain.stdout
    assert "excluded" in first_plain.stdout
    assert "rejected" in first_plain.stdout
    assert str(tmp_path) not in first_plain.stdout
    assert json_result.exit_code == 0
    payload = json.loads(json_result.stdout)
    assert payload["schema_version"] == 1
    assert payload["mode"] == "dry-run"
    assert payload["source_root"] == "."
    assert str(tmp_path) not in json_result.stdout
    assert not destination.exists()
    assert not any(path.name.startswith("csbox-pack-") for path in tmp_path.iterdir())


def test_pack_cli_config_failure_uses_safe_error_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "project"
    source.mkdir()
    secret = "CSBOX_SECRET_SENTINEL_pack_config"

    def fail_create_service(root: Path) -> object:
        del root
        raise ValueError(secret)

    monkeypatch.setattr(cli_module, "create_pack_service", fail_create_service)

    result = runner.invoke(app, ["pack", str(source), "--json"])

    assert result.exit_code == 1
    assert "项目打包失败" in result.stdout
    assert secret not in result.output


def test_sensitive_rejection_is_safe_and_env_example_remains_allowed(tmp_path: Path) -> None:
    source = tmp_path / "project"
    source.mkdir()
    _project(source)
    secret = "CSBOX_SECRET_SENTINEL_pack_rejection"
    (source / ".env").write_text(f"PASSWORD={secret}\n", encoding="utf-8")
    (source / ".env.example").write_text("PASSWORD=replace-me\n", encoding="utf-8")
    destination = tmp_path / "archive.zip"

    plan = PackService().plan(source, destination=destination)
    assert ".env.example" in plan.included
    assert any(item.endswith(":env") for item in plan.rejected)
    assert secret not in json.dumps(plan.model_dump(mode="json"), ensure_ascii=False)

    with pytest.raises(PackServiceError) as error:
        PackService().pack(source, destination=destination)
    assert secret not in str(error.value)
    assert not destination.exists()

    plain = runner.invoke(
        app,
        ["pack", str(source), "--output", str(destination), "--dry-run", "--plain"],
    )
    machine = runner.invoke(
        app,
        ["pack", str(source), "--output", str(destination), "--dry-run", "--json"],
    )
    assert plain.exit_code == 1
    assert machine.exit_code == 1
    assert secret not in plain.stdout
    assert secret not in machine.stdout
    assert any(item.endswith(":env") for item in json.loads(machine.stdout)["rejected"])


def test_untrusted_check_category_cannot_enter_rejection_detail(tmp_path: Path) -> None:
    source = tmp_path / "project"
    source.mkdir()
    _project(source)
    secret = "CSBOX_SECRET_SENTINEL_pack_category"
    finding = type("Finding", (), {"status": "FAIL", "category": secret, "path": None})()
    report = type("Report", (), {"exit_code": 1, "findings": (finding,)})()
    checker = type("Checker", (), {"run": lambda self, root, *, build=False: report})()

    plan = PackService(check_service=checker).plan(source)

    assert plan.rejected == ("project:check-failed",)
    assert secret not in repr(plan)


def test_private_key_marker_after_scan_window_is_rejected(tmp_path: Path) -> None:
    source = tmp_path / "project"
    source.mkdir()
    _project(source)
    (source / "notes.txt").write_text(
        "说明\n" + ("x" * 1024) + "-----BEGIN OPENSSH PRIVATE KEY-----\n",
        encoding="utf-8",
    )

    plan = PackService().plan(source, destination=tmp_path / "archive.zip")

    assert "notes.txt:private-key" in plan.rejected


def test_pack_is_byte_deterministic_with_normalized_zip_metadata(tmp_path: Path) -> None:
    source = tmp_path / "项目"
    source.mkdir()
    _project(source)
    destination = source / "提交.zip"
    service = PackService()

    first = service.pack(source, destination=destination, verify=True)
    first_bytes = destination.read_bytes()
    second = service.pack(source, destination=destination, verify=True, force=True)

    assert first.entries == second.entries
    assert first_bytes == destination.read_bytes()
    with zipfile.ZipFile(destination) as archive:
        assert archive.namelist() == sorted(
            archive.namelist(), key=lambda name: (name.casefold(), name)
        )
        assert archive.testzip() is None
        for info in archive.infolist():
            assert info.date_time == (1980, 1, 1, 0, 0, 0)
            assert info.external_attr == (0o100644 << 16)
            assert ".." not in Path(info.filename).parts


def test_manifest_is_strict_self_consistent_and_contains_only_relative_entries(
    tmp_path: Path,
) -> None:
    source = tmp_path / "项目"
    source.mkdir()
    _project(source)
    secret = "CSBOX_SECRET_SENTINEL_pack_manifest"
    (source / "notes.txt").write_text("公开说明\n", encoding="utf-8")
    (source / "private.txt").write_text(f"not a secret file: {secret}\n", encoding="utf-8")
    (source / "nested").mkdir()
    (source / "nested" / "manifest.json").write_text('{"source": true}\n', encoding="utf-8")
    destination = tmp_path / "交付" / "提交.zip"

    report = PackService().pack(
        source,
        destination=destination,
        include_manifest=True,
        verify=True,
    )

    with zipfile.ZipFile(destination) as archive:
        assert archive.namelist()[-1] == "manifest.json"
        raw_manifest = archive.read("manifest.json")
        manifest = json.loads(raw_manifest.decode("utf-8"), parse_constant=ValueError)
        assert manifest["schema_version"] == 1
        assert manifest["project_type"] == "python"
        assert manifest["verification"]["status"] == "verified"
        assert all(not Path(item["path"]).is_absolute() for item in manifest["files"])
        assert all(".." not in Path(item["path"]).parts for item in manifest["files"])
        assert all(item["path"] != "manifest.json" for item in manifest["files"])
        assert manifest["files"] == [
            {
                "path": name,
                "size": info.file_size,
                "sha256": hashlib.sha256(archive.read(name)).hexdigest(),
            }
            for name, info in ((item.filename, item) for item in archive.infolist())
            if name != "manifest.json"
        ]
        assert "nested/manifest.json" in archive.namelist()
        assert secret not in raw_manifest.decode("utf-8")

    assert report.entries[-1] == "manifest.json"


def test_symlink_and_unsafe_destination_are_never_followed(tmp_path: Path) -> None:
    source = tmp_path / "project"
    source.mkdir()
    _project(source)
    outside = tmp_path / "outside"
    outside.write_text("CSBOX_SECRET_SENTINEL_pack_outside\n", encoding="utf-8")
    link = source / "逃逸.txt"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symlink creation is unavailable")

    destination = tmp_path / "archive.zip"
    plan = PackService().plan(source, destination=destination)
    assert "逃逸.txt:symlink" in plan.excluded
    PackService().pack(source, destination=destination, verify=True)
    with zipfile.ZipFile(destination) as archive:
        assert "逃逸.txt" not in archive.namelist()
        assert all("outside" not in name for name in archive.namelist())
        assert all(
            "CSBOX_SECRET_SENTINEL_pack_outside" not in archive.read(name).decode("utf-8")
            for name in archive.namelist()
        )


def test_pack_does_not_follow_a_file_replaced_by_a_symlink_during_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "project"
    source.mkdir()
    _project(source)
    outside = tmp_path / "outside.txt"
    outside.write_text("CSBOX_SECRET_SENTINEL_pack_race\n", encoding="utf-8")
    destination = tmp_path / "archive.zip"

    from csbox.check.detectors import FileInventory

    original_open = FileInventory.open_entry
    replaced = False

    @contextmanager
    def replace_before_open(inventory, entry):
        nonlocal replaced
        if entry.relative.as_posix() == "README.md" and not replaced:
            entry.absolute.unlink()
            entry.absolute.symlink_to(outside)
            replaced = True
        with original_open(inventory, entry) as stream:
            yield stream

    monkeypatch.setattr(FileInventory, "open_entry", replace_before_open)
    with pytest.raises(PackServiceError) as error:
        PackService().pack(source, destination=destination)

    assert replaced is True
    assert "CSBOX_SECRET_SENTINEL_pack_race" not in str(error.value)
    assert not destination.exists()
    assert outside.read_text(encoding="utf-8") == "CSBOX_SECRET_SENTINEL_pack_race\n"


def test_pack_rejects_same_size_source_mutation_during_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "project"
    source.mkdir()
    _project(source)
    destination = tmp_path / "archive.zip"

    from csbox.check.detectors import FileInventory

    original_open = FileInventory.open_entry
    mutated = False

    @contextmanager
    def mutate_after_copy(inventory, entry):
        nonlocal mutated
        with original_open(inventory, entry) as stream:
            yield stream
            if entry.relative.as_posix() == "README.md" and not mutated:
                entry.absolute.write_text("# 课程示例\n", encoding="utf-8")
                mutated = True

    monkeypatch.setattr(FileInventory, "open_entry", mutate_after_copy)
    with pytest.raises(PackServiceError):
        PackService().pack(source, destination=destination)

    assert mutated is True
    assert not destination.exists()


def test_existing_output_directory_is_not_packed_into_itself(tmp_path: Path) -> None:
    source = tmp_path / "project"
    source.mkdir()
    _project(source)
    output_directory = source / "deliverables"
    output_directory.mkdir()
    old_archive = output_directory / "old.zip"
    old_archive.write_bytes(b"previous output")

    report = PackService().pack(source, destination=output_directory, verify=True)

    assert "deliverables/old.zip" not in report.entries
    with zipfile.ZipFile(output_directory / "student-project-course.zip") as archive:
        assert "deliverables/old.zip" not in archive.namelist()


def test_previous_pack_archive_is_excluded_when_followup_destination_changes(
    tmp_path: Path,
) -> None:
    source = tmp_path / "project"
    source.mkdir()
    _project(source)
    first_destination = source / "first.zip"
    second_destination = source / "second.zip"

    service = PackService()
    service.pack(source, destination=first_destination)
    second_plan = service.plan(source, destination=second_destination)

    assert "first.zip" not in second_plan.included
    assert "first.zip:archive" in second_plan.excluded

    service.pack(source, destination=second_destination, verify=True)
    with zipfile.ZipFile(second_destination) as archive:
        assert "first.zip" not in archive.namelist()


def test_stale_publish_temp_is_excluded_from_a_followup_pack(tmp_path: Path) -> None:
    source = tmp_path / "project"
    source.mkdir()
    _project(source)
    stale = source / ".student-project-course.zip-deadbeef.tmp"
    stale.write_text("incomplete output", encoding="utf-8")

    service = PackService()
    plan = service.plan(source)
    assert ".student-project-course.zip-deadbeef.tmp" not in plan.included
    assert ".student-project-course.zip-deadbeef.tmp:output" in plan.excluded

    destination = source / "student-project-course.zip"
    service.pack(source, destination=destination, verify=True)
    with zipfile.ZipFile(destination) as archive:
        assert stale.name not in archive.namelist()


def test_unicode_normalization_path_conflict_is_rejected(tmp_path: Path) -> None:
    source = tmp_path / "project"
    source.mkdir()
    _project(source)
    (source / "é.txt").write_text("one", encoding="utf-8")
    (source / "e\u0301.txt").write_text("two", encoding="utf-8")

    plan = PackService().plan(source, destination=tmp_path / "archive.zip")

    assert {item.split(":", 1)[0] for item in plan.rejected if item.endswith(":path-conflict")} == {
        "é.txt",
        "e\u0301.txt",
    }


def test_manifest_root_directory_conflict_rejects_pack(tmp_path: Path) -> None:
    source = tmp_path / "project"
    source.mkdir()
    _project(source)
    (source / "manifest.json").mkdir()
    (source / "manifest.json" / "part.txt").write_text("source", encoding="utf-8")

    plan = PackService().plan(
        source,
        destination=tmp_path / "archive.zip",
        include_manifest=True,
    )

    assert "manifest.json/part.txt:path-conflict" in plan.rejected


def test_plan_rejects_casefolded_ancestor_path_conflict_without_verify(
    tmp_path: Path,
) -> None:
    source = tmp_path / "project"
    source.mkdir()
    _project(source)
    (source / "Docs").write_text("file", encoding="utf-8")
    (source / "docs").mkdir()
    (source / "docs" / "index.txt").write_text("nested", encoding="utf-8")

    plan = PackService().plan(source, destination=tmp_path / "archive.zip")

    assert "Docs:path-conflict" in plan.rejected
    assert "docs/index.txt:path-conflict" in plan.rejected
    assert "Docs" not in plan.included
    assert "docs/index.txt" not in plan.included
    with pytest.raises(PackServiceError):
        PackService().pack(source, destination=tmp_path / "archive.zip")


def test_pack_plan_rejects_same_size_source_change_after_confirmation_preview(
    tmp_path: Path,
) -> None:
    source = tmp_path / "project"
    source.mkdir()
    _project(source)
    mutable = source / "mutable.txt"
    mutable.write_text("AAAA", encoding="utf-8")
    destination = tmp_path / "archive.zip"
    service = PackService()

    plan = service.plan(source, destination=destination)
    mutable.write_text("BBBB", encoding="utf-8")

    with pytest.raises(PackServiceError, match="打包计划已变化"):
        service.pack_plan(plan)
    assert not destination.exists()


def test_force_refuses_symlink_or_directory_destination(tmp_path: Path) -> None:
    source = tmp_path / "project"
    source.mkdir()
    _project(source)
    protected = tmp_path / "protected.zip"
    protected.write_bytes(b"protected")
    symlink_destination = tmp_path / "link.zip"
    try:
        symlink_destination.symlink_to(protected)
    except OSError:
        pytest.skip("symlink creation is unavailable")

    with pytest.raises(PackServiceError):
        PackService().pack(source, destination=symlink_destination, force=True)
    assert protected.read_bytes() == b"protected"

    directory_destination = tmp_path / "output-directory"
    directory_destination.mkdir()
    (directory_destination / "student-project-course.zip").mkdir()
    with pytest.raises(PackServiceError):
        PackService().pack(source, destination=directory_destination, force=True)
    assert directory_destination.is_dir()


def test_failed_publish_keeps_existing_archive_and_cleans_temporary_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "project"
    source.mkdir()
    _project(source)
    destination = tmp_path / "archive.zip"
    destination.write_bytes(b"original")

    def fail_publish(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise OSError("CSBOX_SECRET_SENTINEL_pack_publish")

    monkeypatch.setattr(pack_service_module, "atomic_copy_file", fail_publish)
    with pytest.raises(PackServiceError) as error:
        PackService().pack(source, destination=destination, force=True)

    assert str(error.value) == "打包失败，未发布输出文件。"
    assert destination.read_bytes() == b"original"
    assert not any(path.name.startswith(".archive.zip-") for path in tmp_path.iterdir())


def test_default_publish_does_not_replace_destination_created_during_publish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if not safe_paths_module._HAS_POSIX_DIRECTORY_FDS:
        pytest.skip("requires POSIX directory-fd publish primitives")
    source = tmp_path / "project"
    source.mkdir()
    _project(source)
    destination = tmp_path / "archive.zip"
    original_link = safe_paths_module.os.link

    def race_destination(*args: object, **kwargs: object) -> None:
        destination.write_bytes(b"raced")
        original_link(*args, **kwargs)

    monkeypatch.setattr(safe_paths_module.os, "link", race_destination)
    with pytest.raises(PackServiceError):
        PackService().pack(source, destination=destination)

    assert destination.read_bytes() == b"raced"


def test_pack_uses_one_inventory_scan_for_a_ten_thousand_file_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "large-project"
    source.mkdir()
    for index in range(10_000):
        (source / f"file-{index:05d}.txt").write_text("x", encoding="utf-8")
    destination = tmp_path / "large.zip"

    import csbox.check.detectors as detectors

    original_scandir = detectors.os.scandir
    original_build = detectors.FileInventory.build
    scanned_directories: list[object] = []
    build_calls = 0

    def counting_scandir(path: object):
        scanned_directories.append(path)
        return original_scandir(path)

    def counting_build(*args: object, **kwargs: object):
        nonlocal build_calls
        build_calls += 1
        return original_build(*args, **kwargs)

    monkeypatch.setattr(detectors.os, "scandir", counting_scandir)
    monkeypatch.setattr(detectors.FileInventory, "build", counting_build)
    report = PackService().pack(source, destination=destination, verify=True)

    assert build_calls == 1
    assert scanned_directories
    assert report.entries == tuple(f"file-{index:05d}.txt" for index in range(10_000))
    with zipfile.ZipFile(destination) as archive:
        assert archive.namelist() == list(report.entries)
