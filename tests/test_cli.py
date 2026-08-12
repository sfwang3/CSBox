import io
import sys
from importlib import import_module

import pytest
from typer.testing import CliRunner

from csbox.cli.main import app

runner = CliRunner()
cli_module = import_module("csbox.cli.main")


def test_cli_help_lists_doctor() -> None:
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    assert "doctor" in result.stdout


def test_doctor_prints_the_requested_chinese_environment_checks() -> None:
    result = runner.invoke(app, ["doctor"])

    assert result.exit_code == 0
    for label in (
        "操作系统",
        "Python 版本",
        "当前 Shell",
        "检测到 powershell.exe",
        "检测到 pwsh.exe",
        "处于 WSL",
        "终端尺寸",
    ):
        assert label in result.stdout


def test_cli_main_reconfigures_non_utf8_stdout_before_chinese_help(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = io.BytesIO()
    stream = io.TextIOWrapper(output, encoding="cp1252")
    monkeypatch.setattr(sys, "stdout", stream)
    monkeypatch.setattr(sys, "argv", ["csbox", "--help"])

    with pytest.raises(SystemExit) as exit_info:
        cli_module.main()

    stream.flush()
    assert exit_info.value.code == 0
    assert stream.encoding.lower().replace("-", "") == "utf8"
    assert "doctor" in output.getvalue().decode("utf-8")
