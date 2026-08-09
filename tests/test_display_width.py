import pytest

from csbox.core.display_width import display_width, pad_cells, truncate_cells


@pytest.mark.parametrize(
    ("text", "width"),
    [
        ("hello", 5),
        ("项目检查", 8),
        ("API 测试", 8),
        ("CSBox 项目检查", 14),
        ("abc中文123", 10),
        ("计算机网络实验", 14),
        (r"C:\Users\测试用户\桌面\实验一", 29),
        ("~/课程实验/计算机网络/实验一", 28),
        ("e\u0301", 1),
        ("Ａ", 2),
    ],
)
def test_display_width_uses_terminal_cells(text: str, width: int) -> None:
    assert display_width(text) == width


def test_truncate_cells_preserves_combining_marks_and_wide_characters() -> None:
    assert truncate_cells("e\u0301x", 1) == "e\u0301"
    assert truncate_cells("a中b", 2) == "a"
    assert truncate_cells("a中b", 3) == "a中"


def test_truncate_cells_handles_limits_and_ellipsis() -> None:
    assert truncate_cells("项目检查", 0) == ""
    assert truncate_cells("项目检查", 3, ellipsis="...") == "..."
    assert truncate_cells("项目检查", 3, ellipsis="中文") == "中"
    with pytest.raises(ValueError, match="width"):
        truncate_cells("项目检查", -1)


def test_pad_cells_uses_terminal_cells() -> None:
    assert pad_cells("项目", 6) == "项目  "
    assert pad_cells("项目检查", 3) == "项目检查"
