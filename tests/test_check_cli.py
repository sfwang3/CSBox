from __future__ import annotations

import importlib
import json
import math
import stat
from pathlib import Path

import pytest
from typer.testing import CliRunner

from csbox.cli.main import app


def test_check_plain_and_json_are_machine_stable(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("# demo\n")

    plain = CliRunner().invoke(app, ["check", str(tmp_path), "--plain"])
    structured = CliRunner().invoke(app, ["check", str(tmp_path), "--json"])

    assert plain.exit_code == 0
    assert "README" in plain.stdout
    assert structured.exit_code == 0
    payload = json.loads(structured.stdout)
    assert payload["schema_version"] == 1
    assert payload["root"] == "."
    assert payload["text_scan_stats"]["read_count"] >= 0
    assert payload["deep_scan"] is None


def test_check_build_flag_is_forwarded_to_service(monkeypatch, tmp_path: Path) -> None:
    cli_module = importlib.import_module("csbox.cli.main")

    calls: list[tuple[bool, bool]] = []

    class FakeService:
        def run(self, root: Path, *, build: bool = False, deep: bool = False):
            calls.append((build, deep))
            return type(
                "Report",
                (),
                {
                    "exit_code": 0,
                    "status": type("Status", (), {"value": "PASS"})(),
                    "model_dump": lambda self, mode="json": {
                        "root": str(root),
                        "findings": [],
                    },
                },
            )()

    monkeypatch.setattr(cli_module, "create_check_service", lambda root=None: FakeService())

    result = CliRunner().invoke(app, ["check", str(tmp_path), "--json", "--build"])

    assert result.exit_code == 0
    assert calls == [(True, False)]


def test_check_deep_forwards_flag_and_reports_missing_tool_as_skip(
    monkeypatch, tmp_path: Path
) -> None:
    cli_module = importlib.import_module("csbox.cli.main")
    monkeypatch.setattr("csbox.check.rules.shutil.which", lambda name: None)
    (tmp_path / "README.md").write_text("# 项目\n", encoding="utf-8")

    plain = CliRunner().invoke(app, ["check", str(tmp_path), "--plain", "--deep"])
    structured = CliRunner().invoke(app, ["check", str(tmp_path), "--json", "--deep"])

    assert plain.exit_code == 0
    assert "Deep secret scan" in plain.stdout
    assert "Deep secret scan    SKIP" in plain.stdout
    assert structured.exit_code == 0
    payload = json.loads(structured.stdout)
    assert payload["deep_scan"]["status"] == "SKIP"
    assert payload["deep_scan"]["category"] == "deep-secret-scan"
    assert str(tmp_path) not in plain.stdout
    assert str(tmp_path) not in structured.stdout
    del cli_module


def test_check_deep_never_emits_gitleaks_secret_sentinel(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    sentinel = "CSBOX_SECRET_SENTINEL_gitleaks_123"
    fake_tool = tmp_path / "gitleaks"
    fake_tool.write_text(
        "#!/bin/sh\n"
        "printf '%s\\n' '" + sentinel + "'\n"
        "printf '%s\\n' '" + sentinel + "' >&2\n"
        "exit 1\n",
        encoding="utf-8",
    )
    fake_tool.chmod(fake_tool.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    monkeypatch.setattr("csbox.check.rules.shutil.which", lambda name: str(fake_tool))
    (tmp_path / "README.md").write_text("# 项目\n", encoding="utf-8")
    (tmp_path / "config.txt").write_text(sentinel, encoding="utf-8")

    plain = CliRunner().invoke(app, ["check", str(tmp_path), "--plain", "--deep"])
    structured = CliRunner().invoke(app, ["check", str(tmp_path), "--json", "--deep"])

    assert plain.exit_code == 1
    assert structured.exit_code == 1
    assert sentinel not in plain.stdout
    assert sentinel not in structured.stdout
    assert sentinel not in structured.exception.args if structured.exception else True


def test_check_json_serializer_rejects_non_finite_values() -> None:
    cli_module = importlib.import_module("csbox.cli.main")
    serializer = getattr(cli_module, "_strict_json", None)
    assert callable(serializer), "strict JSON serializer is not implemented"

    with pytest.raises(ValueError):
        serializer({"value": math.nan})


def test_cli_path_sanitizer_rejects_windows_and_parent_paths() -> None:
    cli_module = importlib.import_module("csbox.cli.main")

    assert cli_module._safe_relative_location(Path("/project"), Path("C:\\Users\\student")) is None
    assert cli_module._safe_relative_location(Path("/project"), Path("../outside.txt")) is None
