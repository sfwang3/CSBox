from __future__ import annotations

import json
from pathlib import Path

from csbox.check.detectors import FileInventory, detect_projects


def test_detect_projects_finds_all_supported_project_signals(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text(json.dumps({"scripts": {"build": "vite build"}}))
    (tmp_path / "pom.xml").write_text("<project />")
    (tmp_path / "build.gradle.kts").write_text("plugins {}")
    (tmp_path / "pyproject.toml").write_text("[project]\nname='demo'\n")
    (tmp_path / "requirements.txt").write_text("pytest\n")

    projects = detect_projects(tmp_path)

    assert [project.kind for project in projects] == ["node", "maven", "gradle", "python"]
    assert all(project.root == tmp_path for project in projects)


def test_inventory_prunes_artifacts_but_keeps_directory_signals(tmp_path: Path) -> None:
    (tmp_path / "node_modules" / "nested").mkdir(parents=True)
    (tmp_path / "node_modules" / "nested" / "secret.js").write_text("ignored")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("print('ok')")

    inventory = FileInventory.build(tmp_path)

    assert inventory.has_directory("node_modules")
    assert all(
        entry.relative.as_posix() != "node_modules/nested/secret.js" for entry in inventory.files
    )
    assert any(entry.relative.as_posix() == "src/main.py" for entry in inventory.files)
