from __future__ import annotations

import os
import platform
import stat
import subprocess
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from csbox.check.detectors import FileInventory, node_has_build_script
from csbox.check.models import BuildOutcome, CheckStatus, DetectedProject
from csbox.core.subprocess_env import minimal_subprocess_environment


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
        del max_output
        pinned_fd: int | None = None
        actual_command = command
        try:
            actual_command, pinned_fd = _pin_project_executable(command, cwd)
            if actual_command is None:
                return CommandResult(None, "", "", error="unsafe-executable")
            run_kwargs = {
                "cwd": cwd,
                "stdout": subprocess.DEVNULL,
                "stderr": subprocess.DEVNULL,
                "env": minimal_subprocess_environment(),
                "timeout": timeout,
                "check": False,
                "shell": False,
            }
            if pinned_fd is not None:
                run_kwargs["pass_fds"] = (pinned_fd,)
            completed = subprocess.run(
                list(actual_command),
                **run_kwargs,
            )
        except FileNotFoundError:
            return CommandResult(None, "", "", error="missing-tool")
        except subprocess.TimeoutExpired:
            return CommandResult(None, "", "", timed_out=True)
        except OSError:
            return CommandResult(None, "", "", error="execution-error")
        finally:
            if pinned_fd is not None:
                with suppress(OSError):
                    os.close(pinned_fd)
        return CommandResult(
            completed.returncode,
            "",
            "",
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
        if self.is_windows and os.name != "posix":
            system_tool = {"mvnw": "mvn", "gradlew": "gradle"}.get(name, name)
            return self._tool(system_tool)
        candidates = (f"{name}.cmd", f"{name}.bat") if self.is_windows else (name,)
        for candidate in candidates:
            path = project.root / candidate
            if _is_safe_wrapper(path):
                return str(path)
        return self._tool(name)


class NodeBuildAdapter(BuildAdapter):
    adapter_id = "node"

    def build(
        self,
        project: DetectedProject,
        output_dir: Path,
        *,
        inventory: FileInventory | None = None,
    ) -> BuildOutcome:
        del output_dir
        if not node_has_build_script(project, inventory=inventory):
            return BuildOutcome(
                adapter_id=self.adapter_id,
                project=project,
                status=CheckStatus.SKIP,
                message="Node 项目没有 scripts.build，跳过构建。",
            )
        if project.package_manager not in {"npm", "pnpm", "yarn"}:
            return BuildOutcome(
                adapter_id=self.adapter_id,
                project=project,
                status=CheckStatus.SKIP,
                message="未根据 lockfile 或 manifest 确认 Node 包管理器，跳过构建。",
            )
        return self._run(project, (self._tool(project.package_manager), "run", "build"))


class MavenBuildAdapter(BuildAdapter):
    adapter_id = "maven"

    def build(
        self,
        project: DetectedProject,
        output_dir: Path,
        *,
        inventory: FileInventory | None = None,
    ) -> BuildOutcome:
        del output_dir, inventory
        command = (self._wrapper(project, "mvnw"), "-B", "package", "-DskipTests")
        return self._run(project, command)


class GradleBuildAdapter(BuildAdapter):
    adapter_id = "gradle"

    def build(
        self,
        project: DetectedProject,
        output_dir: Path,
        *,
        inventory: FileInventory | None = None,
    ) -> BuildOutcome:
        del output_dir, inventory
        command = (self._wrapper(project, "gradlew"), "build", "-x", "test")
        return self._run(project, command)


class PythonBuildAdapter(BuildAdapter):
    adapter_id = "python"

    def build(
        self,
        project: DetectedProject,
        output_dir: Path,
        *,
        inventory: FileInventory | None = None,
    ) -> BuildOutcome:
        del output_dir, inventory
        if project.marker != "pyproject.toml":
            return BuildOutcome(
                adapter_id=self.adapter_id,
                project=project,
                status=CheckStatus.SKIP,
                message="仅有依赖清单，没有可可靠执行的 Python 构建目标。",
            )
        if project.package_manager not in {"uv", "poetry"}:
            return BuildOutcome(
                adapter_id=self.adapter_id,
                project=project,
                status=CheckStatus.SKIP,
                message="未根据项目文件确认 Python 包管理器，跳过构建。",
            )
        return self._run(project, (self._tool(project.package_manager), "build"))


DEFAULT_BUILD_ADAPTERS: dict[str, type[BuildAdapter]] = {
    "node": NodeBuildAdapter,
    "maven": MavenBuildAdapter,
    "gradle": GradleBuildAdapter,
    "python": PythonBuildAdapter,
}


def _is_safe_wrapper(path: Path) -> bool:
    if path.is_symlink():
        return False
    flags = os.O_RDONLY
    if hasattr(os, "O_NONBLOCK"):
        flags |= os.O_NONBLOCK
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor: int | None = None
    try:
        descriptor = os.open(path, flags)
        return stat.S_ISREG(os.fstat(descriptor).st_mode)
    except OSError:
        return False
    finally:
        if descriptor is not None:
            with suppress(OSError):
                os.close(descriptor)


def _pin_project_executable(
    command: tuple[str, ...], cwd: Path
) -> tuple[tuple[str, ...] | None, int | None]:
    if not command or not Path(command[0]).is_absolute():
        return command, None
    if os.name != "posix":
        try:
            Path(command[0]).relative_to(Path(cwd).resolve(strict=True))
        except (OSError, ValueError):
            return command, None
        return None, None
    if not hasattr(os, "O_DIRECTORY") or not hasattr(os, "O_NOFOLLOW"):
        return command, None
    try:
        root = Path(cwd).resolve(strict=True)
        executable = Path(command[0])
        relative = executable.relative_to(root)
    except (OSError, ValueError):
        return command, None

    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        directory_flags |= os.O_CLOEXEC
    descriptor_flags = os.O_RDONLY | os.O_NOFOLLOW
    if hasattr(os, "O_NONBLOCK"):
        descriptor_flags |= os.O_NONBLOCK
    if hasattr(os, "O_CLOEXEC"):
        descriptor_flags |= os.O_CLOEXEC
    descriptor: int | None = None
    parent_fd: int | None = None
    try:
        parent_fd = os.open(root, directory_flags)
        for part in relative.parts[:-1]:
            next_fd = os.open(part, directory_flags, dir_fd=parent_fd)
            os.close(parent_fd)
            parent_fd = next_fd
        descriptor = os.open(relative.parts[-1], descriptor_flags, dir_fd=parent_fd)
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            os.close(descriptor)
            descriptor = None
            return None, None
        return (f"/proc/self/fd/{descriptor}", *command[1:]), descriptor
    except (OSError, ValueError):
        if descriptor is not None:
            with suppress(OSError):
                os.close(descriptor)
        return None, None
    finally:
        if parent_fd is not None:
            with suppress(OSError):
                os.close(parent_fd)


__all__ = [
    "CommandResult",
    "DEFAULT_BUILD_ADAPTERS",
    "GradleBuildAdapter",
    "MavenBuildAdapter",
    "NodeBuildAdapter",
    "PythonBuildAdapter",
    "SubprocessCommandRunner",
]
