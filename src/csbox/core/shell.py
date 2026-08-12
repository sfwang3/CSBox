from __future__ import annotations

import os
import platform
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import PurePath

from csbox.core.registry import Registry
from csbox.core.subprocess_env import minimal_subprocess_environment

_MAX_SHELL_VERSION_BYTES = 4096


class ShellKind(StrEnum):
    POWERSHELL_51 = "powershell_51"
    POWERSHELL_7 = "powershell_7"
    BASH = "bash"
    ZSH = "zsh"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class ShellProfile:
    kind: ShellKind
    display_name: str
    executable: str
    launch_arguments: tuple[str, ...] = ()
    version_arguments: tuple[str, ...] = ()

    @property
    def command(self) -> tuple[str, ...]:
        return (self.executable, *self.launch_arguments)

    @property
    def version_command(self) -> tuple[str, ...]:
        return (self.executable, *self.version_arguments)


PowerShell51Profile = ShellProfile(
    ShellKind.POWERSHELL_51,
    "Windows PowerShell 5.1",
    "powershell.exe",
    ("-NoLogo",),
    (
        "-NoLogo",
        "-NoProfile",
        "-NonInteractive",
        "-Command",
        "$PSVersionTable.PSVersion.ToString()",
    ),
)
PowerShell7Profile = ShellProfile(
    ShellKind.POWERSHELL_7,
    "PowerShell 7",
    "pwsh.exe",
    ("-NoLogo",),
    (
        "-NoLogo",
        "-NoProfile",
        "-NonInteractive",
        "-Command",
        "$PSVersionTable.PSVersion.ToString()",
    ),
)
BashProfile = ShellProfile(
    ShellKind.BASH,
    "Bash",
    "bash",
    ("--noprofile", "--norc"),
    ("--version",),
)
ZshProfile = ShellProfile(
    ShellKind.ZSH,
    "Zsh",
    "zsh",
    ("-f",),
    ("--version",),
)


SHELL_PROFILES = Registry[ShellProfile]("shell-profile")
for _profile in (PowerShell51Profile, PowerShell7Profile, BashProfile, ZshProfile):
    SHELL_PROFILES.register(_profile.kind.value, _profile)


UNKNOWN_SHELL = ShellProfile(ShellKind.UNKNOWN, "", "")


_SHELL_ALIASES = {
    "powershell_51": ShellKind.POWERSHELL_51,
    "powershell5": ShellKind.POWERSHELL_51,
    "powershell.exe": ShellKind.POWERSHELL_51,
    "powershell": ShellKind.POWERSHELL_51,
    "powershell_7": ShellKind.POWERSHELL_7,
    "powershell7": ShellKind.POWERSHELL_7,
    "pwsh.exe": ShellKind.POWERSHELL_7,
    "pwsh": ShellKind.POWERSHELL_7,
    "bash": ShellKind.BASH,
    "zsh": ShellKind.ZSH,
}


class ShellUnavailableError(RuntimeError):
    """A shell-selection failure with stable Chinese user-facing guidance."""

    def __init__(self, user_message: str, cause: Exception) -> None:
        super().__init__(user_message)
        self.user_message = user_message
        self.cause = cause


def supported_shell_profiles() -> tuple[ShellProfile, ...]:
    return tuple(profile for _, profile in SHELL_PROFILES.items())


def _basename(value: str) -> str:
    normalized = value.replace("\\", "/")
    return PurePath(normalized).name.lower()


def _kind_for(value: str) -> ShellKind | None:
    normalized = value.strip().lower()
    return _SHELL_ALIASES.get(normalized) or _SHELL_ALIASES.get(_basename(normalized))


def detect_shell(
    *,
    environ: Mapping[str, str] | None = None,
    system: str | None = None,
    is_wsl: bool = False,
) -> ShellProfile:
    del is_wsl
    env = dict(environ or {})
    source = env.get("CSBOX_SHELL") or env.get("SHELL") or env.get("ComSpec") or ""
    explicit = env.get("CSBOX_SHELL", "")
    kind = _kind_for(explicit) or _kind_for(source)

    if kind is None and system == "Windows" and env.get("PSVersion", "").startswith("7"):
        kind = ShellKind.POWERSHELL_7
    if kind is None and system == "Windows" and env.get("PSModulePath"):
        kind = ShellKind.POWERSHELL_51

    if kind is None:
        return UNKNOWN_SHELL
    return SHELL_PROFILES.get(kind.value)


def _raise_shell_error(message: str, cause: Exception) -> None:
    raise ShellUnavailableError(message, cause) from cause


def _unavailable_message(profile: ShellProfile) -> str:
    if profile is PowerShell7Profile:
        return "未找到 pwsh.exe。请安装 PowerShell 7，或使用 --shell powershell。"
    if profile is PowerShell51Profile:
        return "未找到 powershell.exe。请启用 Windows PowerShell，或使用 --shell pwsh。"
    if profile is BashProfile:
        return "未找到 bash。请安装 Bash，或使用 --shell zsh。"
    return "未找到 zsh。请安装 Zsh，或使用 --shell bash。"


def _profile_for_requested(value: str) -> ShellProfile:
    kind = _kind_for(value)
    if kind is None:
        cause = ValueError(f"unsupported shell: {value}")
        _raise_shell_error(f"不支持的 Shell：{value}。可选 powershell、pwsh、bash 或 zsh。", cause)
    return SHELL_PROFILES.get(kind.value)


def select_shell(
    requested: str | None,
    config_shell: str | None,
    system: str | None = None,
    environ: Mapping[str, str] | None = None,
    which: Callable[[str], str | None] | None = None,
) -> ShellProfile:
    """Select an available shell according to explicit, configured, and platform order."""

    env = os.environ if environ is None else environ
    os_name = platform.system() if system is None else system
    find_command = shutil.which if which is None else which

    preferred = requested or config_shell
    if preferred:
        profile = _profile_for_requested(preferred)
        if find_command(profile.executable) is None:
            cause = FileNotFoundError(profile.executable)
            _raise_shell_error(_unavailable_message(profile), cause)
        return profile

    current = detect_shell(environ=env, system=os_name)
    if current is not UNKNOWN_SHELL and find_command(current.executable) is not None:
        return current

    ordered_kinds: Sequence[ShellKind]
    if os_name == "Windows":
        ordered_kinds = (ShellKind.POWERSHELL_7, ShellKind.POWERSHELL_51)
    else:
        ordered_kinds = (ShellKind.BASH, ShellKind.ZSH)
    for kind in ordered_kinds:
        profile = SHELL_PROFILES.get(kind.value)
        if find_command(profile.executable) is not None:
            return profile

    cause = FileNotFoundError("no supported shell executable")
    if os_name == "Windows":
        message = "未找到可用的 Shell。请安装 PowerShell 7，或启用 Windows PowerShell。"
    else:
        message = "未找到可用的 Shell。请安装 bash 或 zsh。"
    _raise_shell_error(message, cause)


def detect_shell_version(profile: ShellProfile) -> str | None:
    """Probe a shell version without preventing interactive startup on failure."""

    if not profile.version_command:
        return None
    try:
        with tempfile.TemporaryFile() as output_file:
            subprocess.run(
                list(profile.version_command),
                stdout=output_file,
                stderr=subprocess.STDOUT,
                env=minimal_subprocess_environment(),
                check=True,
                timeout=1.0,
                shell=False,
            )
            output_file.seek(0)
            raw_output = output_file.read(_MAX_SHELL_VERSION_BYTES + 1)
    except (OSError, subprocess.SubprocessError):
        return None
    if len(raw_output) > _MAX_SHELL_VERSION_BYTES:
        return None
    output = raw_output.decode("utf-8", errors="replace").strip()
    return output.splitlines()[0] if output else None
