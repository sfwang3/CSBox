from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

from csbox.locales import load_locale
from csbox.tui.app import CSBoxApp

_TOOL_PATH = (
    Path(__file__).resolve().parents[1]
    / "docs"
    / "tests"
    / "tooling"
    / "generate_readme_screenshots.py"
)
_SPEC = importlib.util.spec_from_file_location("csbox_readme_screenshots", _TOOL_PATH)
assert _SPEC is not None and _SPEC.loader is not None
_TOOL = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _TOOL
_SPEC.loader.exec_module(_TOOL)


def test_readme_screenshot_color_environment_overrides_no_color(monkeypatch) -> None:
    monkeypatch.setenv("NO_COLOR", "1")

    with _TOOL.screenshot_color_environment():
        app = CSBoxApp(
            data_source=_TOOL.DemoHomeDataSource(),
            environment=_TOOL.environment(),
            locale=load_locale(),
        )
        assert app.no_color is False

    assert os.environ["NO_COLOR"] == "1"
