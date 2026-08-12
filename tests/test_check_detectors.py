from __future__ import annotations

import inspect
import json
from pathlib import Path

import pytest

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


def test_detect_projects_uses_inventory_for_nested_markers_and_deduplicates(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text(
        json.dumps({"packageManager": "pnpm@9.0.0", "scripts": {"build": "vite build"}}),
        encoding="utf-8",
    )
    (tmp_path / "pnpm-lock.yaml").write_text("lockfileVersion: '9.0'\n", encoding="utf-8")
    (tmp_path / "pom.xml").write_text("<project />", encoding="utf-8")
    (tmp_path / "build.gradle").write_text("plugins {}", encoding="utf-8")
    (tmp_path / "settings.gradle.kts").write_text("rootProject.name = 'demo'", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname = 'demo'\n[tool.poetry]\nname = 'demo'\n",
        encoding="utf-8",
    )
    (tmp_path / "poetry.lock").write_text("content-hash = 'x'\n", encoding="utf-8")
    (tmp_path / "requirements.txt").write_text("pytest\n", encoding="utf-8")

    (tmp_path / "java" / "module-a").mkdir(parents=True)
    (tmp_path / "java" / "module-a" / "pom.xml").write_text("<project />", encoding="utf-8")
    (tmp_path / "java" / "module-b").mkdir(parents=True)
    (tmp_path / "java" / "module-b" / "build.gradle.kts").write_text("plugins {}", encoding="utf-8")
    (tmp_path / "looks-like-module").mkdir()

    inventory = FileInventory.build(tmp_path)
    assert "inventory" in inspect.signature(detect_projects).parameters
    projects = detect_projects(tmp_path, inventory=inventory)

    actual = [(project.kind, project.root.relative_to(tmp_path).as_posix()) for project in projects]
    assert actual == [
        ("node", "."),
        ("maven", "."),
        ("gradle", "."),
        ("python", "."),
        ("maven", "java/module-a"),
        ("gradle", "java/module-b"),
    ]
    assert projects[0].package_manager == "pnpm"
    assert projects[3].package_manager == "poetry"
    assert (
        len(
            [
                project
                for project in projects
                if project.kind == "gradle" and project.root == tmp_path
            ]
        )
        == 1
    )


@pytest.mark.parametrize(
    ("marker", "manager"),
    [
        ("package-lock.json", "npm"),
        ("yarn.lock", "yarn"),
        ("pnpm-lock.yaml", "pnpm"),
    ],
)
def test_node_manager_requires_real_lockfile_or_manifest(
    tmp_path: Path, marker: str, manager: str
) -> None:
    (tmp_path / "package.json").write_text("{}", encoding="utf-8")
    (tmp_path / marker).write_text("lock\n", encoding="utf-8")

    project = detect_projects(tmp_path)[0]

    assert project.kind == "node"
    assert hasattr(project, "package_manager")
    assert project.package_manager == manager


def test_python_uv_and_poetry_detection_uses_real_files(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname = 'demo'\n[tool.uv]\ndev-dependencies = []\n",
        encoding="utf-8",
    )
    (tmp_path / "uv.lock").write_text("version = 1\n", encoding="utf-8")

    project = detect_projects(tmp_path)[0]
    assert hasattr(project, "package_manager")
    assert project.package_manager == "uv"


def test_nested_symlink_and_unmarked_directory_are_not_projects(tmp_path: Path) -> None:
    real = tmp_path / "real-module"
    real.mkdir()
    (real / "pom.xml").write_text("<project />", encoding="utf-8")
    (tmp_path / "link-module").symlink_to(real, target_is_directory=True)
    (tmp_path / "module-name-only").mkdir()

    projects = detect_projects(tmp_path)

    assert [(project.kind, project.root.name) for project in projects] == [("maven", "real-module")]


def test_detect_projects_does_not_rebuild_supplied_inventory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "pom.xml").write_text("<project />", encoding="utf-8")
    inventory = FileInventory.build(tmp_path)
    assert "inventory" in inspect.signature(detect_projects).parameters

    def fail_build(*args, **kwargs):
        raise AssertionError("supplied inventory must be reused")

    monkeypatch.setattr("csbox.check.detectors.FileInventory.build", fail_build)

    projects = detect_projects(tmp_path, inventory=inventory)

    assert [project.kind for project in projects] == ["maven"]
