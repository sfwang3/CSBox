from __future__ import annotations

from csbox.core.display_width import _cell_clusters, display_width


def wrap_cells(text: str, width: int) -> tuple[str, ...]:
    """Wrap text by terminal cells without splitting wcwidth clusters."""
    if width <= 0:
        raise ValueError("width must be positive")

    lines: list[str] = []
    for logical_line in text.split("\n"):
        current: list[str] = []
        used = 0
        for cluster, cluster_width in _cell_clusters(logical_line):
            if cluster_width > width:
                raise ValueError(
                    f"cannot fit a display-width {cluster_width} cluster into width {width}"
                )
            if current and used + cluster_width > width:
                lines.append("".join(current))
                current = []
                used = 0
            current.append(cluster)
            used += cluster_width
        lines.append("".join(current))

    result = tuple(lines)
    assert all(display_width(line) <= width for line in result)
    return result
