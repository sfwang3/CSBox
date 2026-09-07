"""Regenerate README screenshots from the real Textual applications.

The fixtures are synthetic and written below /tmp only. Textual provides the
render; CairoSVG turns that render into a CJK-readable PNG for GitHub.
"""

from __future__ import annotations

import asyncio
import html
import json
import os
import re
import tempfile
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from csbox.core.events import TerminalEvent, TerminalEventType
from csbox.core.models import EnvironmentSnapshot
from csbox.lab.captures import CaptureStore
from csbox.lab.fake_data import FakeHomeDataSource
from csbox.lab.models import SessionPaths
from csbox.lab.screen import TerminalEmulator
from csbox.locales import load_locale
from csbox.tui.app import CSBoxApp, ReviewApp
from csbox.tui.screens.review import ReviewController

ROOT = Path(__file__).resolve().parents[3]
OUTPUT = ROOT / "docs" / "assets" / "readme"
HOME_SIZE = (120, 43)
REVIEW_SIZE = (120, 35)
_BOX_DRAWING_GLYPHS = frozenset("─━│┃┌┐└┘├┤┬┴┼╭╮╰╯═║╔╗╚╝╠╣╦╩╬▁▂▃▄▅▆▇▉▊▋▌▍▎▏▔ ")
_SVG_TEXT = re.compile(r"(<text\b[^>]*>)(.*?)(</text>)", re.DOTALL)


@contextmanager
def screenshot_color_environment():
    """Keep terminal-only NO_COLOR settings out of public screenshot assets."""

    previous = os.environ.pop("NO_COLOR", None)
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("NO_COLOR", None)
        else:
            os.environ["NO_COLOR"] = previous


def prepare_svg_for_png(svg: str) -> str:
    """Use CJK glyphs for content and a narrow face for terminal borders."""

    prepared = svg.replace(
        "font-family: Fira Code, monospace;",
        'font-family: "Noto Sans Mono CJK SC", "Noto Sans CJK SC", sans-serif;',
    )

    def keep_terminal_font(match: re.Match[str]) -> str:
        content = html.unescape(match.group(2)).replace("\xa0", " ")
        content = content.replace("\n", "").replace("\r", "")
        if not content.strip() or not all(
            character in _BOX_DRAWING_GLYPHS for character in content
        ):
            return match.group(0)
        opening = match.group(1)
        if "font-family=" not in opening:
            opening = f'{opening[:-1]} font-family="Fira Code, monospace">'
        return f"{opening}{match.group(2)}{match.group(3)}"

    return _SVG_TEXT.sub(keep_terminal_font, prepared)


class DemoHomeDataSource(FakeHomeDataSource):
    """Keep the Home fixture's displayed project path intentionally generic."""

    def get_home_snapshot(self, environment: EnvironmentSnapshot):
        return (
            super()
            .get_home_snapshot(environment)
            .model_copy(update={"project_dir": Path("/tmp/csbox-readme-demo/课程项目")})
        )


def environment() -> EnvironmentSnapshot:
    return EnvironmentSnapshot(
        os_name="Linux",
        os_version="README fixture",
        python_version="3.12.3",
        shell="Bash",
        shell_executable="bash",
        powershell_51_available=False,
        powershell_7_available=False,
        is_wsl=False,
        terminal_columns=120,
        terminal_rows=HOME_SIZE[1],
    )


def make_review_session(root: Path) -> SessionPaths:
    paths = SessionPaths(root / "readme-review")
    paths.root.mkdir(parents=True)
    paths.cast.write_text(
        "\n".join(
            [
                json.dumps({"version": 3, "term": {"cols": 48, "rows": 8}}),
                json.dumps(
                    [
                        0.2,
                        "o",
                        "课程项目实验\r\n"
                        "$ python check.py\r\n"
                        "checking files...\r\n"
                        "checking captures...\r\n"
                        "PASS: 3 checks\r\n",
                    ]
                ),
                json.dumps([0.8, "o", "$ echo 关键画面已保存\r\n关键画面已保存\r\n"]),
                json.dumps([0.5, "o", "$ exit\r\n"]),
                json.dumps([0.5, "x", "0"]),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    paths.metadata.write_text(
        json.dumps(
            {
                "id": "readme-review",
                "name": "课程项目实验",
                "status": "completed",
                "startedAt": "2026-08-11T00:00:00Z",
                "endedAt": "2026-08-11T00:00:10Z",
                "platform": "linux",
                "shell": "bash",
                "shellVersion": "5.2",
                "initialRows": 8,
                "initialColumns": 48,
                "cwd": "/tmp/csbox-readme-demo/课程项目",
                "csboxVersion": "0.5.1",
            }
        ),
        encoding="utf-8",
    )

    def snapshot(text: str, timestamp: float):
        emulator = TerminalEmulator(columns=48, rows=8)
        emulator.apply(
            TerminalEvent(
                sequence=1,
                monotonic_time=timestamp,
                relative_time=timestamp,
                type=TerminalEventType.OUTPUT,
                payload=text.encode("utf-8"),
            )
        )
        return replace(emulator.snapshot(), relative_time=timestamp)

    capture_ids = iter(("capture-1", "capture-2", "capture-3"))
    captures = CaptureStore(
        paths.captures,
        id_factory=lambda: next(capture_ids),
        clock=lambda: datetime(2026, 8, 11, 0, 0, 5, tzinfo=UTC),
    )
    capture_cwd = Path("/tmp/csbox-readme-demo/课程项目")
    for timestamp, title, text in (
        (0.6, "环境准备完成", "课程项目实验\r\n$ pwd\r\n课程项目\r\n"),
        (1.0, "检查结果通过", "课程项目实验\r\n$ python check.py\r\nPASS: 3 checks\r\n"),
        (1.6, "关键画面已保存", "课程项目实验\r\n$ echo 关键画面已保存\r\n关键画面已保存\r\n"),
    ):
        captures.create_capture(
            snapshot(text, timestamp),
            timestamp=timestamp,
            cwd=capture_cwd,
            command="python check.py" if "检查" in title else None,
            title=title,
        )
    return paths


def write_png(app: CSBoxApp | ReviewApp, destination: Path) -> None:
    try:
        import cairosvg
    except ModuleNotFoundError as error:
        raise SystemExit(
            "PNG rendering needs CairoSVG; run "
            "uv run --with cairosvg python docs/tests/tooling/generate_readme_screenshots.py"
        ) from error

    # Textual's export uses Fira Code, which intentionally has no CJK glyphs.
    # The existing Linux CI image provides Noto CJK fonts for the same reason.
    svg = prepare_svg_for_png(app.export_screenshot())
    cairosvg.svg2png(
        bytestring=svg.encode("utf-8"),
        write_to=str(destination),
        output_width=1200,
    )


async def render() -> None:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    locale = load_locale()
    with (
        screenshot_color_environment(),
        tempfile.TemporaryDirectory(prefix="csbox-readme-demo-") as temporary,
    ):
        fixture_root = Path(temporary)
        home = CSBoxApp(
            data_source=DemoHomeDataSource(),
            environment=environment(),
            locale=locale,
        )
        async with home.run_test(size=HOME_SIZE) as pilot:
            await pilot.pause()
            write_png(home, OUTPUT / "home.png")
            await pilot.press("?")
            await pilot.pause()
            write_png(home, OUTPUT / "help.png")

        review = ReviewApp(
            controller=ReviewController.from_session(make_review_session(fixture_root)),
            locale=locale,
        )
        async with review.run_test(size=REVIEW_SIZE) as pilot:
            await pilot.pause()
            await pilot.press("right")
            await pilot.pause()
            write_png(review, OUTPUT / "review.png")


if __name__ == "__main__":
    asyncio.run(render())
