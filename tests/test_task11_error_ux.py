from __future__ import annotations

import importlib
from pathlib import Path

import pytest
from typer.testing import CliRunner

from csbox.cli.main import app

SECRET = "CSBOX_SECRET_SENTINEL_error_ux"


def test_check_error_is_actionable_without_raw_exception(monkeypatch, tmp_path: Path) -> None:
    cli_module = importlib.import_module("csbox.cli.main")

    class FailingCheck:
        def run(self, *_args: object, **_kwargs: object) -> None:
            raise RuntimeError(SECRET)

    monkeypatch.setattr(cli_module, "create_check_service", lambda _root: FailingCheck())

    result = CliRunner().invoke(app, ["check", str(tmp_path)])

    assert result.exit_code == 1
    assert "发生了什么" in result.stdout
    assert "在哪里" in result.stdout
    assert "怎么处理" in result.stdout
    assert SECRET not in result.stdout
    assert SECRET not in result.stderr


def test_pack_error_is_actionable_without_raw_exception(monkeypatch, tmp_path: Path) -> None:
    cli_module = importlib.import_module("csbox.cli.main")

    class FailingPack:
        def pack(self, *_args: object, **_kwargs: object) -> None:
            raise RuntimeError(SECRET)

    monkeypatch.setattr(cli_module, "create_pack_service", lambda _root: FailingPack())

    result = CliRunner().invoke(app, ["pack", str(tmp_path)])

    assert result.exit_code == 1
    assert "发生了什么" in result.stdout
    assert "在哪里" in result.stdout
    assert "怎么处理" in result.stdout
    assert SECRET not in result.stdout
    assert SECRET not in result.stderr


def test_verbose_pack_error_does_not_expose_exception_values(monkeypatch, tmp_path: Path) -> None:
    cli_module = importlib.import_module("csbox.cli.main")

    class FailingPack:
        def pack(self, *_args: object, **_kwargs: object) -> None:
            raise RuntimeError(SECRET)

    monkeypatch.setattr(cli_module, "create_pack_service", lambda _root: FailingPack())

    result = CliRunner().invoke(app, ["pack", str(tmp_path), "--verbose"])

    assert result.exit_code == 1
    assert "RuntimeError" in result.stderr
    assert SECRET not in result.stdout
    assert SECRET not in result.stderr


def test_verbose_pack_error_redacts_prefixed_secret_assignments(
    monkeypatch, tmp_path: Path
) -> None:
    secrets = (
        "CSBOX_SECRET_SENTINEL_openai_key",
        "CSBOX_SECRET_SENTINEL_aws_access_key",
    )
    cli_module = importlib.import_module("csbox.cli.main")

    class FailingPack:
        def pack(self, *_args: object, **_kwargs: object) -> None:
            cause = OSError(
                5,
                f"OPENAI_API_KEY={secrets[0]} AWS_SECRET_ACCESS_KEY={secrets[1]}",
            )
            raise RuntimeError("pack startup failed") from cause

    monkeypatch.setattr(cli_module, "create_pack_service", lambda _root: FailingPack())

    result = CliRunner().invoke(app, ["pack", str(tmp_path), "--verbose"])

    assert result.exit_code == 1
    assert "OPENAI_API_KEY=<redacted>" in result.stderr
    assert "AWS_SECRET_ACCESS_KEY=<redacted>" in result.stderr
    for secret in secrets:
        assert secret not in result.stdout
        assert secret not in result.stderr


def test_lab_start_error_is_actionable_without_raw_exception(monkeypatch) -> None:
    cli_module = importlib.import_module("csbox.cli.main")

    class FailingLab:
        def start(self, *_args: object, **_kwargs: object) -> None:
            raise RuntimeError(SECRET)

    monkeypatch.setattr(cli_module, "create_lab_service", lambda: FailingLab())

    result = CliRunner().invoke(app, ["lab", "start", "中文实验"])

    assert result.exit_code == 1
    assert "发生了什么" in result.stdout
    assert "在哪里" in result.stdout
    assert "怎么处理" in result.stdout
    assert SECRET not in result.stdout
    assert SECRET not in result.stderr


def test_verbose_lab_error_preserves_type_chain_and_native_code(monkeypatch) -> None:
    cli_module = importlib.import_module("csbox.cli.main")

    class FailingLab:
        def start(self, *_args: object, **_kwargs: object) -> None:
            cause = OSError(2, "missing shell")
            raise RuntimeError("host startup failed") from cause

    monkeypatch.setattr(cli_module, "create_lab_service", lambda: FailingLab())

    result = CliRunner().invoke(app, ["lab", "start", "中文实验", "--verbose"])

    assert result.exit_code == 1
    assert "RuntimeError" in result.stderr
    assert "FileNotFoundError" in result.stderr
    assert "native_code=2" in result.stderr


def test_lab_start_reports_unlaunchable_shell_as_a_distinct_actionable_failure(
    monkeypatch,
) -> None:
    cli_module = importlib.import_module("csbox.cli.main")

    class FailingLab:
        def start(self, *_args: object, **_kwargs: object) -> None:
            from csbox.lab.windows_host import LabLaunchError

            raise LabLaunchError(
                "shell_executable_unlaunchable",
                "PowerShell executable could not be launched",
            )

    monkeypatch.setattr(cli_module, "create_lab_service", lambda: FailingLab())

    result = CliRunner().invoke(app, ["lab", "start", "中文实验"])

    assert result.exit_code == 1
    assert "Shell 可执行文件无法启动" in result.stdout
    assert "重新安装 PowerShell" in result.stdout


@pytest.mark.parametrize(
    ("kind", "expected_problem", "expected_action"),
    [
        ("windows_terminal_unavailable", "未找到 Windows Terminal", "安装或修复 Windows Terminal"),
        ("windows_terminal_launch_failure", "Windows Terminal 无法启动", "运行 wt.exe"),
        ("shell_executable_unavailable", "未找到 Shell 可执行文件", "安装 PowerShell"),
        ("conpty_initialization_failure", "Windows ConPTY 无法初始化", "确认 Windows 版本"),
        ("runtime_backend_failure", "终端后端运行失败", "重新启动实验"),
    ],
)
def test_lab_start_reports_distinct_windows_launch_failure_categories(
    monkeypatch,
    kind: str,
    expected_problem: str,
    expected_action: str,
) -> None:
    cli_module = importlib.import_module("csbox.cli.main")

    class FailingLab:
        def start(self, *_args: object, **_kwargs: object) -> None:
            from csbox.lab.windows_host import LabLaunchError

            raise LabLaunchError(kind, "dedicated launch failed")

    monkeypatch.setattr(cli_module, "create_lab_service", lambda: FailingLab())

    result = CliRunner().invoke(app, ["lab", "start", "中文实验"])

    assert result.exit_code == 1
    normalized_output = " ".join(result.stdout.split())
    assert expected_problem in normalized_output
    assert expected_action in normalized_output
