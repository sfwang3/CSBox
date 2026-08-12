from __future__ import annotations

import math
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Final

from PIL import Image, ImageDraw, ImageFont

from csbox.core.safe_paths import atomic_write_bytes
from csbox.lab.fonts import (
    FontResolutionError,
    FontResolver,
    ResolvedFonts,
    font_supports_text,
)
from csbox.lab.screen import TerminalCell, TerminalSnapshot

RGB = tuple[int, int, int]
MAX_RENDER_PIXELS: Final = 32_000_000
_MAX_FONT_CACHE_ENTRIES: Final = 1024

_DARK_PALETTE: Final[dict[str, RGB]] = {
    "black": (0, 0, 0),
    "red": (205, 49, 49),
    "green": (13, 188, 121),
    "brown": (229, 229, 16),
    "yellow": (229, 229, 16),
    "blue": (36, 114, 200),
    "magenta": (188, 63, 188),
    "cyan": (17, 168, 205),
    "white": (229, 229, 229),
    "brightblack": (102, 102, 102),
    "brightred": (241, 76, 76),
    "brightgreen": (35, 209, 139),
    "brightyellow": (245, 245, 67),
    "brightblue": (59, 142, 234),
    "brightmagenta": (214, 112, 214),
    "brightcyan": (41, 184, 219),
    "brightwhite": (255, 255, 255),
}

_LIGHT_PALETTE: Final[dict[str, RGB]] = {
    **_DARK_PALETTE,
    "black": (32, 32, 32),
    "white": (232, 232, 232),
    "brightwhite": (255, 255, 255),
}


@dataclass(frozen=True, slots=True)
class RenderTheme:
    background: RGB
    foreground: RGB
    palette: dict[str, RGB]

    @classmethod
    def dark(cls) -> RenderTheme:
        return cls(background=(12, 12, 12), foreground=(204, 204, 204), palette=_DARK_PALETTE)

    @classmethod
    def light(cls) -> RenderTheme:
        return cls(background=(250, 250, 250), foreground=(32, 32, 32), palette=_LIGHT_PALETTE)

    def resolve_color(self, value: str, *, background: bool = False) -> RGB:
        if value == "default":
            return self.background if background else self.foreground
        normalized = value.lower().removeprefix("#")
        if len(normalized) == 6:
            try:
                return tuple(bytes.fromhex(normalized))  # type: ignore[return-value]
            except ValueError:
                pass
        return self.palette.get(value.lower(), self.background if background else self.foreground)


class TerminalEvidenceRenderer:
    """Render immutable terminal snapshots on a fixed Pillow cell grid."""

    def __init__(
        self,
        font_resolver: FontResolver | None = None,
        *,
        font_size: int = 18,
        padding: int = 8,
    ) -> None:
        if font_size <= 0:
            raise ValueError("font_size must be positive")
        if padding < 0:
            raise ValueError("padding must not be negative")
        self.font_size = font_size
        self.padding = padding
        self.fonts: ResolvedFonts = (font_resolver or FontResolver()).resolve()
        self._ascii_font = ImageFont.truetype(str(self.fonts.ascii), size=font_size)
        self._cjk_font = ImageFont.truetype(str(self.fonts.cjk), size=font_size)
        self._fallback_fonts = tuple(
            ImageFont.truetype(str(path), size=font_size) for path in self.fonts.fallbacks
        )
        self._font_cache: dict[str, ImageFont.FreeTypeFont] = {}
        ascii_advance = self._ascii_font.getlength("M")
        cjk_half_advance = self._cjk_font.getlength("中") / 2
        self.cell_width = max(1, math.ceil(ascii_advance), math.ceil(cjk_half_advance))
        ascii_ascent, ascii_descent = self._ascii_font.getmetrics()
        cjk_ascent, cjk_descent = self._cjk_font.getmetrics()
        self.cell_height = max(ascii_ascent + ascii_descent, cjk_ascent + cjk_descent)

    def render(
        self,
        snapshot: TerminalSnapshot,
        destination: Path | str,
        theme: RenderTheme | None = None,
    ) -> Path:
        if not isinstance(snapshot, TerminalSnapshot):
            raise TypeError("snapshot must be a TerminalSnapshot")
        selected_theme = theme or RenderTheme.dark()
        path = Path(destination)
        size = (
            snapshot.columns * self.cell_width + self.padding * 2,
            snapshot.rows * self.cell_height + self.padding * 2,
        )
        if size[0] * size[1] > MAX_RENDER_PIXELS:
            raise ValueError("终端证据图片像素尺寸过大，请缩小终端后重试。")
        image = Image.new("RGB", size, selected_theme.background)
        draw = ImageDraw.Draw(image)
        for row_index, row in enumerate(snapshot.cells):
            for column_index, cell in enumerate(row):
                if cell.width == 0:
                    continue
                self._draw_cell(draw, row_index, column_index, cell, selected_theme)
        _save_png_atomic(image, path)
        return path

    def _draw_cell(
        self,
        draw: ImageDraw.ImageDraw,
        row: int,
        column: int,
        cell: TerminalCell,
        theme: RenderTheme,
    ) -> None:
        x = self.padding + column * self.cell_width
        y = self.padding + row * self.cell_height
        span = self.cell_width * max(1, cell.width)
        foreground = theme.resolve_color(cell.foreground)
        background = theme.resolve_color(cell.background, background=True)
        if cell.reverse:
            foreground, background = background, foreground
        draw.rectangle(
            (x, y, x + span - 1, y + self.cell_height - 1),
            fill=background,
        )
        if cell.character:
            font = self._font_for_text(cell.character, wide=cell.width == 2)
            text_options: dict[str, object] = {"font": font, "fill": foreground}
            if cell.bold:
                text_options.update(stroke_width=1, stroke_fill=foreground)
            draw.text((x, y), cell.character, **text_options)
        if cell.underline:
            underline_y = y + self.cell_height - 2
            draw.line((x, underline_y, x + span - 1, underline_y), fill=foreground, width=1)
        if cell.strikethrough:
            strike_y = y + self.cell_height // 2
            draw.line((x, strike_y, x + span - 1, strike_y), fill=foreground, width=1)

    def _font_for_text(self, text: str, *, wide: bool) -> ImageFont.FreeTypeFont:
        cached = self._font_cache.get(text)
        if cached is not None:
            return cached
        preferred = (self._cjk_font, *self._fallback_fonts, self._ascii_font)
        if not wide and text.isascii():
            preferred = (self._ascii_font, self._cjk_font, *self._fallback_fonts)
        for font in preferred:
            if font_supports_text(font, text):
                if len(self._font_cache) >= _MAX_FONT_CACHE_ENTRIES:
                    self._font_cache.clear()
                self._font_cache[text] = font
                return font
        codepoints = " ".join(f"U+{ord(character):04X}" for character in text)
        raise FontResolutionError(
            f"找不到能显示字符 {codepoints} 的字体，请配置包含该字形的 render.font。"
        )


def _save_png_atomic(image: Image.Image, destination: Path) -> None:
    stream = BytesIO()
    image.save(stream, format="PNG")
    atomic_write_bytes(destination, stream.getvalue())
