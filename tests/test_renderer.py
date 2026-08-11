from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from csbox.lab.fonts import FontResolutionError, FontResolver
from csbox.lab.renderer import (
    RenderTheme,
    TerminalEvidenceRenderer,
    _save_png_atomic,
)
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


def test_font_resolver_is_deterministic(
    font_paths: tuple[Path, Path],
) -> None:
    ascii_font, cjk_font = font_paths
    resolver = FontResolver(explicit=ascii_font, candidates=(cjk_font,))

    first = resolver.resolve()
    second = resolver.resolve()

    assert first == second
    assert first.ascii == ascii_font
    assert first.cjk == cjk_font


def test_font_resolver_missing_cjk_error_does_not_depend_on_system_cjk_font(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dummy_font = tmp_path / "ascii-only.ttf"
    dummy_font.write_bytes(b"test double boundary")
    monkeypatch.setattr("csbox.lab.fonts._is_ascii_monospace", lambda path: True)
    monkeypatch.setattr("csbox.lab.fonts._has_cjk_coverage", lambda path: False)

    with pytest.raises(FontResolutionError, match="中文.*字体|字体.*中文"):
        FontResolver(candidates=(dummy_font,)).resolve()


@pytest.mark.parametrize(
    "missing_glyph", ("\U00020000", "\ue000"), ids=("extension-b", "private-use")
)
def test_renderer_rejects_actual_snapshot_glyph_missing_from_all_candidates(
    tmp_path: Path, font_paths: tuple[Path, Path], missing_glyph: str
) -> None:
    terminal = snapshot(
        (
            TerminalCell(character=missing_glyph, width=2),
            TerminalCell(character="", width=0),
        )
    )
    renderer = TerminalEvidenceRenderer(FontResolver(candidates=font_paths))
    destination = tmp_path / "missing-extension-b.png"

    with pytest.raises(FontResolutionError, match="字符|字形|字体"):
        renderer.render(terminal, destination, RenderTheme.dark())

    assert not destination.exists()


def test_renderer_atomically_preserves_existing_png_when_save_fails(
    tmp_path: Path,
    font_paths: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    terminal = snapshot((TerminalCell(character="A"),))
    renderer = TerminalEvidenceRenderer(FontResolver(candidates=font_paths))
    destination = tmp_path / "evidence.png"
    destination.write_bytes(b"previous png")

    def fail_save(image: Image.Image, target: str | Path, *args: object, **kwargs: object) -> None:
        Path(target).write_bytes(b"partial png")
        raise OSError("disk full")

    monkeypatch.setattr(Image.Image, "save", fail_save)

    with pytest.raises(OSError, match="disk full"):
        renderer.render(terminal, destination, RenderTheme.dark())

    assert destination.read_bytes() == b"previous png"
    assert not list(tmp_path.glob("*.tmp"))


def test_renderer_fsynchronizes_png_with_a_write_capable_descriptor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "evidence.png"
    opened_modes: list[str] = []
    real_open = Path.open

    def observe_open(
        path: Path,
        mode: str = "r",
        buffering: int = -1,
        encoding: str | None = None,
        errors: str | None = None,
        newline: str | None = None,
    ) -> object:
        if path.parent == tmp_path and path.name.startswith(".evidence.png."):
            opened_modes.append(mode)
        return real_open(path, mode, buffering, encoding, errors, newline)

    monkeypatch.setattr(Path, "open", observe_open)

    _save_png_atomic(Image.new("RGB", (2, 2), "black"), destination)

    assert "r+b" in opened_modes


def test_renderer_marks_bold_text_without_changing_its_cell_origin(
    tmp_path: Path,
    font_paths: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    terminal = snapshot((TerminalCell(character="B", bold=True),))
    renderer = TerminalEvidenceRenderer(FontResolver(candidates=font_paths), padding=4)
    calls: list[tuple[tuple[float, float], int]] = []
    original_text = ImageDraw.ImageDraw.text

    def observe_text(
        draw: ImageDraw.ImageDraw,
        xy: tuple[float, float],
        text: str,
        *args: object,
        **kwargs: object,
    ) -> None:
        calls.append((xy, int(kwargs.get("stroke_width", 0))))
        original_text(draw, xy, text, *args, **kwargs)

    monkeypatch.setattr(ImageDraw.ImageDraw, "text", observe_text)

    renderer.render(terminal, tmp_path / "bold.png", RenderTheme.dark())

    assert calls == [((renderer.padding, renderer.padding), 1)]
