from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import PurePath

from csbox.core.registry import Registry


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


SHELL_PROFILES = Registry[ShellProfile]("shell-profile")
for _profile in (
    ShellProfile(ShellKind.POWERSHELL_51, "Windows PowerShell 5.1", "powershell.exe"),
    ShellProfile(ShellKind.POWERSHELL_7, "PowerShell 7", "pwsh.exe"),
    ShellProfile(ShellKind.BASH, "Bash", "bash"),
    ShellProfile(ShellKind.ZSH, "Zsh", "zsh"),
):
    SHELL_PROFILES.register(_profile.kind.value, _profile)


UNKNOWN_SHELL = ShellProfile(ShellKind.UNKNOWN, "", "")


def supported_shell_profiles() -> tuple[ShellProfile, ...]:
    return tuple(profile for _, profile in SHELL_PROFILES.items())


def _basename(value: str) -> str:
    normalized = value.replace("\\", "/")
    return PurePath(normalized).name.lower()


def detect_shell(
    *,
    environ: Mapping[str, str] | None = None,
    system: str | None = None,
    is_wsl: bool = False,
) -> ShellProfile:
    del is_wsl
    env = dict(environ or {})
    source = env.get("CSBOX_SHELL") or env.get("SHELL") or env.get("ComSpec") or ""
    executable = _basename(source)
    explicit = env.get("CSBOX_SHELL", "").strip().lower()

    aliases = {
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
    kind = aliases.get(explicit) or aliases.get(executable)

    if kind is None and system == "Windows" and env.get("PSVersion", "").startswith("7"):
        kind = ShellKind.POWERSHELL_7
    if kind is None and system == "Windows" and env.get("PSModulePath"):
        kind = ShellKind.POWERSHELL_51

    if kind is None:
        return UNKNOWN_SHELL
    return SHELL_PROFILES.get(kind.value)
