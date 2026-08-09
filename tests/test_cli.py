from typer.testing import CliRunner

from csbox.cli.main import app

runner = CliRunner()


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
