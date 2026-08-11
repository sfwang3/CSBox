from __future__ import annotations

import json
import platform
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from csbox.check.detectors import node_has_build_script
from csbox.check.models import BuildOutcome, CheckStatus, DetectedProject


@dataclass(frozen=True, slots=True)
class CommandResult:
    returncode: int | None
    stdout: str
    stderr: str
    timed_out: bool = False
    error: str | None = None


class CommandRunner(Protocol):
    def run(
        self,
        command: tuple[str, ...],
        *,
        cwd: Path,
        timeout: float,
        max_output: int,
    ) -> CommandResult: ...


class SubprocessCommandRunner:
    def run(
        self,
        command: tuple[str, ...],
        *,
        cwd: Path,
        timeout: float,
        max_output: int,
    ) -> CommandResult:
        try:
            completed = subprocess.run(
                list(command),
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
                shell=False,
            )
        except FileNotFoundError:
            return CommandResult(None, "", "", error="missing-tool")
        except subprocess.TimeoutExpired:
            return CommandResult(None, "", "", timed_out=True)
        except OSError:
            return CommandResult(None, "", "", error="execution-error")
        return CommandResult(
            completed.returncode,
            completed.stdout[:max_output],
            completed.stderr[:max_output],
        )


class BuildAdapter:
    adapter_id: str

    def __init__(
        self,
        *,
        runner: CommandRunner | None = None,
        platform_name: str | None = None,
        timeout: float = 120.0,
        max_output: int = 64 * 1024,
    ) -> None:
        self.runner = runner or SubprocessCommandRunner()
        self.platform_name = (platform_name or platform.system()).casefold()
        self.timeout = timeout
        self.max_output = max_output

    @property
    def is_windows(self) -> bool:
        return self.platform_name.startswith("win")

    def _run(self, project: DetectedProject, command: tuple[str, ...]) -> BuildOutcome:
        result = self.runner.run(
            command,
            cwd=project.root,
            timeout=self.timeout,
            max_output=self.max_output,
        )
        if result.timed_out:
            status = CheckStatus.WARN
            message = "构建超时，请检查构建过程。"
        elif result.error == "missing-tool":
            status = CheckStatus.SKIP
            message = "未找到构建工具。"
        elif result.error:
            status = CheckStatus.FAIL
            message = "构建进程无法启动。"
        elif result.returncode == 0:
            status = CheckStatus.PASS
            message = "构建完成。"
        else:
            status = CheckStatus.FAIL
            message = "构建失败。"
        return BuildOutcome(
            adapter_id=self.adapter_id,
            project=project,
            status=status,
            message=message,
            command=command,
            exit_code=result.returncode,
        )

    def _tool(self, name: str) -> str:
        return f"{name}.cmd" if self.is_windows else name

    def _wrapper(self, project: DetectedProject, name: str) -> str:
        candidates = (f"{name}.cmd", f"{name}.bat") if self.is_windows else (name,)
        for candidate in candidates:
            path = project.root / candidate
            if path.is_file():
                return str(path)
        return self._tool(name)


class NodeBuildAdapter(BuildAdapter):
    adapter_id = "node"

    def build(self, project: DetectedProject, output_dir: Path) -> BuildOutcome:
        del output_dir
        try:
            package = json.loads((project.root / project.marker).read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return BuildOutcome(
                adapter_id=self.adapter_id,
                project=project,
                status=CheckStatus.SKIP,
                message="package.json 无法读取，跳过 Node 构建。",
            )
        if not node_has_build_script(project):
            return BuildOutcome(
                adapter_id=self.adapter_id,
                project=project,
                status=CheckStatus.SKIP,
                message="Node 项目没有 scripts.build，跳过构建。",
            )
        del package
        return self._run(project, (self._tool("npm"), "run", "build"))


class MavenBuildAdapter(BuildAdapter):
    adapter_id = "maven"

    def build(self, project: DetectedProject, output_dir: Path) -> BuildOutcome:
        del output_dir
        command = (self._wrapper(project, "mvnw"), "-B", "package", "-DskipTests")
        return self._run(project, command)


class GradleBuildAdapter(BuildAdapter):
    adapter_id = "gradle"

    def build(self, project: DetectedProject, output_dir: Path) -> BuildOutcome:
        del output_dir
        command = (self._wrapper(project, "gradlew"), "build", "-x", "test")
        return self._run(project, command)


class PythonBuildAdapter(BuildAdapter):
    adapter_id = "python"

    def build(self, project: DetectedProject, output_dir: Path) -> BuildOutcome:
        del output_dir
        if project.marker != "pyproject.toml":
            return BuildOutcome(
                adapter_id=self.adapter_id,
                project=project,
                status=CheckStatus.SKIP,
                message="仅有依赖清单，没有可可靠执行的 Python 构建目标。",
            )
        return self._run(project, (self._tool("uv"), "build"))


DEFAULT_BUILD_ADAPTERS: dict[str, type[BuildAdapter]] = {
    "node": NodeBuildAdapter,
    "maven": MavenBuildAdapter,
    "gradle": GradleBuildAdapter,
    "python": PythonBuildAdapter,
}


__all__ = [
    "CommandResult",
    "DEFAULT_BUILD_ADAPTERS",
    "GradleBuildAdapter",
    "MavenBuildAdapter",
    "NodeBuildAdapter",
    "PythonBuildAdapter",
    "SubprocessCommandRunner",
]
