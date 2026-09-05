from __future__ import annotations

import ntpath
import os
import platform
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import PurePath, PureWindowsPath

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

    def __init__(
        self,
        user_message: str,
        cause: Exception,
        *,
        kind: str = "shell_executable_unavailable",
        executable: str | None = None,
    ) -> None:
        super().__init__(user_message)
        self.user_message = user_message
        self.cause = cause
        self.kind = kind
        self.debug_context = () if executable is None else (f"resolved_executable={executable}",)


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
    env = dict(os.environ if environ is None else environ)
    source = env.get("CSBOX_SHELL") or env.get("SHELL") or env.get("ComSpec") or ""
    explicit = env.get("CSBOX_SHELL", "")
    kind = _kind_for(explicit) or _kind_for(source)

    if kind is None and system == "Windows" and env.get("PSVersion", "").startswith("7"):
        kind = ShellKind.POWERSHELL_7

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
    which: Callable[..., str | None] | None = None,
) -> ShellProfile:
    """Select an available shell according to explicit, configured, and platform order."""

    env = os.environ if environ is None else environ
    os_name = platform.system() if system is None else system
    find_command = shutil.which if which is None else which
    search_path = _environment_value(env, "PATH")

    preferred = requested or config_shell
    if preferred:
        profile = _profile_for_requested(preferred)
        resolved = _resolve_profile_executable(
            profile,
            system=os_name,
            environ=env,
            search_path=search_path,
            which=find_command,
        )
        if resolved is None:
            cause = FileNotFoundError(profile.executable)
            _raise_shell_error(_unavailable_message(profile), cause)
        return replace(profile, executable=resolved)

    current = detect_shell(environ=env, system=os_name)
    if current is not UNKNOWN_SHELL:
        resolved = _resolve_profile_executable(
            current,
            system=os_name,
            environ=env,
            search_path=search_path,
            which=find_command,
        )
        if resolved is not None:
            return replace(current, executable=resolved)

    ordered_kinds: Sequence[ShellKind]
    if os_name == "Windows":
        ordered_kinds = (ShellKind.POWERSHELL_7, ShellKind.POWERSHELL_51)
    else:
        ordered_kinds = (ShellKind.BASH, ShellKind.ZSH)
    for kind in ordered_kinds:
        profile = SHELL_PROFILES.get(kind.value)
        resolved = _resolve_profile_executable(
            profile,
            system=os_name,
            environ=env,
            search_path=search_path,
            which=find_command,
        )
        if resolved is not None:
            return replace(profile, executable=resolved)

    cause = FileNotFoundError("no supported shell executable")
    if os_name == "Windows":
        message = "未找到可用的 Shell。请安装 PowerShell 7，或启用 Windows PowerShell。"
    else:
        message = "未找到可用的 Shell。请安装 bash 或 zsh。"
    _raise_shell_error(message, cause)


def _resolve_profile_executable(
    profile: ShellProfile,
    *,
    system: str,
    environ: Mapping[str, str],
    search_path: str | None,
    which: Callable[..., str | None],
) -> str | None:
    resolved = _locate_executable(
        profile.executable,
        system=system,
        search_path=search_path,
        which=which,
    )
    if resolved is None or system != "Windows" or not _is_windows_app_alias(resolved, environ):
        return resolved
    # Do not serialize an App Execution Alias into the dedicated host request. pywinpty
    # ultimately builds a CreateProcess command line, where an alias path containing spaces
    # can be interpreted as an invalid executable target.
    filtered_path = _without_windows_app_alias_paths(search_path, environ)
    native = _locate_executable(
        profile.executable,
        system=system,
        search_path=filtered_path,
        which=which,
    )
    if native is not None:
        return native
    if profile.kind is ShellKind.POWERSHELL_7:
        native = _resolve_powershell_execution_alias(resolved, environ)
        if native is not None:
            return native
        cause = OSError("PowerShell App Execution Alias did not resolve to a native executable")
        raise ShellUnavailableError(
            "PowerShell 7 的应用执行别名无法启动。请重新安装 PowerShell 7。",
            cause,
            kind="shell_executable_unlaunchable",
            executable=resolved,
        ) from cause
    return resolved


def _locate_executable(
    command: str,
    *,
    system: str,
    search_path: str | None,
    which: Callable[..., str | None],
) -> str | None:
    if system != "Windows":
        return which(command, path=search_path)
    if _is_fully_qualified_windows_path(command):
        candidates = (command,)
    else:
        candidates = tuple(
            ntpath.join(entry, command)
            for entry in (search_path or "").split(";")
            if entry and _is_fully_qualified_windows_path(entry)
        )
    for candidate in candidates:
        # Supplying a directory-qualified candidate prevents shutil.which from applying
        # Windows' implicit current-directory search ahead of the requested PATH entry.
        resolved = which(candidate, path="")
        if resolved is not None and _is_fully_qualified_windows_path(resolved):
            return ntpath.normpath(resolved)
    return None


def _is_fully_qualified_windows_path(value: str) -> bool:
    return PureWindowsPath(value).is_absolute()


def _resolve_powershell_execution_alias(
    alias: str,
    environ: Mapping[str, str],
) -> str | None:
    arguments = [
        alias,
        "-NoLogo",
        "-NoProfile",
        "-NonInteractive",
        "-Command",
        "[Console]::Out.Write([Environment]::ProcessPath)",
    ]
    try:
        with tempfile.TemporaryFile() as output_file:
            subprocess.run(
                arguments,
                stdout=output_file,
                stderr=subprocess.STDOUT,
                env=minimal_subprocess_environment(environ),
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
    try:
        resolved = raw_output.decode("utf-8", errors="strict").strip()
    except UnicodeError:
        return None
    if (
        not resolved
        or not _is_fully_qualified_windows_path(resolved)
        or ntpath.basename(resolved).casefold() != "pwsh.exe"
        or _is_windows_app_alias(resolved, environ)
        or not os.path.isfile(resolved)
    ):
        return None
    return ntpath.normpath(resolved)


def _environment_value(environ: Mapping[str, str], name: str) -> str | None:
    target = name.casefold()
    return next((value for key, value in environ.items() if key.casefold() == target), None)


def _windows_app_alias_root(environ: Mapping[str, str]) -> str | None:
    local_app_data = _environment_value(environ, "LOCALAPPDATA")
    if not local_app_data:
        return None
    return ntpath.normcase(ntpath.normpath(ntpath.join(local_app_data, "Microsoft", "WindowsApps")))


def _is_windows_app_alias(path: str, environ: Mapping[str, str]) -> bool:
    root = _windows_app_alias_root(environ)
    if root is None:
        return False
    candidate = ntpath.normcase(ntpath.normpath(path))
    return candidate == root or candidate.startswith(root + ntpath.sep)


def _without_windows_app_alias_paths(
    search_path: str | None,
    environ: Mapping[str, str],
) -> str | None:
    if search_path is None:
        return None
    root = _windows_app_alias_root(environ)
    if root is None:
        return search_path
    kept = []
    for entry in search_path.split(";"):
        if not entry:
            continue
        candidate = ntpath.normcase(ntpath.normpath(entry))
        if candidate != root and not candidate.startswith(root + ntpath.sep):
            kept.append(entry)
    return ";".join(kept)


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
