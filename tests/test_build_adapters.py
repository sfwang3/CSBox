from __future__ import annotations

import json
from pathlib import Path

from csbox.check.build import (
    CommandResult,
    GradleBuildAdapter,
    MavenBuildAdapter,
    NodeBuildAdapter,
    PythonBuildAdapter,
)
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


def project(tmp_path: Path, kind: str, marker: str) -> DetectedProject:
    return DetectedProject(kind=kind, root=tmp_path, marker=marker)


def test_node_only_builds_when_scripts_build_exists(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text(json.dumps({"scripts": {"build": "vite build"}}))
    runner = RecordingRunner()

    outcome = NodeBuildAdapter(runner=runner, platform_name="linux").build(
        project(tmp_path, "node", "package.json"), tmp_path / "out"
    )

    assert outcome.status is CheckStatus.PASS
    assert runner.calls[0][0] == ("npm", "run", "build")
    assert runner.calls[0][1] == tmp_path


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
    assert runner.calls[1][0][0].endswith("gradlew.bat")


def test_python_requirements_only_project_is_skipped(tmp_path: Path) -> None:
    (tmp_path / "requirements.txt").write_text("pytest\n")
    runner = RecordingRunner()

    outcome = PythonBuildAdapter(runner=runner).build(
        project(tmp_path, "python", "requirements.txt"), tmp_path / "out"
    )

    assert outcome.status is CheckStatus.SKIP
    assert runner.calls == []
