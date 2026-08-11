from __future__ import annotations

import importlib
import json
from pathlib import Path

from typer.testing import CliRunner

from csbox.cli.main import app


def test_check_plain_and_json_are_machine_stable(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("# demo\n")

    plain = CliRunner().invoke(app, ["check", str(tmp_path), "--plain"])
    structured = CliRunner().invoke(app, ["check", str(tmp_path), "--json"])

    assert plain.exit_code == 0
    assert "README" in plain.stdout
    assert structured.exit_code == 0
    assert json.loads(structured.stdout)["root"] == str(tmp_path)


def test_check_build_flag_is_forwarded_to_service(monkeypatch, tmp_path: Path) -> None:
    cli_module = importlib.import_module("csbox.cli.main")

    calls: list[bool] = []

    class FakeService:
        def run(self, root: Path, *, build: bool = False):
            calls.append(build)
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
    assert calls == [True]
