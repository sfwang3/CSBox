from __future__ import annotations

import importlib.resources
from pathlib import Path

from csbox.locales import load_locale


def test_runtime_resources_are_loaded_via_package_resources() -> None:
    locale = load_locale("zh_CN")
    theme = importlib.resources.files("csbox.tui.themes").joinpath("csbox.tcss")

    assert locale("brand.subtitle") == "计算机实验与项目交付工具"
    assert theme.is_file()
    assert "Screen" in theme.read_text(encoding="utf-8")


def test_runtime_resource_paths_are_not_repository_relative() -> None:
    package_root = Path(__file__).resolve().parents[1] / "src" / "csbox"
    locale_path = importlib.resources.files("csbox.locales").joinpath("zh_CN.json")
    theme_path = importlib.resources.files("csbox.tui.themes").joinpath("csbox.tcss")

    assert Path(str(locale_path)).is_relative_to(package_root)
    assert Path(str(theme_path)).is_relative_to(package_root)
