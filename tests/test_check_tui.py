from __future__ import annotations

from pathlib import Path

import pytest

from csbox.check.models import CheckFinding, CheckReport, CheckStatus, DetectedProject
from csbox.locales import load_locale
from csbox.tui.app import CheckApp
from csbox.tui.screens.project_check import ProjectCheckScreen


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
