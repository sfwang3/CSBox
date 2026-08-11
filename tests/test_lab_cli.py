from __future__ import annotations

import importlib
import json
from pathlib import Path

from typer.testing import CliRunner

from csbox.cli.main import app
from csbox.lab.models import SessionPaths
from csbox.lab.repository import SessionSummary
from csbox.lab.service import LabRunResult


class FakeLabService:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.started: list[tuple[object, ...]] = []
        self.exported: list[tuple[object, ...]] = []

    def list(self) -> tuple[SessionSummary, ...]:
        return ()

    def start(self, *args, **kwargs) -> LabRunResult:
        self.started.append((args, kwargs))
        return LabRunResult(
            session=SessionPaths(self.root / "session"),
            status="completed",
            exit_code=0,
            advisory="提示：F12 可用于 Capture。",
        )

    def export(self, session: str, destination: Path | None, *, theme: str, force: bool):
        self.exported.append((session, destination, theme, force))
        return type("Export", (), {"destination": destination or self.root / "export"})()


def test_lab_list_json_is_stable_and_has_no_ansi(monkeypatch, tmp_path: Path) -> None:
    service = FakeLabService(tmp_path)
    cli_module = importlib.import_module("csbox.cli.main")
    monkeypatch.setattr(cli_module, "create_lab_service", lambda: service)

    result = CliRunner().invoke(app, ["lab", "list", "--json"])

    assert result.exit_code == 0
    assert json.loads(result.stdout) == []
    assert "\x1b[" not in result.stdout


def test_lab_start_and_export_are_service_commands(monkeypatch, tmp_path: Path) -> None:
    service = FakeLabService(tmp_path)
    cli_module = importlib.import_module("csbox.cli.main")
    monkeypatch.setattr(cli_module, "create_lab_service", lambda: service)

    started = CliRunner().invoke(app, ["lab", "start", "实验", "--shell", "bash"])
    exported = CliRunner().invoke(
        app,
        [
            "lab",
            "export",
            "session-1",
            "--output",
            str(tmp_path / "导出"),
            "--theme",
            "light",
            "--force",
        ],
    )

    assert started.exit_code == 0
    assert "completed" in started.stdout or "完成" in started.stdout
    assert service.started[0][1]["shell"] == "bash"
    assert exported.exit_code == 0
    assert service.exported == [("session-1", tmp_path / "导出", "light", True)]
