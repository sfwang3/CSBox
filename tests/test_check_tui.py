from __future__ import annotations

from pathlib import Path

import pytest

from csbox.check.models import CheckFinding, CheckReport, CheckStatus, DetectedProject
from csbox.locales import load_locale
from csbox.tui.app import CheckApp
from csbox.tui.screens.project_check import ProjectCheckScreen
from csbox.tui.widgets.project_check import CheckFindings


def report(tmp_path: Path) -> CheckReport:
    return CheckReport(
        root=tmp_path,
        projects=(DetectedProject(kind="python", root=tmp_path, marker="pyproject.toml"),),
        findings=(
            CheckFinding(rule_id="readme", status=CheckStatus.PASS, message="README 已找到"),
        ),
    )


@pytest.mark.asyncio
async def test_check_screen_is_usable_at_80_and_120_columns(tmp_path: Path) -> None:
    app = CheckApp(report=report(tmp_path), locale=load_locale())

    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        assert isinstance(app.screen, ProjectCheckScreen)
        assert app.screen.is_wide is False

    wide_app = CheckApp(report=report(tmp_path), locale=load_locale())
    async with wide_app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        assert wide_app.screen.is_wide is True


@pytest.mark.asyncio
async def test_check_screen_renders_project_relative_locations(tmp_path: Path) -> None:
    module = tmp_path / "模块" / "子模块"
    module.mkdir(parents=True)
    report_with_nested = CheckReport(
        root=tmp_path,
        projects=(
            DetectedProject(kind="python", root=tmp_path, marker="pyproject.toml"),
            DetectedProject(kind="maven", root=module, marker="pom.xml"),
        ),
        findings=(
            CheckFinding(
                rule_id="readme",
                status=CheckStatus.PASS,
                message="README 已找到",
                path=Path("说明.md"),
            ),
        ),
    )

    app = CheckApp(report=report_with_nested, locale=load_locale())
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        projects_text = str(app.screen.query_one("#check-projects", CheckFindings).renderable)
        findings_text = str(app.screen.query_one("#check-findings", CheckFindings).renderable)

    assert str(tmp_path) not in projects_text
    assert "模块/子模块" in projects_text
    assert str(tmp_path) not in findings_text


@pytest.mark.asyncio
async def test_check_screen_renders_deep_scan_finding(tmp_path: Path) -> None:
    report_with_deep_scan = CheckReport(
        root=tmp_path,
        deep_scan=CheckFinding(
            rule_id="deep-secret-scan",
            status=CheckStatus.FAIL,
            message="发现 secret，请立即轮换。",
            category="deep-secret-scan",
        ),
    )
    app = CheckApp(report=report_with_deep_scan, locale=load_locale())

    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        findings_text = str(app.screen.query_one("#check-findings", CheckFindings).renderable)

    assert "deep-secret-scan" in findings_text
    assert "发现 secret" not in findings_text
