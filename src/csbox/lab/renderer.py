from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from PIL import Image, ImageDraw, ImageFont

from csbox.lab.fonts import FontResolver, ResolvedFonts
from csbox.lab.screen import TerminalCell, TerminalSnapshot

RGB = tuple[int, int, int]

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
        path.parent.mkdir(parents=True, exist_ok=True)
        size = (
            snapshot.columns * self.cell_width + self.padding * 2,
            snapshot.rows * self.cell_height + self.padding * 2,
        )
        image = Image.new("RGB", size, selected_theme.background)
        draw = ImageDraw.Draw(image)
        for row_index, row in enumerate(snapshot.cells):
            for column_index, cell in enumerate(row):
                if cell.width == 0:
                    continue
                self._draw_cell(draw, row_index, column_index, cell, selected_theme)
        image.save(path, format="PNG")
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
            font = self._cjk_font if cell.width == 2 else self._ascii_font
            draw.text((x, y), cell.character, font=font, fill=foreground)
        if cell.underline:
            underline_y = y + self.cell_height - 2
            draw.line((x, underline_y, x + span - 1, underline_y), fill=foreground, width=1)
        if cell.strikethrough:
            strike_y = y + self.cell_height // 2
            draw.line((x, strike_y, x + span - 1, strike_y), fill=foreground, width=1)
