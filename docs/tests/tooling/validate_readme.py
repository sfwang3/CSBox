"""Deterministic contract checks for the two public README files."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
README_PATHS = {
    "chinese": ROOT / "README.md",
    "english": ROOT / "README.en.md",
}
LINK_RE = re.compile(r"\]\(([^)]+)\)")
ANCHOR_RE = re.compile(r'href="#([^"]+)"')

REQUIRED_SECTIONS = {
    "chinese": (
        "## 我到底能拿 CSBox 干什么？",
        "## 第一次使用 CSBox？从这里开始",
        "## 快速开始",
        "## 学生的一条完整路径",
        "## 看看当前界面",
        "## 使用前 → 交付后",
        "## 按目标理解功能",
        "## 文件和导出结果",
        "## 能做 / 不能做",
        "## 本地优先与隐私",
        "## 兼容性",
        "## 常见问题",
        "高级：直接使用 CLI 命令",
    ),
    "english": (
        "## What can I actually use CSBox for?",
        "## First time using CSBox? Start here",
        "## Quick Start",
        "## A student journey",
        "## See the current UI",
        "## Before → after",
        "## Features organized around your goals",
        "## What files and exports look like",
        "## Does / Does not",
        "## Local-first and privacy",
        "## Compatibility",
        "## FAQ",
        "Advanced: direct CLI commands",
    ),
}

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

LANGUAGE_URLS = {
    "chinese": (
        "https://github.com/sfwang3/CSBox/blob/main/README.en.md",
        "https://github.com/sfwang3/CSBox/blob/main/README.md",
    ),
    "english": (
        "https://github.com/sfwang3/CSBox/blob/main/README.md",
        "https://github.com/sfwang3/CSBox/blob/main/README.en.md",
    ),
}

SCREENSHOT_URLS = {
    "home": "https://raw.githubusercontent.com/sfwang3/CSBox/main/docs/assets/readme/home.png",
    "help": "https://raw.githubusercontent.com/sfwang3/CSBox/main/docs/assets/readme/help.png",
    "review": "https://raw.githubusercontent.com/sfwang3/CSBox/main/docs/assets/readme/review.png",
}

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


def _check_local_links(name: str, text: str) -> None:
    for target in LINK_RE.findall(text):
        target = target.split(None, 1)[0]
        if target.startswith(("#", "http://", "https://", "mailto:")):
            continue
        local_target = target.split("#", 1)[0]
        assert (ROOT / local_target).is_file(), f"{name}: missing local link: {target}"

    anchors = set(ANCHOR_RE.findall(text))
    ids = set(re.findall(r'\bid="([^"]+)"', text))
    headings = {
        re.sub(r"[^a-z0-9 -]", "", heading.lower()).strip().replace(" ", "-")
        for heading in re.findall(r"^#{1,6} +(.+)$", text, re.MULTILINE)
    }
    for anchor in anchors:
        assert anchor in ids or anchor in headings, f"{name}: missing anchor #{anchor}"


def _check_readme(name: str, text: str) -> None:
    assert "0.6.0rc1" in text
    assert "README.zh-CN.md" not in text
    assert "uv tool install csbox" in text
    assert "pip install csbox" not in text.lower()
    if name == "chinese":
        assert re.search(r"[\u4e00-\u9fff]", text)
        assert "尚未发布到 PyPI" in text
        assert "不生成结论、答案或课程作业正文" in text
    else:
        assert "not published to PyPI" in text
        assert "does not generate conclusions, answers, or coursework prose" in text
    assert "Lab Capture ─┐" in text
    assert "Evidence Set ── Report" in text
    assert "API Step ────┘" in text
    for section in REQUIRED_SECTIONS[name]:
        assert section in text, f"{name}: missing section {section}"
    for url in LANGUAGE_URLS[name]:
        assert url in text
    for url in SCREENSHOT_URLS.values():
        assert f'href="{url}"' in text
        assert f'src="{url}"' in text
    _check_local_links(name, text)


def main() -> None:
    assert set(path.name for path in README_PATHS.values()) == {"README.md", "README.en.md"}
    assert not (ROOT / "README.zh-CN.md").exists()
    texts = {name: path.read_text(encoding="utf-8") for name, path in README_PATHS.items()}
    for name, text in texts.items():
        _check_readme(name, text)

    for command in DOCUMENTED_COMMANDS:
        assert all(command in text for text in texts.values()), command

    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert 'readme = "README.md"' in pyproject
    assert '"README.md"' in pyproject
    assert '"README.en.md"' in pyproject

    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert changelog.startswith("# Changelog\n\n## 0.6.0rc1")
    released = changelog.split("## 0.5.0rc1", maxsplit=1)[0]
    assert "This version is not published to PyPI" not in released

    lab_keymap = (ROOT / "src/csbox/lab/keymap.py").read_text(encoding="utf-8")
    review_keymap = (ROOT / "src/csbox/tui/keymap.py").read_text(encoding="utf-8")
    help_source = (ROOT / "src/csbox/tui/help.py").read_text(encoding="utf-8")
    assert '"f12"' in lab_keymap
    for key in REVIEW_KEYS:
        assert f'"{key}"' in review_keymap, key
    assert 'Binding("?"' in help_source and 'Binding("f1"' in help_source

    for name in SCREENSHOT_URLS:
        asset = ROOT / "docs" / "assets" / "readme" / f"{name}.png"
        assert asset.is_file() and asset.stat().st_size > 0, asset
    print("README contract checks passed")


if __name__ == "__main__":
    main()
