from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from typing import Literal

from csbox.core.display_width import display_width
from csbox.core.shell import ShellProfile
from csbox.lab.models import SessionPaths

_UNSAFE_BIDI_CLASSES = frozenset({"LRE", "RLE", "LRO", "RLO", "PDF", "LRI", "RLI", "FSI", "PDI"})
_MAX_EXPERIMENT_NAME_BYTES = 1024


class ExperimentNameError(ValueError):
    """An experiment name cannot be used in a Lab start request."""


@dataclass(frozen=True, slots=True)
class ShellOption:
    value: str
    label: str

    @classmethod
    def from_profile(cls, profile: ShellProfile) -> ShellOption:
        return cls(value=profile.kind.value, label=profile.display_name)


@dataclass(frozen=True, slots=True)
class LabStartRequest:
    experiment_name: str
    shell: str

    def __post_init__(self) -> None:
        normalized_name = self.experiment_name.strip()
        if not normalized_name:
            raise ExperimentNameError("请输入实验名称。")
        if any(
            unicodedata.category(character) in {"Cc", "Cs"}
            or unicodedata.bidirectional(character) in _UNSAFE_BIDI_CLASSES
            for character in normalized_name
        ):
            raise ExperimentNameError("实验名称不能包含控制字符。")
        if display_width(normalized_name) <= 0:
            raise ExperimentNameError("实验名称需要包含可见字符。")
        if len(normalized_name.encode("utf-8")) > _MAX_EXPERIMENT_NAME_BYTES:
            raise ExperimentNameError("实验名称过长，请缩短后重试。")
        normalized_shell = self.shell.strip()
        if not normalized_shell:
            raise ValueError("shell selection must not be empty")
        object.__setattr__(self, "experiment_name", normalized_name)
        object.__setattr__(self, "shell", normalized_shell)

    @classmethod
    def from_input(cls, experiment_name: str, shell: str) -> LabStartRequest:
        return cls(experiment_name=experiment_name, shell=shell)


@dataclass(frozen=True, slots=True)
class HomeNotice:
    message: str
    kind: Literal["info", "running", "completed", "interrupted", "failed", "warning"] = "info"


@dataclass(frozen=True, slots=True)
class ActiveLabSession:
    paths: SessionPaths
    experiment_name: str


def lab_status_notice(experiment_name: str, status: str) -> HomeNotice:
    if status == "running":
        return HomeNotice(
            f"实验“{experiment_name}”正在专用终端中运行；结束后此处会自动更新。",
            "running",
        )
    if status == "completed":
        return HomeNotice(f"实验“{experiment_name}”已完成。", "completed")
    if status == "interrupted":
        return HomeNotice(f"实验“{experiment_name}”已中断，记录已保留。", "interrupted")
    if status == "failed":
        return HomeNotice(f"实验“{experiment_name}”未能正常完成，记录已保留。", "failed")
    return HomeNotice(f"实验“{experiment_name}”的状态暂不可用。", "warning")


def no_available_shell_message(system: str) -> str:
    if system == "Windows":
        return (
            "未找到可用的 PowerShell。\n"
            "建议安装 PowerShell 7，或确认 Windows PowerShell 可以正常启动。"
        )
    return "未找到 Bash 或 Zsh。\n请确认系统 Shell 已正确安装。"


def lab_start_failure_notice(error: BaseException) -> HomeNotice:
    guidance = {
        "windows_terminal_unavailable": "未找到 Windows Terminal，请安装或修复后重试。",
        "windows_terminal_launch_failure": "Windows Terminal 无法启动，请检查安装后重试。",
        "shell_executable_unlaunchable": "所选 Shell 无法启动，请选择另一个已安装的 Shell。",
        "shell_executable_unavailable": "所选 Shell 已不可用，请返回后重新选择。",
        "conpty_initialization_failure": "Windows 终端环境无法初始化，请关闭其他终端后重试。",
        "runtime_backend_failure": "实验终端运行失败，请返回后重新开始实验。",
    }.get(
        getattr(error, "kind", None),
        "实验无法启动，请检查当前项目权限和 Shell 后重试。",
    )
    return HomeNotice(guidance, "failed")


def metadata_retry_notice(experiment_name: str) -> HomeNotice:
    return HomeNotice(
        f"暂时无法读取实验“{experiment_name}”的状态，正在自动重试。",
        "warning",
    )
