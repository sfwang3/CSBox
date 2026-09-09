from __future__ import annotations

import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCREENSHOT_URLS = {
    "home": "https://raw.githubusercontent.com/sfwang3/CSBox/main/docs/assets/readme/home.png",
    "help": "https://raw.githubusercontent.com/sfwang3/CSBox/main/docs/assets/readme/help.png",
    "review": "https://raw.githubusercontent.com/sfwang3/CSBox/main/docs/assets/readme/review.png",
}
COMMON_COMMANDS = (
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


def test_public_readmes_have_the_two_file_pypi_safe_contract() -> None:
    chinese_path = PROJECT_ROOT / "README.md"
    english_path = PROJECT_ROOT / "README.en.md"

    assert chinese_path.is_file()
    assert english_path.is_file()
    assert not (PROJECT_ROOT / "README.zh-CN.md").exists()

    chinese = chinese_path.read_text(encoding="utf-8")
    english = english_path.read_text(encoding="utf-8")
    assert "## 快速开始" in chinese
    assert "CSBox 是" in chinese
    assert "## Quick Start" in english
    assert "CSBox is" in english
    assert "https://github.com/sfwang3/CSBox/blob/main/README.en.md" in chinese
    assert "https://github.com/sfwang3/CSBox/blob/main/README.md" in english

    for text in (chinese, english):
        for url in SCREENSHOT_URLS.values():
            assert f'href="{url}"' in text
            assert f'src="{url}"' in text
        for command in COMMON_COMMANDS:
            assert command in text
        assert "0.6.0" in text
        assert "Check" in text
        assert "Pack" in text
        assert "does not generate" in text or "不生成" in text


def test_readme_metadata_and_changelog_match_the_development_baseline() -> None:
    pyproject = (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    changelog = (PROJECT_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")

    assert 'readme = "README.md"' in pyproject
    assert '"README.en.md"' in pyproject
    assert "## 0.6.0" in changelog
    released = changelog.split("## 0.5.0rc1", maxsplit=1)[0]
    assert "not published to PyPI" not in released
    assert "尚未发布到 PyPI" not in released


def test_readme_screenshot_targets_are_local_public_assets() -> None:
    for name in ("home", "help", "review"):
        path = PROJECT_ROOT / "docs" / "assets" / "readme" / f"{name}.png"
        assert path.is_file()
        assert path.stat().st_size > 0

    for readme in (PROJECT_ROOT / "README.md", PROJECT_ROOT / "README.en.md"):
        text = readme.read_text(encoding="utf-8")
        assert not re.search(r"(?:src|href)=\"docs/assets/readme/", text)
