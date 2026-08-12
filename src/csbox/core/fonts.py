"""Shared font-resolution API used by Lab and API evidence renderers."""

from csbox.lab.fonts import FontResolutionError, FontResolver, ResolvedFonts, font_supports_text

__all__ = ["FontResolutionError", "FontResolver", "ResolvedFonts", "font_supports_text"]
