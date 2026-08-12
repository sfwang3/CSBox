from __future__ import annotations

from csbox.core.display_width import _cell_clusters, display_width


def _logical_lines(text: str):
    start = 0
    while True:
        end = text.find("\n", start)
        if end < 0:
            yield text[start:]
            return
        yield text[start:end]
        start = end + 1


def wrap_cells(text: str, width: int, *, max_lines: int | None = None) -> tuple[str, ...]:
    """Wrap text by terminal cells without splitting wcwidth clusters."""
    if width <= 0:
        raise ValueError("width must be positive")
    if max_lines is not None and (type(max_lines) is not int or max_lines < 0):
        raise ValueError("max_lines must be a non-negative integer or None")
    if max_lines == 0:
        return ()

    lines: list[str] = []
    for logical_line in _logical_lines(text):
        current: list[str] = []
        used = 0
        for cluster, cluster_width in _cell_clusters(logical_line):
            if cluster_width > width:
                raise ValueError(
                    f"cannot fit a display-width {cluster_width} cluster into width {width}"
                )
            if current and used + cluster_width > width:
                lines.append("".join(current))
                if max_lines is not None and len(lines) >= max_lines:
                    return tuple(lines)
                current = []
                used = 0
            current.append(cluster)
            used += cluster_width
        lines.append("".join(current))
        if max_lines is not None and len(lines) >= max_lines:
            return tuple(lines)

    result = tuple(lines)
    assert all(display_width(line) <= width for line in result)
    return result
