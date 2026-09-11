from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest
from textual.color import Color

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


@pytest.mark.asyncio
async def test_home_readme_viewport_has_no_entry_overflow_or_scrollbar_artifacts() -> None:
    app = CSBoxApp(
        data_source=_TOOL.DemoHomeDataSource(),
        environment=_TOOL.environment(),
        locale=load_locale(),
    )

    async with app.run_test(size=_TOOL.HOME_SIZE) as pilot:
        await pilot.pause()

        action_panel = app.screen.query_one("#action-panel")
        assert len(app.screen.query(".entry-card")) == 7
        assert action_panel.max_scroll_y == 0
        assert action_panel.show_vertical_scrollbar is False
        assert app.screen.query_one("#entry-start").styles.border.top[0] == "round"

        layout = app.screen._compositor.render_update(
            full=True,
            screen_stack=app._background_screens,
            simplify=False,
        )
        action_rows = (
            layout.strips[y].crop(action_panel.region.x, action_panel.region.right).text
            for y in range(action_panel.region.y, action_panel.region.bottom)
        )
        rendered_action_panel = "\n".join(action_rows)
        assert not any(marker in rendered_action_panel for marker in ("▔", "▁", "▆"))


@pytest.mark.asyncio
async def test_help_readme_viewport_uses_a_non_black_scrollbar_track() -> None:
    app = CSBoxApp(
        data_source=_TOOL.DemoHomeDataSource(),
        environment=_TOOL.environment(),
        locale=load_locale(),
    )

    async with app.run_test(size=_TOOL.HOME_SIZE) as pilot:
        await pilot.pause()
        await pilot.press("?")
        await pilot.pause()

        scroll = app.screen.query_one("#help-scroll")
        assert scroll.styles.scrollbar_background == Color(16, 28, 41)
        assert scroll.styles.scrollbar_color == Color(50, 111, 162)


@pytest.mark.asyncio
async def test_review_readme_viewport_gives_the_terminal_pane_full_height(
    tmp_path: Path,
) -> None:
    app = _TOOL.ReviewApp(
        controller=_TOOL.ReviewController.from_session(_TOOL.make_review_session(tmp_path)),
        locale=load_locale(),
    )

    async with app.run_test(size=_TOOL.REVIEW_SIZE) as pilot:
        await pilot.pause()
        body = app.screen.query_one("#review-body")
        terminal = app.screen.query_one("#review-terminal")
        assert terminal.region.height == body.region.height


@pytest.mark.asyncio
async def test_readme_home_svg_export_is_deterministic() -> None:
    app = CSBoxApp(
        data_source=_TOOL.DemoHomeDataSource(),
        environment=_TOOL.environment(),
        locale=load_locale(),
    )

    async with app.run_test(size=_TOOL.HOME_SIZE) as pilot:
        await pilot.pause()
        first = _TOOL.prepare_svg_for_png(app.export_screenshot())
        second = _TOOL.prepare_svg_for_png(app.export_screenshot())
        assert first == second


def test_readme_png_font_preparation_keeps_terminal_borders_bounded() -> None:
    svg = (
        "<style>font-family: Fira Code, monospace;</style>"
        '<text class="border">╭────────────────╮</text>'
        '<text class="content">CSBox 帮助</text>'
        '<text class="scrollbar">▆▆</text>'
    )

    prepared = _TOOL.prepare_svg_for_png(svg)

    assert 'font-family: "Noto Sans Mono CJK SC", "Noto Sans CJK SC", sans-serif;' in prepared
    assert (
        '<text class="border" font-family="Fira Code, monospace">╭────────────────╮</text>'
    ) in prepared
    assert '<text class="scrollbar" font-family="Fira Code, monospace">▆▆</text>' in prepared
    assert '<text class="content">CSBox 帮助</text>' in prepared
