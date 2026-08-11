from __future__ import annotations

import importlib
import json
from pathlib import Path

from typer.testing import CliRunner

from csbox.cli.main import app
from csbox.pack.models import PackReport


def test_pack_json_routes_to_service(monkeypatch, tmp_path: Path) -> None:
    cli_module = importlib.import_module("csbox.cli.main")
    calls: list[tuple[Path, bool, bool]] = []

    class FakeService:
        def pack(self, root: Path, *, destination=None, verify=False, force=False, **kwargs):
            del kwargs
            calls.append((root, verify, force))
            return PackReport(
                source_root=root,
                destination=destination or tmp_path / "out.zip",
                entries=("README.md",),
                excluded=(),
                source_bytes=1,
                archive_bytes=2,
                verified=verify,
            )

    monkeypatch.setattr(cli_module, "create_pack_service", lambda root=None: FakeService())

    result = CliRunner().invoke(
        app,
        [
            "pack",
            str(tmp_path),
            "--output",
            str(tmp_path / "out.zip"),
            "--verify",
            "--force",
            "--json",
        ],
    )

    assert result.exit_code == 0
    assert json.loads(result.stdout)["verified"] is True
    assert calls == [(tmp_path, True, True)]
