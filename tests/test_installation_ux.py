from __future__ import annotations

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_readme_leads_with_beginner_first_story() -> None:
    readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
    quick_start = readme[readme.index("## 快速开始") : readme.index("## 平台范围")]

    assert "当前 0.5.0.dev0 开发版尚未发布到 PyPI" in readme
    assert "git clone" in readme
    assert (
        "安装 → csbox → 开始实验 → F12 保存关键画面 → 输入 exit → 实验记录 → 导出材料"
        in quick_start
    )
    for text in ("uv sync", "uv run csbox", "开始实验", "F12", "exit", "实验记录"):
        assert text in quick_start


def test_readme_does_not_claim_index_installation() -> None:
    readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")

    assert "pipx install csbox" not in readme
    assert "uv tool install csbox" not in readme


def test_readme_console_script_matches_metadata() -> None:
    readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
    pyproject = (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")

    assert 'csbox = "csbox.cli:main"' in pyproject
    assert "csbox --help" in readme


def test_readme_describes_the_complete_v05_delivery_surface() -> None:
    readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")

    for surface in (
        "Lab Evidence",
        "API Evidence",
        "Project Check",
        "Safe Pack",
        "Evidence Collection",
        "Report Handoff",
        "Course Report Formatting",
    ):
        assert surface in readme
    assert "不生成实验分析、结论、答案" in readme
