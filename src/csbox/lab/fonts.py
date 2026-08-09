from __future__ import annotations

import os
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from PIL import ImageFont


class FontResolutionError(RuntimeError):
    """No system font combination can render terminal evidence safely."""


@dataclass(frozen=True, slots=True)
class ResolvedFonts:
    ascii: Path
    cjk: Path


class FontResolver:
    """Resolve a deterministic monospace/CJK system-font pair."""

    def __init__(
        self,
        explicit: Path | str | None = None,
        candidates: Iterable[Path | str] | None = None,
    ) -> None:
        self.explicit = Path(explicit).expanduser() if explicit is not None else None
        supplied = tuple(Path(candidate).expanduser() for candidate in candidates or ())
        search = supplied if candidates is not None else _default_candidates()
        ordered = ((self.explicit,) if self.explicit is not None else ()) + search
        self.candidates = tuple(dict.fromkeys(ordered))
        self._resolved: ResolvedFonts | None = None

    def resolve(self) -> ResolvedFonts:
        if self._resolved is not None:
            return self._resolved
        available = tuple(path for path in self.candidates if path.is_file())
        ascii_font = next((path for path in available if _is_ascii_monospace(path)), None)
        if ascii_font is None:
            raise FontResolutionError(
                "找不到可用的等宽字体，请在配置 render.font 中填写字体文件路径。"
            )
        cjk_font = next((path for path in available if _has_cjk_coverage(path)), None)
        if cjk_font is None:
            raise FontResolutionError(
                "找不到支持中文的字体，请安装 Noto CJK/微软雅黑，"
                "或在配置 render.font 中填写支持中文的字体文件路径。"
            )
        self._resolved = ResolvedFonts(ascii=ascii_font, cjk=cjk_font)
        return self._resolved


def _default_candidates() -> tuple[Path, ...]:
    windows_root = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
    wsl_windows = Path("/mnt/c/Windows/Fonts")
    roots_and_names = (
        (windows_root, "CascadiaMono.ttf"),
        (windows_root, "CascadiaCode.ttf"),
        (windows_root, "consola.ttf"),
        (windows_root, "msyh.ttc"),
        (windows_root, "simsun.ttc"),
        (Path("/usr/share/fonts/truetype/dejavu"), "DejaVuSansMono.ttf"),
        (Path("/usr/share/fonts/truetype/noto"), "NotoSansMono-Regular.ttf"),
        (Path("/usr/share/fonts/opentype/noto"), "NotoSansMonoCJK-Regular.ttc"),
        (Path("/usr/share/fonts/opentype/noto"), "NotoSansCJK-Regular.ttc"),
        (Path("/usr/share/fonts/truetype/noto"), "NotoSansCJK-Regular.ttc"),
        (wsl_windows, "CascadiaMono.ttf"),
        (wsl_windows, "CascadiaCode.ttf"),
        (wsl_windows, "consola.ttf"),
        (wsl_windows, "msyh.ttc"),
        (wsl_windows, "simsun.ttc"),
    )
    return tuple(root / name for root, name in roots_and_names)


def _load_font(path: Path, size: int = 16) -> ImageFont.FreeTypeFont | None:
    try:
        return ImageFont.truetype(str(path), size=size)
    except (OSError, ValueError):
        return None


def _is_ascii_monospace(path: Path) -> bool:
    font = _load_font(path)
    if font is None or not _has_glyph(font, "A"):
        return False
    advances = [font.getlength(character) for character in ("i", "M", "0", " ")]
    return max(advances) - min(advances) < 0.01


def _has_cjk_coverage(path: Path) -> bool:
    font = _load_font(path)
    return font is not None and all(_has_glyph(font, character) for character in "中文项目")


def _has_glyph(font: ImageFont.FreeTypeFont, character: str) -> bool:
    return _glyph_signature(font, character) != _glyph_signature(font, "\u0378")


def _glyph_signature(
    font: ImageFont.FreeTypeFont, character: str
) -> tuple[tuple[int, int], tuple[int, int, int, int] | None, bytes]:
    mask = font.getmask(character, mode="L")
    return mask.size, mask.getbbox(), bytes(mask)
