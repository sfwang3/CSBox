from __future__ import annotations

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


def available(*commands: str) -> Callable[[str], str | None]:
    paths = {command: f"/available/{command}" for command in commands}
    return paths.get


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
        environ={},
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

    assert profile is BashProfile


def test_selection_interface_accepts_the_documented_positional_arguments() -> None:
    profile = select_shell("bash", None, "Linux", {}, available("bash"))

    assert profile is BashProfile


def test_config_shell_takes_priority_over_current_shell() -> None:
    profile = select_shell(
        None,
        "bash",
        system="Linux",
        environ={"SHELL": "/bin/zsh"},
        which=available("bash", "zsh"),
    )

    assert profile is BashProfile


@pytest.mark.parametrize(
    ("system", "environ", "commands", "expected"),
    [
        ("Windows", {}, ("powershell.exe", "pwsh.exe"), PowerShell7Profile),
        (
            "Windows",
            {"ComSpec": "powershell.exe"},
            ("powershell.exe", "pwsh.exe"),
            PowerShell51Profile,
        ),
        ("Linux", {}, ("bash", "zsh"), BashProfile),
        ("Linux", {"SHELL": "/usr/bin/zsh"}, ("bash", "zsh"), ZshProfile),
        (
            "Linux",
            {"SHELL": "/bin/bash", "WSL_DISTRO_NAME": "Ubuntu"},
            ("bash", "zsh", "pwsh.exe"),
            BashProfile,
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

    assert profile is expected


def test_auto_selection_skips_an_unavailable_current_shell() -> None:
    profile = select_shell(
        None,
        None,
        system="Linux",
        environ={"SHELL": "/usr/bin/zsh"},
        which=available("bash"),
    )

    assert profile is BashProfile


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
