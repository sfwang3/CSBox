from __future__ import annotations

import importlib
from pathlib import Path

from typer.testing import CliRunner

from csbox.cli.main import app

SECRET = "CSBOX_SECRET_SENTINEL_error_ux"


def test_check_error_is_actionable_without_raw_exception(monkeypatch, tmp_path: Path) -> None:
    cli_module = importlib.import_module("csbox.cli.main")

    class FailingCheck:
        def run(self, *_args: object, **_kwargs: object) -> None:
            raise RuntimeError(SECRET)

    monkeypatch.setattr(cli_module, "create_check_service", lambda _root: FailingCheck())

    result = CliRunner().invoke(app, ["check", str(tmp_path), "--verbose"])

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

    result = CliRunner().invoke(app, ["pack", str(tmp_path), "--verbose"])

    assert result.exit_code == 1
    assert "发生了什么" in result.stdout
    assert "在哪里" in result.stdout
    assert "怎么处理" in result.stdout
    assert SECRET not in result.stdout
    assert SECRET not in result.stderr


def test_lab_start_error_is_actionable_without_raw_exception(monkeypatch) -> None:
    cli_module = importlib.import_module("csbox.cli.main")

    class FailingLab:
        def start(self, *_args: object, **_kwargs: object) -> None:
            raise RuntimeError(SECRET)

    monkeypatch.setattr(cli_module, "create_lab_service", lambda: FailingLab())

    result = CliRunner().invoke(app, ["lab", "start", "中文实验", "--verbose"])

    assert result.exit_code == 1
    assert "发生了什么" in result.stdout
    assert "在哪里" in result.stdout
    assert "怎么处理" in result.stdout
    assert SECRET not in result.stdout
    assert SECRET not in result.stderr
