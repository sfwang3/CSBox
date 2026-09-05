"""Keyboard bindings shared by the interactive terminal views."""

from __future__ import annotations

from typing import Final

KEYMAP: Final[dict[str, str]] = {
    "toggle_play": "space",
    "seek_back": "left",
    "seek_forward": "right",
    "seek_back_large": "pageup",
    "seek_forward_large": "pagedown",
    "select_previous_capture": "up",
    "select_next_capture": "down",
    "create_capture": "c",
    "jump_capture": "j",
    "edit_capture": "e",
    "delete_capture": "delete",
    "cycle_pane": "tab",
    "go_back": "escape",
}

REVIEW_BINDINGS: Final[tuple[tuple[str, str, str], ...]] = (
    (KEYMAP["cycle_pane"], "cycle_pane", "切换视图"),
    (KEYMAP["toggle_play"], "toggle_play", "播放/暂停"),
    (KEYMAP["seek_back"], "seek_back", "后退"),
    (KEYMAP["seek_forward"], "seek_forward", "前进"),
    (KEYMAP["seek_back_large"], "seek_back_large", "大步后退"),
    (KEYMAP["seek_forward_large"], "seek_forward_large", "大步前进"),
    (KEYMAP["select_previous_capture"], "select_previous_capture", "上一个关键画面"),
    (KEYMAP["select_next_capture"], "select_next_capture", "下一个关键画面"),
    (KEYMAP["create_capture"], "create_capture", "补充关键画面"),
    (KEYMAP["jump_capture"], "jump_capture", "跳转关键画面"),
    (KEYMAP["edit_capture"], "edit_capture", "编辑标题"),
    (KEYMAP["delete_capture"], "delete_capture", "删除关键画面"),
    (KEYMAP["go_back"], "go_back", "返回"),
    ("q", "go_back", "返回"),
)

__all__ = ["KEYMAP", "REVIEW_BINDINGS"]
