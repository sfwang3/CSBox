from __future__ import annotations

import importlib
import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

from typer.testing import CliRunner

from csbox.cli.main import app
from csbox.core.display_width import display_width
from csbox.lab.models import SessionMetadata, SessionPaths
from csbox.lab.repository import SessionSummary
from csbox.lab.service import LabRunResult


class FakeLabService:
    def __init__(self, root: Path, summaries: tuple[SessionSummary, ...] = ()) -> None:
        self.root = root
        self.started: list[tuple[object, ...]] = []
        self.exported: list[tuple[object, ...]] = []
        self.summaries = summaries

    def list(self) -> tuple[SessionSummary, ...]:
        return self.summaries

    def capture_advisory(self) -> SimpleNamespace:
        return SimpleNamespace(message="启动前提示：Capture")

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
    payload = json.loads(result.stdout)
    assert payload["schema_version"] == 1
    assert payload["sessions"] == []
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


def test_lab_list_json_does_not_wrap_long_fields(monkeypatch, tmp_path: Path) -> None:
    metadata = SessionMetadata(
        id="long-session",
        name="实验" * 120,
        status="completed",
        startedAt=datetime.now(UTC),
        endedAt=datetime.now(UTC),
        platform="linux",
        shell="bash",
        shellVersion=None,
        initialRows=24,
        initialColumns=80,
        cwd=Path("/tmp") / ("课程" * 120),
        csboxVersion="0.1.0",
    )
    service = FakeLabService(
        tmp_path,
        summaries=(
            SessionSummary(
                paths=SessionPaths(tmp_path / "long-session"),
                metadata=metadata,
                capture_count=2,
            ),
        ),
    )
    cli_module = importlib.import_module("csbox.cli.main")
    monkeypatch.setattr(cli_module, "create_lab_service", lambda: service)

    result = CliRunner().invoke(app, ["lab", "list", "--json"])

    assert result.exit_code == 0
    assert json.loads(result.stdout)["sessions"][0]["captures"] == 2


def test_lab_list_plain_truncates_cjk_name_and_path_by_display_cells(
    monkeypatch, tmp_path: Path
) -> None:
    long_name = "实验" * 120
    long_cwd = Path("C:/Users/测试用户/桌面/实验一") / ("课程实验" * 40)
    metadata = SessionMetadata(
        id="long-session",
        name=long_name,
        status="completed",
        startedAt=datetime.now(UTC),
        endedAt=datetime.now(UTC),
        platform="windows",
        shell="powershell_51",
        shellVersion=None,
        initialRows=24,
        initialColumns=80,
        cwd=long_cwd,
        csboxVersion="0.1.0",
    )
    service = FakeLabService(
        tmp_path,
        summaries=(
            SessionSummary(
                paths=SessionPaths(tmp_path / "long-session"),
                metadata=metadata,
                capture_count=2,
            ),
        ),
    )
    cli_module = importlib.import_module("csbox.cli.main")
    monkeypatch.setattr(cli_module, "create_lab_service", lambda: service)

    result = CliRunner().invoke(app, ["lab", "list"])

    assert result.exit_code == 0
    assert long_name not in result.stdout
    assert str(long_cwd) not in result.stdout
    assert "…" in result.stdout
    assert all(display_width(line) <= 80 for line in result.stdout.splitlines())
