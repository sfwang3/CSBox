from __future__ import annotations

import os
import platform
import shutil
from collections.abc import Callable, Mapping

from csbox.core.models import EnvironmentSnapshot
from csbox.core.shell import detect_shell


def detect_is_wsl(
    *,
    environ: Mapping[str, str] | None = None,
    release: str | None = None,
) -> bool:
    env = os.environ if environ is None else environ
    if env.get("WSL_DISTRO_NAME") or env.get("WSL_INTEROP"):
        return True
    release_value = platform.release() if release is None else release
    release_lower = release_value.lower()
    return "microsoft-standard" in release_lower or "microsoft-wsl" in release_lower


def detect_terminal_size(
    size_provider: Callable[[], os.terminal_size] | None = None,
    *,
    fallback: tuple[int, int] = (80, 24),
) -> tuple[int, int]:
    try:
        size = (
            shutil.get_terminal_size(fallback=fallback)
            if size_provider is None
            else size_provider()
        )
    except OSError:
        return fallback
    if size.columns <= 0 or size.lines <= 0:
        return fallback
    return size.columns, size.lines


def detect_environment(
    *,
    environ: Mapping[str, str] | None = None,
    system: str | None = None,
    release: str | None = None,
    python_version: str | None = None,
    which: Callable[[str], str | None] | None = None,
    size_provider: Callable[[], os.terminal_size] | None = None,
) -> EnvironmentSnapshot:
    env = os.environ if environ is None else environ
    os_name = platform.system() if system is None else system
    os_version = platform.release() if release is None else release
    wsl = detect_is_wsl(environ=env, release=os_version)
    shell_profile = detect_shell(environ=env, system=os_name, is_wsl=wsl)
    find_command = shutil.which if which is None else which
    columns, rows = detect_terminal_size(size_provider)

    return EnvironmentSnapshot(
        os_name=os_name,
        os_version=os_version,
        python_version=platform.python_version() if python_version is None else python_version,
        shell=shell_profile.display_name,
        shell_executable=shell_profile.executable or None,
        powershell_51_available=find_command("powershell.exe") is not None,
        powershell_7_available=find_command("pwsh.exe") is not None,
        is_wsl=wsl,
        terminal_columns=columns,
        terminal_rows=rows,
    )
