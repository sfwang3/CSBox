from __future__ import annotations

from collections.abc import Iterator

from wcwidth import wcwidth


def _cell_width(character: str) -> int:
    return max(wcwidth(character), 0)


def _cell_clusters(text: str) -> Iterator[tuple[str, int]]:
    cluster = ""
    cluster_width = 0

    for character in text:
        character_width = _cell_width(character)
        if character_width == 0:
            cluster += character
            continue

        if cluster:
            yield cluster, cluster_width
        cluster = character
        cluster_width = character_width

    if cluster:
        yield cluster, cluster_width


def display_width(text: str) -> int:
    """Return the number of terminal cells occupied by *text*."""
    return sum(_cell_width(character) for character in text)


def _truncate_prefix(text: str, width: int) -> str:
    result: list[str] = []
    used_width = 0
    for cluster, cluster_width in _cell_clusters(text):
        if used_width + cluster_width > width:
            break
        result.append(cluster)
        used_width += cluster_width
    return "".join(result)


def truncate_cells(text: str, width: int, ellipsis: str = "") -> str:
    """Truncate *text* to *width* terminal cells without splitting glyph parts."""
    if width < 0:
        raise ValueError("width must not be negative")
    if width == 0:
        return ""
    if display_width(text) <= width:
        return text

    ellipsis_width = display_width(ellipsis)
    if ellipsis_width > width:
        return _truncate_prefix(ellipsis, width)
    return _truncate_prefix(text, width - ellipsis_width) + ellipsis


def pad_cells(text: str, width: int) -> str:
    """Pad *text* with spaces until it occupies at least *width* terminal cells."""
    if width < 0:
        raise ValueError("width must not be negative")
    return text + " " * max(width - display_width(text), 0)
