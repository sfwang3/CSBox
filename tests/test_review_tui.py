from __future__ import annotations

import json
from pathlib import Path

import pytest
from rich.console import Console

from csbox.core.display_width import display_width
from csbox.lab.models import SessionPaths
from csbox.lab.screen import TerminalCell, TerminalCursor, TerminalSnapshot
from csbox.locales import load_locale
from csbox.tui.app import ReviewApp
from csbox.tui.screens.review import (
    ReviewController,
    ReviewScreen,
    format_progress,
    snapshot_to_text,
)


def make_session(tmp_path: Path) -> SessionPaths:
    paths = SessionPaths(tmp_path / "session-real")
    paths.root.mkdir(parents=True, exist_ok=True)
    paths.cast.write_text(
        "\n".join(
            [
                json.dumps({"version": 3, "term": {"cols": 12, "rows": 3}}),
                json.dumps([1.0, "o", "hello\n项目检查"]),
                json.dumps([2.0, "r", "16x4"]),
                json.dumps([2.0, "o", "中文"]),
                json.dumps([1.0, "x", "0"]),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    paths.metadata.write_text(
        json.dumps(
            {
                "id": "session-real",
                "name": "中文回放",
                "status": "completed",
                "startedAt": "2026-08-11T00:00:00Z",
                "endedAt": "2026-08-11T00:00:10Z",
                "platform": "linux",
                "shell": "bash",
                "shellVersion": "5.2",
                "initialRows": 3,
                "initialColumns": 12,
                "cwd": str(tmp_path),
                "csboxVersion": "0.1.0",
            }
        ),
        encoding="utf-8",
    )
    return paths


def test_review_controller_supports_play_seek_backward_and_capture_edit_delete(
    tmp_path: Path,
) -> None:
    paths = make_session(tmp_path)
    controller = ReviewController.from_session(paths)

    assert controller.current_time == 0.0
    assert controller.duration == pytest.approx(6.0)
    controller.play()
    controller.advance(2.5)
    assert controller.current_time == pytest.approx(2.5)
    controller.pause()
    controller.seek(1.0)
    assert controller.current_time == pytest.approx(1.0)
    controller.seek(-5.0)
    assert controller.current_time == 0.0
    controller.seek(3.0)
    capture = controller.create_capture("计算机网络实验")
    assert capture.timestamp == pytest.approx(3.0)
    assert controller.captures[0].title == "计算机网络实验"
    controller.create_capture("第二个 Capture")
    assert controller.select_capture(-1) == 0
    assert controller.select_capture(1) == 1
    controller.edit_capture_title(capture.capture_id, "新的标题")
    assert controller.captures[0].title == "新的标题"
    assert controller.jump_to_capture(0) == pytest.approx(3.0)
    assert controller.delete_capture(capture.capture_id) is True
    assert len(controller.captures) == 1


def test_progress_uses_terminal_cells_and_clamps_values() -> None:
    progress = format_progress(5.0, 10.0, width=20)
    assert len(progress) == 20
    assert progress.count("=") == 9
    assert "项目" in format_progress(0.0, 0.0, width=20, label="项目")
    assert format_progress(20.0, 10.0, width=8).startswith("[")


def test_snapshot_text_preserves_cjk_cells_and_terminal_attributes() -> None:
    row = (
        TerminalCell("a"),
        TerminalCell("b", bold=True, underline=True),
        TerminalCell("中", width=2, foreground="red", background="blue", reverse=True),
        TerminalCell("", width=0),
        TerminalCell("e\u0301"),
        TerminalCell(" "),
    )
    snapshot = TerminalSnapshot(
        rows=1,
        columns=6,
        cells=(row,),
        cursor=TerminalCursor(),
    )

    rendered = snapshot_to_text(snapshot)

    assert display_width(str(rendered)) == snapshot.columns
    assert str(rendered) == "ab中e\u0301 "
    console = Console()
    assert rendered.get_style_at_offset(console, 1).bold is True
    assert rendered.get_style_at_offset(console, 1).underline is True
    reverse_style = rendered.get_style_at_offset(console, 2)
    assert reverse_style.color.name == "blue"
    assert reverse_style.bgcolor.name == "red"


@pytest.mark.asyncio
async def test_review_app_q_exits_instead_of_revealing_blank_base_screen(
    tmp_path: Path,
) -> None:
    app = ReviewApp(
        controller=ReviewController.from_session(make_session(tmp_path)), locale=load_locale()
    )

    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        await pilot.press("q")
        await pilot.pause()

        assert app.is_running is False


@pytest.mark.asyncio
async def test_review_screen_is_narrow_at_80_and_wide_at_120(tmp_path: Path) -> None:
    controller = ReviewController.from_session(make_session(tmp_path))
    app = ReviewApp(controller=controller, locale=load_locale())

    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, ReviewScreen)
        assert screen.is_wide is False
        await pilot.press("tab")
        assert screen.active_pane == "timeline"
        await pilot.press("tab")
        assert screen.active_pane == "captures"

    wide_app = ReviewApp(
        controller=ReviewController.from_session(make_session(tmp_path)),
        locale=load_locale(),
    )
    async with wide_app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        screen = wide_app.screen
        assert isinstance(screen, ReviewScreen)
        assert screen.is_wide is True
        assert screen.query_one("#review-terminal")
        assert screen.query_one("#review-sidebar")
