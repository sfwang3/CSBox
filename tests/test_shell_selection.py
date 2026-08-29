from __future__ import annotations

import ntpath
import subprocess
from collections.abc import Callable

import pytest

from csbox.core.shell import (
    BashProfile,
    PowerShell7Profile,
    PowerShell51Profile,
    ShellKind,
    ShellUnavailableError,
    ZshProfile,
    detect_shell_version,
    select_shell,
)


def available(*commands: str) -> Callable[..., str | None]:
    paths = {command: f"/available/{command}" for command in commands}

    def locate(command: str, *, path: str | None = None) -> str | None:
        del path
        if ntpath.dirname(command) and ntpath.basename(command) in paths:
            return command
        return paths.get(command)

    return locate


def windows_locator(*files: str) -> Callable[..., str | None]:
    available_files = {ntpath.normcase(ntpath.normpath(file)): file for file in files}

    def locate(command: str, *, path: str | None = None) -> str | None:
        for directory in (path or "").split(";"):
            candidate = ntpath.normcase(ntpath.normpath(ntpath.join(directory, command)))
            if candidate in available_files:
                return available_files[candidate]
        return None

    return locate


@pytest.mark.parametrize(
    ("alias", "expected_kind"),
    [
        ("powershell", ShellKind.POWERSHELL_51),
        ("powershell.exe", ShellKind.POWERSHELL_51),
        ("powershell5", ShellKind.POWERSHELL_51),
        ("powershell_51", ShellKind.POWERSHELL_51),
        ("pwsh", ShellKind.POWERSHELL_7),
        ("pwsh.exe", ShellKind.POWERSHELL_7),
        ("powershell7", ShellKind.POWERSHELL_7),
        ("powershell_7", ShellKind.POWERSHELL_7),
        ("bash", ShellKind.BASH),
        ("/bin/bash", ShellKind.BASH),
        ("zsh", ShellKind.ZSH),
        ("/usr/bin/zsh", ShellKind.ZSH),
    ],
)
def test_explicit_shell_aliases_select_the_requested_profile(
    alias: str, expected_kind: ShellKind
) -> None:
    command = {
        ShellKind.POWERSHELL_51: "powershell.exe",
        ShellKind.POWERSHELL_7: "pwsh.exe",
        ShellKind.BASH: "bash",
        ShellKind.ZSH: "zsh",
    }[expected_kind]

    profile = select_shell(
        alias,
        None,
        system="Windows" if "power" in alias or "pwsh" in alias else "Linux",
        environ=({"PATH": r"C:\available"} if "power" in alias or "pwsh" in alias else {}),
        which=available(command),
    )

    assert profile.kind is expected_kind


def test_requested_shell_takes_priority_over_config_and_current_shell() -> None:
    profile = select_shell(
        "bash",
        "zsh",
        system="Linux",
        environ={"SHELL": "/bin/zsh"},
        which=available("bash", "zsh"),
    )

    assert profile.kind is ShellKind.BASH


def test_selection_interface_accepts_the_documented_positional_arguments() -> None:
    profile = select_shell("bash", None, "Linux", {}, available("bash"))

    assert profile.kind is ShellKind.BASH


def test_config_shell_takes_priority_over_current_shell() -> None:
    profile = select_shell(
        None,
        "bash",
        system="Linux",
        environ={"SHELL": "/bin/zsh"},
        which=available("bash", "zsh"),
    )

    assert profile.kind is ShellKind.BASH


@pytest.mark.parametrize(
    ("system", "environ", "commands", "expected"),
    [
        (
            "Windows",
            {"PATH": r"C:\available"},
            ("powershell.exe", "pwsh.exe"),
            ShellKind.POWERSHELL_7,
        ),
        (
            "Windows",
            {"ComSpec": "powershell.exe", "PATH": r"C:\available"},
            ("powershell.exe", "pwsh.exe"),
            ShellKind.POWERSHELL_51,
        ),
        ("Linux", {}, ("bash", "zsh"), ShellKind.BASH),
        ("Linux", {"SHELL": "/usr/bin/zsh"}, ("bash", "zsh"), ShellKind.ZSH),
        (
            "Linux",
            {"SHELL": "/bin/bash", "WSL_DISTRO_NAME": "Ubuntu"},
            ("bash", "zsh", "pwsh.exe"),
            ShellKind.BASH,
        ),
    ],
)
def test_auto_selection_prefers_current_shell_then_platform_order(
    system: str,
    environ: dict[str, str],
    commands: tuple[str, ...],
    expected: object,
) -> None:
    profile = select_shell(
        None,
        None,
        system=system,
        environ=environ,
        which=available(*commands),
    )

    assert profile.kind is expected


def test_auto_selection_skips_an_unavailable_current_shell() -> None:
    profile = select_shell(
        None,
        None,
        system="Linux",
        environ={"SHELL": "/usr/bin/zsh"},
        which=available("bash"),
    )

    assert profile.kind is ShellKind.BASH


@pytest.mark.parametrize(
    "path",
    [
        r"C:\Program Files\WindowsApps\Microsoft.PowerShell_7.6.4.0_x64__8wekyb3d8bbwe",
        (
            r"C:\Program Files\WindowsApps\Microsoft.PowerShell_7.6.4.0_x64__8wekyb3d8bbwe"
            r";C:\Users\S.F. Wang\AppData\Local\Microsoft\WindowsApps"
        ),
        (
            r"C:\Users\S.F. Wang\AppData\Local\Microsoft\WindowsApps"
            r";C:\Program Files\WindowsApps\Microsoft.PowerShell_7.6.4.0_x64__8wekyb3d8bbwe"
        ),
    ],
)
def test_windowsapps_path_order_never_changes_selected_native_pwsh(path: str) -> None:
    alias = r"C:\Users\S.F. Wang\AppData\Local\Microsoft\WindowsApps\pwsh.exe"
    native = (
        r"C:\Program Files\WindowsApps\Microsoft.PowerShell_7.6.4.0_x64__8wekyb3d8bbwe"
        r"\pwsh.exe"
    )
    profile = select_shell(
        "pwsh",
        None,
        system="Windows",
        environ={
            "LOCALAPPDATA": r"C:\Users\S.F. Wang\AppData\Local",
            "PATH": path,
        },
        which=windows_locator(alias, native),
    )

    assert profile.executable == native
    assert profile.command == (native, "-NoLogo")


def test_windows_path_resolution_does_not_accept_an_implicit_cwd_hit() -> None:
    trusted_directory = r"C:\Program Files\PowerShell\7"
    native = trusted_directory + r"\pwsh.exe"
    cwd_executable = r"E:\test\student project\pwsh.exe"
    calls: list[tuple[str, str | None]] = []

    def locate(command: str, *, path: str | None = None) -> str | None:
        calls.append((command, path))
        if command == "pwsh.exe":
            return cwd_executable
        if ntpath.normcase(ntpath.normpath(command)) == ntpath.normcase(native):
            return native
        return None

    profile = select_shell(
        "pwsh",
        None,
        system="Windows",
        environ={"PATH": trusted_directory},
        which=locate,
    )

    assert profile.executable == native
    assert all(command != "pwsh.exe" for command, _path in calls)


def test_windows_root_relative_path_entry_is_not_a_fully_qualified_executable() -> None:
    lookup_calls: list[str] = []

    def locate(command: str, *, path: str | None = None) -> str | None:
        del path
        lookup_calls.append(command)
        return command

    with pytest.raises(ShellUnavailableError):
        select_shell(
            "pwsh",
            None,
            system="Windows",
            environ={"PATH": r"\Windows\System32"},
            which=locate,
        )

    assert lookup_calls == []


def test_windowsapps_only_resolves_the_native_msix_process_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    alias_root = r"C:\Users\S.F. Wang\AppData\Local\Microsoft\WindowsApps"
    alias = alias_root + r"\pwsh.exe"
    native = (
        r"C:\Program Files\WindowsApps\Microsoft.PowerShell_7.6.4.0_x64__8wekyb3d8bbwe"
        r"\pwsh.exe"
    )
    calls: list[tuple[list[str], dict[str, object]]] = []

    def resolve_alias(arguments: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        calls.append((arguments, kwargs))
        output = kwargs["stdout"]
        assert hasattr(output, "write")
        output.write(native.encode("utf-8"))  # type: ignore[union-attr]
        return subprocess.CompletedProcess(arguments, 0)

    monkeypatch.setattr(subprocess, "run", resolve_alias)
    monkeypatch.setattr("csbox.core.shell.os.path.isfile", lambda path: path == native)

    profile = select_shell(
        "pwsh",
        None,
        system="Windows",
        environ={
            "LOCALAPPDATA": r"C:\Users\S.F. Wang\AppData\Local",
            "PATH": alias_root,
            "SYSTEMROOT": r"C:\Windows",
        },
        which=windows_locator(alias),
    )

    assert profile.executable == native
    assert calls[0][0] == [
        alias,
        "-NoLogo",
        "-NoProfile",
        "-NonInteractive",
        "-Command",
        "[Console]::Out.Write([Environment]::ProcessPath)",
    ]
    assert calls[0][1]["shell"] is False
    assert calls[0][1]["timeout"] <= 2.0  # type: ignore[operator]


def test_windowsapps_only_path_never_falls_back_to_a_cwd_executable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    alias_root = r"C:\Users\S.F. Wang\AppData\Local\Microsoft\WindowsApps"
    alias = alias_root + r"\pwsh.exe"
    cwd_executable = r"E:\test\student project\pwsh.exe"
    native = (
        r"C:\Program Files\WindowsApps\Microsoft.PowerShell_7.6.4.0_x64__8wekyb3d8bbwe"
        r"\pwsh.exe"
    )
    lookup_calls: list[tuple[str, str | None]] = []

    def locate(command: str, *, path: str | None = None) -> str | None:
        lookup_calls.append((command, path))
        if ntpath.normcase(ntpath.normpath(command)) == ntpath.normcase(alias):
            return alias
        if command == "pwsh.exe":
            return cwd_executable
        return None

    def resolve_alias(arguments: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        output = kwargs["stdout"]
        assert hasattr(output, "write")
        output.write(native.encode("utf-8"))  # type: ignore[union-attr]
        return subprocess.CompletedProcess(arguments, 0)

    monkeypatch.setattr(subprocess, "run", resolve_alias)
    monkeypatch.setattr("csbox.core.shell.os.path.isfile", lambda path: path == native)

    profile = select_shell(
        "pwsh",
        None,
        system="Windows",
        environ={
            "LOCALAPPDATA": r"C:\Users\S.F. Wang\AppData\Local",
            "PATH": alias_root,
            "SYSTEMROOT": r"C:\Windows",
        },
        which=locate,
    )

    assert profile.executable == native
    assert all(ntpath.isabs(command) for command, _path in lookup_calls)


def test_windowsapps_alias_with_invalid_process_path_output_is_unlaunchable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    alias_root = r"C:\Users\S.F. Wang\AppData\Local\Microsoft\WindowsApps"
    alias = alias_root + r"\pwsh.exe"

    def emit_invalid_utf8(
        arguments: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[bytes]:
        output = kwargs["stdout"]
        assert hasattr(output, "write")
        output.write(b"\xff")  # type: ignore[union-attr]
        return subprocess.CompletedProcess(arguments, 0)

    monkeypatch.setattr(subprocess, "run", emit_invalid_utf8)

    with pytest.raises(ShellUnavailableError) as caught:
        select_shell(
            "pwsh",
            None,
            system="Windows",
            environ={
                "LOCALAPPDATA": r"C:\Users\S.F. Wang\AppData\Local",
                "PATH": alias_root,
                "SYSTEMROOT": r"C:\Windows",
            },
            which=windows_locator(alias),
        )

    assert caught.value.kind == "shell_executable_unlaunchable"
    assert caught.value.debug_context == (f"resolved_executable={alias}",)


def test_unavailable_explicit_pwsh_has_chinese_guidance_and_cause() -> None:
    with pytest.raises(ShellUnavailableError) as caught:
        select_shell(
            "pwsh",
            None,
            system="Windows",
            environ={},
            which=available("powershell.exe"),
        )

    assert "未找到 pwsh.exe" in caught.value.user_message
    assert "--shell powershell" in caught.value.user_message
    assert isinstance(caught.value.cause, FileNotFoundError)
    assert caught.value.__cause__ is caught.value.cause
    assert getattr(caught.value, "kind", None) == "shell_executable_unavailable"


def test_unknown_explicit_shell_has_chinese_error_and_cause() -> None:
    with pytest.raises(ShellUnavailableError) as caught:
        select_shell(
            "fish",
            None,
            system="Linux",
            environ={},
            which=available("bash"),
        )

    assert "不支持的 Shell" in caught.value.user_message
    assert isinstance(caught.value.cause, ValueError)


def test_no_available_shell_has_chinese_installation_guidance() -> None:
    with pytest.raises(ShellUnavailableError) as caught:
        select_shell(None, None, system="Linux", environ={}, which=available())

    assert "未找到可用的 Shell" in caught.value.user_message
    assert "bash" in caught.value.user_message
    assert isinstance(caught.value.cause, FileNotFoundError)


@pytest.mark.parametrize(
    ("profile", "expected_arguments"),
    [
        (PowerShell51Profile, ["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive"]),
        (PowerShell7Profile, ["pwsh.exe", "-NoLogo", "-NoProfile", "-NonInteractive"]),
        (BashProfile, ["bash", "--version"]),
        (ZshProfile, ["zsh", "--version"]),
    ],
)
def test_version_probe_uses_argument_arrays_and_a_short_timeout(
    monkeypatch: pytest.MonkeyPatch,
    profile: object,
    expected_arguments: list[str],
) -> None:
    calls: list[tuple[list[str], dict[str, object]]] = []
    secret = "CSBOX_SECRET_SENTINEL_task17_shell_version_env"
    monkeypatch.setenv("CSBOX_VAR_SHELL_SECRET", secret)

    def fake_run(arguments: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        calls.append((arguments, kwargs))
        output = kwargs["stdout"]
        assert hasattr(output, "write")
        output.write(b"7.4.6\n")  # type: ignore[union-attr]
        return subprocess.CompletedProcess(arguments, 0)

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert detect_shell_version(profile) == "7.4.6"  # type: ignore[arg-type]
    assert calls[0][0][: len(expected_arguments)] == expected_arguments
    assert calls[0][1]["timeout"] <= 2.0  # type: ignore[operator]
    child_env = calls[0][1]["env"]
    assert isinstance(child_env, dict)
    assert "CSBOX_VAR_SHELL_SECRET" not in child_env
    assert secret not in child_env.values()


def test_version_probe_failure_is_non_fatal(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        del args, kwargs
        raise subprocess.TimeoutExpired(["bash", "--version"], timeout=1)

    monkeypatch.setattr(subprocess, "run", fail)

    assert detect_shell_version(BashProfile) is None
