from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import csbox.check.build as build_module
from csbox.check.build import (
    CommandResult,
    GradleBuildAdapter,
    MavenBuildAdapter,
    NodeBuildAdapter,
    PythonBuildAdapter,
    SubprocessCommandRunner,
)
from csbox.check.detectors import MAX_TEXT_SCAN_BYTES, FileInventory
from csbox.check.models import CheckStatus, DetectedProject


class RecordingRunner:
    def __init__(self, result: CommandResult | None = None) -> None:
        self.calls: list[tuple[tuple[str, ...], Path, float, int]] = []
        self.result = result or CommandResult(returncode=0, stdout="built", stderr="")

    def run(
        self,
        command: tuple[str, ...],
        *,
        cwd: Path,
        timeout: float,
        max_output: int,
    ) -> CommandResult:
        self.calls.append((command, cwd, timeout, max_output))
        return self.result


def project(
    tmp_path: Path,
    kind: str,
    marker: str,
    package_manager: str | None = None,
) -> DetectedProject:
    return DetectedProject(
        kind=kind,
        root=tmp_path,
        marker=marker,
        package_manager=package_manager,
    )


def test_node_only_builds_when_scripts_build_exists(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text(json.dumps({"scripts": {"build": "vite build"}}))
    runner = RecordingRunner()

    outcome = NodeBuildAdapter(runner=runner, platform_name="linux").build(
        project(tmp_path, "node", "package.json", "npm"), tmp_path / "out"
    )

    assert outcome.status is CheckStatus.PASS
    assert runner.calls[0][0] == ("npm", "run", "build")
    assert runner.calls[0][1] == tmp_path


@pytest.mark.parametrize("manager", ["npm", "pnpm", "yarn"])
def test_node_build_uses_detected_package_manager(tmp_path: Path, manager: str) -> None:
    (tmp_path / "package.json").write_text(json.dumps({"scripts": {"build": "build"}}))
    runner = RecordingRunner()

    outcome = NodeBuildAdapter(runner=runner, platform_name="linux").build(
        project(tmp_path, "node", "package.json", manager), tmp_path / "out"
    )

    assert outcome.status is CheckStatus.PASS
    assert runner.calls[0][0] == (manager, "run", "build")


def test_node_build_does_not_guess_a_missing_package_manager(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text(json.dumps({"scripts": {"build": "build"}}))
    runner = RecordingRunner()

    outcome = NodeBuildAdapter(runner=runner, platform_name="linux").build(
        project(tmp_path, "node", "package.json"), tmp_path / "out"
    )

    assert outcome.status is CheckStatus.SKIP
    assert runner.calls == []


def test_node_build_skips_an_oversized_manifest_without_unbounded_read(
    tmp_path: Path,
) -> None:
    manifest = b'{"scripts":{"build":"build"}}' + b" " * MAX_TEXT_SCAN_BYTES
    (tmp_path / "package.json").write_bytes(manifest)
    runner = RecordingRunner()

    outcome = NodeBuildAdapter(runner=runner, platform_name="linux").build(
        project(tmp_path, "node", "package.json", "npm"), tmp_path / "out"
    )

    assert outcome.status is CheckStatus.SKIP
    assert runner.calls == []


def test_node_build_reuses_the_shared_inventory_text_cache(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text(
        json.dumps({"scripts": {"build": "build"}}), encoding="utf-8"
    )
    inventory = FileInventory.build(tmp_path)
    runner = RecordingRunner()

    outcome = NodeBuildAdapter(runner=runner, platform_name="linux").build(
        project(tmp_path, "node", "package.json", "npm"),
        tmp_path / "out",
        inventory=inventory,
    )

    assert outcome.status is CheckStatus.PASS
    assert inventory.text_scan_stats.read_count == 1
    assert inventory.text_scan_stats.requested == 1


def test_maven_and_gradle_prefer_wrappers_and_add_windows_suffix(tmp_path: Path) -> None:
    (tmp_path / "mvnw").write_text("#!/bin/sh\n")
    (tmp_path / "gradlew.bat").write_text("@echo off\n")
    runner = RecordingRunner()

    MavenBuildAdapter(runner=runner, platform_name="linux").build(
        project(tmp_path, "maven", "pom.xml"), tmp_path / "out"
    )
    GradleBuildAdapter(runner=runner, platform_name="windows").build(
        project(tmp_path, "gradle", "build.gradle"), tmp_path / "out"
    )

    assert runner.calls[0][0][0].endswith("mvnw")
    if os.name == "nt":
        assert runner.calls[1][0][0].endswith("gradle.cmd")
    else:
        assert runner.calls[1][0][0].endswith("gradlew.bat")


def test_build_adapters_do_not_select_symlink_wrappers(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside-mvnw"
    outside.write_text("#!/bin/sh\n", encoding="utf-8")
    (tmp_path / "mvnw").symlink_to(outside)
    runner = RecordingRunner()

    MavenBuildAdapter(runner=runner, platform_name="linux").build(
        project(tmp_path, "maven", "pom.xml"), tmp_path / "out"
    )

    assert runner.calls[0][0][0] == "mvnw"


@pytest.mark.skipif(
    not (os.name == "posix" and Path("/proc/self/fd").is_dir()),
    reason="pinned executable descriptors require procfs on POSIX",
)
def test_subprocess_runner_pins_project_wrapper_descriptor(tmp_path: Path) -> None:
    wrapper = tmp_path / "mvnw"
    wrapper.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    wrapper.chmod(0o755)

    result = SubprocessCommandRunner().run(
        (str(wrapper),),
        cwd=tmp_path,
        timeout=5,
        max_output=1024,
    )

    assert result.returncode == 0
    assert result.error is None


def test_windows_build_does_not_execute_a_project_wrapper(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "mvnw.cmd").write_text("@echo off\n", encoding="utf-8")
    monkeypatch.setattr(build_module.os, "name", "nt")
    runner = RecordingRunner()

    MavenBuildAdapter(runner=runner, platform_name="windows").build(
        project(tmp_path, "maven", "pom.xml"), tmp_path / "out"
    )

    assert runner.calls[0][0][0] == "mvn.cmd"


def test_python_requirements_only_project_is_skipped(tmp_path: Path) -> None:
    (tmp_path / "requirements.txt").write_text("pytest\n")
    runner = RecordingRunner()

    outcome = PythonBuildAdapter(runner=runner).build(
        project(tmp_path, "python", "requirements.txt"), tmp_path / "out"
    )

    assert outcome.status is CheckStatus.SKIP
    assert runner.calls == []


@pytest.mark.parametrize("manager", ["uv", "poetry"])
def test_python_build_uses_detected_package_manager(tmp_path: Path, manager: str) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'demo'\n")
    runner = RecordingRunner()

    outcome = PythonBuildAdapter(runner=runner).build(
        project(tmp_path, "python", "pyproject.toml", manager), tmp_path / "out"
    )

    assert outcome.status is CheckStatus.PASS
    assert runner.calls[0][0] == (manager, "build")
