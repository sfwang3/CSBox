from importlib.metadata import version

from typer.testing import CliRunner

from csbox import __version__
from csbox.cli.main import app


def test_version_is_single_runtime_source() -> None:
    assert __version__ == version("csbox")
    assert __version__ == "0.6.0rc1"


def test_cli_version_prints_runtime_version() -> None:
    result = CliRunner().invoke(app, ["--version"])

    assert result.exit_code == 0
    assert result.stdout.strip() == "0.6.0rc1"
