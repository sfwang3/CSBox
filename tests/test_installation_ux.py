from __future__ import annotations

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_readme_leads_with_beginner_first_story() -> None:
    readme = (PROJECT_ROOT / "README.en.md").read_text(encoding="utf-8")
    quick_start = readme[readme.index("## Quick Start") : readme.index("## See the current UI")]

    assert "0.7.0" in readme
    assert "stable release on PyPI" in readme
    assert "git clone" in readme
    assert (
        "Home → Record → Evidence → Prepare Submission → Verify → "
        "user manually uploads" in quick_start
    )
    for text in ("开始实验", "F12", "exit", "实验记录"):
        assert text in quick_start
    for text in ("uv sync", "uv run csbox", "开始实验", "F12", "exit", "实验记录"):
        assert text in readme


def test_readme_documents_the_truthful_pypi_installation() -> None:
    readme = (PROJECT_ROOT / "README.en.md").read_text(encoding="utf-8")

    assert "pipx install csbox" not in readme
    assert "pip install csbox" not in readme
    assert "uv tool install csbox" in readme


def test_readme_console_script_matches_metadata() -> None:
    readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
    pyproject = (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")

    assert 'csbox = "csbox.cli:main"' in pyproject
    assert "csbox --help" in readme


def test_readme_describes_the_complete_v06_delivery_surface() -> None:
    readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")

    for surface in (
        "Lab Evidence",
        "API Evidence",
        "Evidence Set",
        "Review",
        "Report",
        "Check",
        "Pack",
    ):
        assert surface in readme
    assert "不生成结论、答案或课程作业正文" in readme


def test_both_readmes_keep_the_beginner_boundaries() -> None:
    english = (PROJECT_ROOT / "README.en.md").read_text(encoding="utf-8")
    chinese = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")

    for readme in (english, chinese):
        assert "F12" in readme
        assert "Check" in readme
        assert "Pack" in readme
        assert "0.7.0" in readme
    assert "不生成结论、答案或课程作业正文" in chinese


def test_changelog_leads_with_the_current_v07_stable_surface() -> None:
    changelog = (PROJECT_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")

    assert changelog.startswith("# Changelog\n\n## 0.7.0")
    assert "## 0.6.1" in changelog
    assert "This version is not published to PyPI" not in changelog
    for surface in (
        "Lab Evidence",
        "API Evidence",
        "Evidence Collection",
        "Report Handoff",
        "Submission Handoff",
    ):
        assert surface in changelog
