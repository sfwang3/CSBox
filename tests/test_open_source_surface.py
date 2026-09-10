from __future__ import annotations

from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ISSUE_TEMPLATE_ROOT = PROJECT_ROOT / ".github" / "ISSUE_TEMPLATE"


def test_contributor_entrypoint_is_present_and_linked_from_both_readmes() -> None:
    guide = PROJECT_ROOT / "CONTRIBUTING.md"

    assert guide.is_file()
    guide_text = guide.read_text(encoding="utf-8")
    guide_lower = guide_text.lower()
    for phrase in (
        "bug reports",
        "feature requests",
        "development setup",
        "repository layout",
        "targeted",
        "ruff",
        "display width",
        "windows",
        "linux",
        "pull requests",
        "security reporting",
        "academic",
    ):
        assert phrase in guide_lower

    for readme_name in ("README.md", "README.en.md"):
        readme = (PROJECT_ROOT / readme_name).read_text(encoding="utf-8")
        assert "CONTRIBUTING.md" in readme


def test_issue_forms_are_valid_yaml_and_request_reproducible_safe_reports() -> None:
    expected_files = {"bug_report.yml", "feature_request.yml", "config.yml"}

    assert {path.name for path in ISSUE_TEMPLATE_ROOT.iterdir()} == expected_files
    parsed = {
        path.name: yaml.safe_load(path.read_text(encoding="utf-8"))
        for path in ISSUE_TEMPLATE_ROOT.iterdir()
    }

    for name in ("bug_report.yml", "feature_request.yml"):
        form = parsed[name]
        assert isinstance(form, dict)
        assert form["name"]
        assert form["description"]
        assert isinstance(form["body"], list)

    bug_text = ISSUE_TEMPLATE_ROOT.joinpath("bug_report.yml").read_text(encoding="utf-8")
    feature_text = ISSUE_TEMPLATE_ROOT.joinpath("feature_request.yml").read_text(encoding="utf-8")
    assert "password" in bug_text.lower()
    assert "api token" in bug_text.lower()
    assert "private key" in bug_text.lower()
    assert "real user problem" in feature_text.lower()
    assert "current workaround" in feature_text.lower()
    assert "why it belongs in csbox" in feature_text.lower()
    assert "sk-" not in (bug_text + feature_text)
    assert "ghp_" not in (bug_text + feature_text)
    assert "BEGIN PRIVATE KEY" not in (bug_text + feature_text)


def test_pull_request_template_stays_compact_and_covers_release_invariants() -> None:
    template = (PROJECT_ROOT / ".github" / "PULL_REQUEST_TEMPLATE.md").read_text(encoding="utf-8")

    for heading in ("What changed", "Why", "How tested", "Risk / affected area", "Checklist"):
        assert f"## {heading}" in template
    for checklist_item in (
        "targeted tests",
        "ruff",
        "display-width",
        "secrets",
        "public documentation was updated",
    ):
        assert checklist_item.lower() in template.lower()


def test_changelog_describes_maintenance_scope_and_current_release() -> None:
    changelog = (PROJECT_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    section = changelog.split("## 0.6.1", 1)[1].split("## 0.6.0", 1)[0]

    assert changelog.startswith("# Changelog\n\n## 0.6.1")
    section_lower = section.lower()
    for phrase in (
        "open-source project surface",
        "contribution workflow",
        "github metadata",
        "project urls",
        "release presentation",
        "housekeeping",
    ):
        assert phrase in section_lower
    assert "new product workflow" not in section.lower()
