from __future__ import annotations

import inspect
import json
from pathlib import Path

from csbox.check import detectors as detector_module
from csbox.check.build import CommandResult
from csbox.check.models import CheckFinding, CheckStatus
from csbox.check.service import CheckService, CheckServiceError
from csbox.cli.main import _check_json_payload, _print_check_plain, _strict_json


class FailingIfCalledRunner:
    def __init__(self) -> None:
        self.called = False

    def run(self, *args: object, **kwargs: object) -> CommandResult:
        del args, kwargs
        self.called = True
        raise AssertionError("build runner must not run without --build")


def test_check_does_not_build_by_default(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("# demo\n")
    (tmp_path / "pyproject.toml").write_text("[project]\nname='demo'\n")
    runner = FailingIfCalledRunner()

    report = CheckService(command_runner=runner).run(tmp_path)

    assert runner.called is False
    assert report.projects[0].kind == "python"


def test_check_build_appends_outcomes_and_never_exposes_raw_failure(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("# demo\n")
    (tmp_path / "pyproject.toml").write_text("[project]\nname='demo'\n")
    (tmp_path / "uv.lock").write_text("version = 1\n")

    class Runner:
        def run(self, *args: object, **kwargs: object) -> CommandResult:
            del args, kwargs
            return CommandResult(returncode=1, stdout="secret build output", stderr="tool failed")

    report = CheckService(command_runner=Runner()).run(tmp_path, build=True)

    assert report.builds
    assert report.builds[0].status is CheckStatus.FAIL
    assert "secret build output" not in report.model_dump_json()


def test_check_builds_one_inventory_and_reuses_it_for_detectors(
    tmp_path: Path, monkeypatch
) -> None:
    (tmp_path / "README.md").write_text("# demo\n", encoding="utf-8")
    (tmp_path / "pom.xml").write_text("<project />", encoding="utf-8")
    import csbox.check.service as service_module

    original_build = service_module.FileInventory.build
    calls = 0

    def counting_build(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original_build(*args, **kwargs)

    monkeypatch.setattr(service_module.FileInventory, "build", counting_build)

    assert "deep" in inspect.signature(CheckService.run).parameters
    report = CheckService().run(tmp_path)

    assert calls == 1
    assert report.projects[0].kind == "maven"
    assert report.text_scan_stats["read_count"] >= 0


def test_check_deep_scan_is_optional_and_is_serialized_with_scan_stats(
    tmp_path: Path, monkeypatch
) -> None:
    (tmp_path / "README.md").write_text("# demo\n", encoding="utf-8")
    expected = CheckFinding(
        rule_id="deep-secret-scan",
        status=CheckStatus.SKIP,
        message="未找到 gitleaks。",
        category="deep-secret-scan",
    )
    monkeypatch.setattr(
        "csbox.check.service.run_deep_secret_scan", lambda root: expected, raising=False
    )

    assert "deep" in inspect.signature(CheckService.run).parameters
    report = CheckService().run(tmp_path, deep=True)

    assert report.deep_scan == expected
    assert report.text_scan_stats["cached_entries"] >= 0
    assert report.deep_scan.category == "deep-secret-scan"


def test_check_service_error_does_not_expose_project_path(tmp_path: Path) -> None:
    missing = tmp_path / "missing-project"

    try:
        CheckService().run(missing)
    except CheckServiceError as error:
        assert str(tmp_path) not in str(error)
        assert error.__cause__ is None
    else:
        raise AssertionError("missing project should fail safely")


def test_check_report_and_outputs_stay_stable_for_ten_thousand_files(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    for index in range(10_000):
        (tmp_path / f"file-{index:05d}.txt").write_text("x", encoding="utf-8")

    original_scandir = detector_module.os.scandir
    scanned_directories: list[object] = []

    def counting_scandir(path):
        scanned_directories.append(path)
        return original_scandir(path)

    monkeypatch.setattr(detector_module.os, "scandir", counting_scandir)
    report = CheckService().run(tmp_path)

    assert len(report.projects) == 0
    assert len(scanned_directories) == 1
    assert report.text_scan_stats["read_count"] == 10_000

    payload = _check_json_payload(report)
    serialized = _strict_json(payload)
    assert json.loads(serialized)["root"] == "."

    _print_check_plain(report)
    assert "项目：." in capsys.readouterr().out


def test_check_treats_deep_json_and_toml_manifests_as_unknown_metadata(
    tmp_path: Path,
) -> None:
    node = tmp_path / "node"
    node.mkdir()
    (node / "package.json").write_text(
        '{"scripts":' + ("[" * 10_000) + ("0" + "]" * 10_000) + "}",
        encoding="utf-8",
    )
    python = tmp_path / "python"
    python.mkdir()
    (python / "pyproject.toml").write_text(
        "x = " + ("[" * 10_000) + ("0" + "]" * 10_000),
        encoding="utf-8",
    )

    node_report = CheckService().run(node)
    python_report = CheckService().run(python)

    assert [project.kind for project in node_report.projects] == ["node"]
    assert [project.kind for project in python_report.projects] == ["python"]
