import os

from csbox.core.environment import detect_environment, detect_is_wsl, detect_terminal_size
from csbox.core.shell import ShellKind, detect_shell


def test_detects_powershell_7_from_explicit_shell() -> None:
    profile = detect_shell(
        environ={"CSBOX_SHELL": "pwsh.exe"},
        system="Windows",
        is_wsl=False,
    )

    assert profile.kind is ShellKind.POWERSHELL_7
    assert profile.executable == "pwsh.exe"


def test_unknown_shell_profile_has_no_user_facing_fallback_copy() -> None:
    profile = detect_shell(environ={}, system="Linux", is_wsl=False)

    assert profile.kind is ShellKind.UNKNOWN
    assert profile.display_name == ""


def test_detects_windows_powershell_51_from_explicit_shell() -> None:
    profile = detect_shell(
        environ={"CSBOX_SHELL": "powershell.exe"},
        system="Windows",
        is_wsl=False,
    )

    assert profile.kind is ShellKind.POWERSHELL_51
    assert profile.display_name == "Windows PowerShell 5.1"


def test_detects_zsh_and_wsl_bash_from_shell_environment() -> None:
    assert (
        detect_shell(environ={"SHELL": "/bin/zsh"}, system="Linux", is_wsl=False).kind
        is ShellKind.ZSH
    )
    assert (
        detect_shell(
            environ={"SHELL": "/bin/bash", "WSL_DISTRO_NAME": "Ubuntu"},
            system="Linux",
            is_wsl=True,
        ).kind
        is ShellKind.BASH
    )


def test_detects_wsl_from_environment_or_kernel_release() -> None:
    assert detect_is_wsl(environ={"WSL_INTEROP": "/run/WSL/1_interop"}, release="Linux")
    assert detect_is_wsl(
        environ={},
        release="5.15.153.1-microsoft-standard-WSL2",
    )
    assert not detect_is_wsl(environ={}, release="6.8.0-generic")


def test_detects_terminal_size_and_falls_back_to_80_by_24() -> None:
    assert detect_terminal_size(lambda: os.terminal_size((120, 30))) == (120, 30)
    assert detect_terminal_size(lambda: (_ for _ in ()).throw(OSError("not a tty"))) == (80, 24)


def test_collects_environment_with_injected_platform_inputs() -> None:
    available = {"powershell.exe": "C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"}

    snapshot = detect_environment(
        environ={"CSBOX_SHELL": "powershell.exe", "WSL_DISTRO_NAME": ""},
        system="Windows",
        release="10.0",
        python_version="3.12.3",
        which=available.get,
        size_provider=lambda: os.terminal_size((100, 26)),
    )

    assert snapshot.os_name == "Windows"
    assert snapshot.python_version == "3.12.3"
    assert snapshot.shell == "Windows PowerShell 5.1"
    assert snapshot.powershell_51_available is True
    assert snapshot.powershell_7_available is False
    assert snapshot.is_wsl is False
    assert (snapshot.terminal_columns, snapshot.terminal_rows) == (100, 26)
