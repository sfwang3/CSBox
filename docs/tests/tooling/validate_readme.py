"""Small contract check for the two public README files."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
READMES = (ROOT / "README.md", ROOT / "README.zh-CN.md")
LINK_RE = re.compile(r"\]\(([^)]+)\)")
ANCHOR_RE = re.compile(r"href=\"#([^\"]+)\"")

REQUIRED = (
    ("use_cases", "What can I actually use CSBox for?", "我到底能拿 CSBox 干什么？"),
    ("start", "First time using CSBox? Start here", "第一次使用 CSBox？从这里开始"),
    ("quick", "## Quick Start", "## 快速开始"),
    ("journey", "## A student journey", "## 学生的一条完整路径"),
    ("screenshots", "## See the current UI", "## 看看当前界面"),
    ("before_after", "## Before → after", "## 使用前 → 交付后"),
    ("features", "## Features organized around your goals", "## 按目标理解功能"),
    ("files", "## What files and exports look like", "## 文件和导出结果"),
    ("boundaries", "## Does / Does not", "## 能做 / 不能做"),
    ("privacy", "## Local-first and privacy", "## 本地优先与隐私"),
    ("compatibility", "## Compatibility", "## 兼容性"),
    ("faq", "## FAQ", "## 常见问题"),
    ("advanced", "Advanced: direct CLI commands", "高级：直接使用 CLI 命令"),
)

DOCUMENTED_COMMANDS = (
    "csbox --help",
    "csbox --version",
    "csbox doctor",
    "csbox lab list",
    "csbox lab review",
    "csbox lab export",
    "csbox api",
    "csbox check --plain",
    "csbox pack --dry-run",
    "csbox pack --verify",
    "csbox report export",
)

REVIEW_KEYS = (
    "space",
    "left",
    "right",
    "pageup",
    "pagedown",
    "up",
    "down",
    "c",
    "j",
    "e",
    "delete",
    "tab",
    "escape",
    "q",
)


def main() -> None:
    texts = {path.name: path.read_text(encoding="utf-8") for path in READMES}
    for path in READMES:
        text = texts[path.name]
        assert not re.search(r"\bv?0\.5[ABC]\b", text, flags=re.IGNORECASE)
        assert "pip install csbox" not in text.lower()
        assert "install csbox from pypi" not in text.lower()
        assert ("not" in text.lower() or "尚未" in text or "未" in text) and "pypi" in text.lower()
        for target in LINK_RE.findall(text):
            target = target.split(None, 1)[0]
            if target.startswith(("#", "http://", "https://", "mailto:")):
                continue
            local_target = target.split("#", 1)[0]
            assert (ROOT / local_target).is_file(), f"missing local link: {target}"
        anchors = set(ANCHOR_RE.findall(text))
        ids = set(re.findall(r"\bid=\"([^\"]+)\"", text))
        headings = {
            re.sub(r"[^a-z0-9 -]", "", heading.lower()).strip().replace(" ", "-")
            for heading in re.findall(r"^#{1,6} +(.+)$", text, re.MULTILINE)
        }
        for anchor in anchors:
            assert anchor in ids or anchor in headings, f"missing anchor #{anchor}"

    for marker, english, chinese in REQUIRED:
        del marker
        assert english in texts["README.md"], english
        assert chinese in texts["README.zh-CN.md"], chinese

    for command in DOCUMENTED_COMMANDS:
        assert command in texts["README.md"], command
        assert command in texts["README.zh-CN.md"], command

    lab_keymap = (ROOT / "src/csbox/lab/keymap.py").read_text(encoding="utf-8")
    review_keymap = (ROOT / "src/csbox/tui/keymap.py").read_text(encoding="utf-8")
    help_source = (ROOT / "src/csbox/tui/help.py").read_text(encoding="utf-8")
    assert '"f12"' in lab_keymap
    for key in REVIEW_KEYS:
        assert f'"{key}"' in review_keymap, key
    assert 'Binding("?"' in help_source and 'Binding("f1"' in help_source

    expected_assets = {
        ROOT / "docs/assets/readme/home.png",
        ROOT / "docs/assets/readme/help.png",
        ROOT / "docs/assets/readme/review.png",
    }
    for asset in expected_assets:
        assert asset.is_file() and asset.stat().st_size > 0, asset
    print("README contract checks passed")


if __name__ == "__main__":
    main()
