from __future__ import annotations

import pytest

from csbox.core.display_width import display_width
from csbox.core.text_layout import wrap_cells


@pytest.mark.parametrize("width", [2, 3])
@pytest.mark.parametrize("text", ["计算机网络实验", "abc中文123"])
def test_wrap_cells_never_exceeds_display_width(text: str, width: int) -> None:
    lines = wrap_cells(text, width)

    assert lines
    assert all(display_width(line) <= width for line in lines)


def test_wrap_cells_never_exceeds_display_width_for_ascii_at_width_one() -> None:
    lines = wrap_cells("abc", 1)

    assert all(display_width(line) <= 1 for line in lines)


def test_wrap_cells_does_not_split_a_combining_character_from_its_base() -> None:
    lines = wrap_cells("e\u0301abc", 1)

    assert lines[0] == "e\u0301"
    assert all(display_width(line) <= 1 for line in lines)


def test_wrap_cells_rejects_a_cluster_wider_than_the_requested_width() -> None:
    with pytest.raises(ValueError, match="cannot fit.*width 1"):
        wrap_cells("中", 1)


def test_wrap_cells_rejects_non_positive_width() -> None:
    with pytest.raises(ValueError):
        wrap_cells("abc", 0)
