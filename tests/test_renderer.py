from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from csbox.lab.fonts import FontResolutionError, FontResolver
from csbox.lab.renderer import RenderTheme, TerminalEvidenceRenderer
from csbox.lab.screen import TerminalCell, TerminalCursor, TerminalSnapshot

ASCII_FONT_CANDIDATES = (
    Path("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"),
    Path("/mnt/c/Windows/Fonts/CascadiaMono.ttf"),
    Path("C:/Windows/Fonts/consola.ttf"),
)
CJK_FONT_CANDIDATES = (
    Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
    Path("/usr/share/fonts/opentype/noto/NotoSansMonoCJK-Regular.ttc"),
    Path("/mnt/c/Windows/Fonts/msyh.ttc"),
    Path("C:/Windows/Fonts/msyh.ttc"),
)


def installed_font(candidates: tuple[Path, ...], purpose: str) -> Path:
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    pytest.skip(f"system {purpose} test font is unavailable")


@pytest.fixture
def font_paths() -> tuple[Path, Path]:
    return (
        installed_font(ASCII_FONT_CANDIDATES, "ASCII monospace"),
        installed_font(CJK_FONT_CANDIDATES, "CJK"),
    )


def snapshot(*rows: tuple[TerminalCell, ...]) -> TerminalSnapshot:
    return TerminalSnapshot(
        rows=len(rows),
        columns=len(rows[0]),
        cells=rows,
        cursor=TerminalCursor(),
        relative_time=1.0,
    )


def test_renderer_has_grid_dimensions_dark_light_backgrounds_and_chinese_path(
    tmp_path: Path, font_paths: tuple[Path, Path]
) -> None:
    terminal = snapshot(
        (TerminalCell(character="A"), TerminalCell(), TerminalCell()),
        (TerminalCell(), TerminalCell(), TerminalCell()),
    )
    renderer = TerminalEvidenceRenderer(
        FontResolver(candidates=font_paths), font_size=18, padding=5
    )

    dark_path = renderer.render(terminal, tmp_path / "证据" / "深色截图.png", RenderTheme.dark())
    light_path = renderer.render(terminal, tmp_path / "证据" / "浅色截图.png", RenderTheme.light())

    expected_size = (
        terminal.columns * renderer.cell_width + renderer.padding * 2,
        terminal.rows * renderer.cell_height + renderer.padding * 2,
    )
    with Image.open(dark_path) as image:
        assert image.size == expected_size
        assert image.convert("RGB").getpixel((0, 0)) == RenderTheme.dark().background
    with Image.open(light_path) as image:
        assert image.size == expected_size
        assert image.convert("RGB").getpixel((0, 0)) == RenderTheme.light().background
    assert dark_path.name == "深色截图.png"


def test_renderer_uses_cell_origins_for_ascii_cjk_and_combining_text(
    tmp_path: Path,
    font_paths: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    terminal = snapshot(
        (
            TerminalCell(character="A"),
            TerminalCell(character="中", width=2),
            TerminalCell(character="", width=0),
            TerminalCell(character="e\u0301"),
        )
    )
    renderer = TerminalEvidenceRenderer(
        FontResolver(candidates=font_paths), font_size=20, padding=7
    )
    text_calls: list[tuple[tuple[float, float], str]] = []
    original_text = ImageDraw.ImageDraw.text

    def observe_text(
        draw: ImageDraw.ImageDraw,
        xy: tuple[float, float],
        text: str,
        *args: object,
        **kwargs: object,
    ) -> None:
        text_calls.append((xy, text))
        original_text(draw, xy, text, *args, **kwargs)

    monkeypatch.setattr(ImageDraw.ImageDraw, "text", observe_text)

    renderer.render(terminal, tmp_path / "layout.png", RenderTheme.dark())

    assert text_calls == [
        ((renderer.padding, renderer.padding), "A"),
        ((renderer.padding + renderer.cell_width, renderer.padding), "中"),
        ((renderer.padding + renderer.cell_width * 3, renderer.padding), "e\u0301"),
    ]


def test_reverse_swaps_cell_colors_and_underline_uses_cell_span(
    tmp_path: Path,
    font_paths: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    terminal = snapshot(
        (
            TerminalCell(
                character="R",
                foreground="red",
                background="blue",
                underline=True,
                reverse=True,
            ),
        )
    )
    renderer = TerminalEvidenceRenderer(FontResolver(candidates=font_paths), padding=3)
    theme = RenderTheme.dark()
    rectangles: list[tuple[tuple[int, int, int, int], tuple[int, int, int]]] = []
    texts: list[tuple[str, tuple[int, int, int]]] = []
    lines: list[tuple[tuple[int, int, int, int], tuple[int, int, int]]] = []
    original_rectangle = ImageDraw.ImageDraw.rectangle
    original_text = ImageDraw.ImageDraw.text
    original_line = ImageDraw.ImageDraw.line

    def observe_rectangle(
        draw: ImageDraw.ImageDraw,
        xy: tuple[int, int, int, int],
        *args: object,
        **kwargs: object,
    ) -> None:
        rectangles.append((xy, kwargs["fill"]))
        original_rectangle(draw, xy, *args, **kwargs)

    def observe_text(
        draw: ImageDraw.ImageDraw,
        xy: tuple[float, float],
        text: str,
        *args: object,
        **kwargs: object,
    ) -> None:
        texts.append((text, kwargs["fill"]))
        original_text(draw, xy, text, *args, **kwargs)

    def observe_line(
        draw: ImageDraw.ImageDraw,
        xy: tuple[int, int, int, int],
        *args: object,
        **kwargs: object,
    ) -> None:
        lines.append((xy, kwargs["fill"]))
        original_line(draw, xy, *args, **kwargs)

    monkeypatch.setattr(ImageDraw.ImageDraw, "rectangle", observe_rectangle)
    monkeypatch.setattr(ImageDraw.ImageDraw, "text", observe_text)
    monkeypatch.setattr(ImageDraw.ImageDraw, "line", observe_line)

    renderer.render(terminal, tmp_path / "attributes.png", theme)

    assert rectangles[-1] == (
        (
            renderer.padding,
            renderer.padding,
            renderer.padding + renderer.cell_width - 1,
            renderer.padding + renderer.cell_height - 1,
        ),
        theme.resolve_color("red", background=True),
    )
    assert texts[-1] == ("R", theme.resolve_color("blue"))
    assert lines[-1] == (
        (
            renderer.padding,
            renderer.padding + renderer.cell_height - 2,
            renderer.padding + renderer.cell_width - 1,
            renderer.padding + renderer.cell_height - 2,
        ),
        theme.resolve_color("blue"),
    )


def test_font_resolver_is_deterministic_and_requires_real_cjk_coverage(
    font_paths: tuple[Path, Path],
) -> None:
    ascii_font, cjk_font = font_paths
    resolver = FontResolver(explicit=ascii_font, candidates=(cjk_font,))

    first = resolver.resolve()
    second = resolver.resolve()

    assert first == second
    assert first.ascii == ascii_font
    assert first.cjk == cjk_font

    with pytest.raises(FontResolutionError, match="中文.*字体|字体.*中文"):
        FontResolver(candidates=(ascii_font,)).resolve()
