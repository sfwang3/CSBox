from __future__ import annotations

from pathlib import Path

import pytest
from textual.widgets import Static

from csbox.check.models import CheckFinding, CheckReport, CheckStatus
from csbox.check.service import CheckService
from csbox.cli.main import _check_json_payload, _print_check_plain
from csbox.config import CheckConfig, CSBoxConfig
from csbox.locales import load_locale
from csbox.tui.app import CheckApp
from csbox.tui.widgets.project_check import CheckFindings


def test_core_actionable_findings_explain_reason_and_next_step(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text('TOKEN="local-only"\n', encoding="utf-8")
    (tmp_path / "private.pem").write_text(
        "-----BEGIN OPENSSH PRIVATE KEY-----\nsanitized fixture only\n",
        encoding="utf-8",
    )
    (tmp_path / "build").mkdir()
    (tmp_path / "notes.txt").write_text(
        "Windows C:\\Users\\student\\project\\main.py\n",
        encoding="utf-8",
    )
    (tmp_path / "settings.py").write_text(
        'token = "sk_live_51N3aB7xQ2mL9pR4vC8dE0fG"\n',
        encoding="utf-8",
    )
    (tmp_path / "large.bin").write_bytes(b"x" * (1024 * 1024 + 1))

    report = CheckService(config=CSBoxConfig(check=CheckConfig(large_file_threshold_mb=1))).run(
        tmp_path
    )

    expected_categories = {
        "README",
        "env",
        "private-key",
        "artifact",
        "large-file",
        "windows-absolute-path",
        "hard-coded-secret",
    }
    findings = {
        finding.category: finding
        for finding in report.findings
        if finding.category in expected_categories
        and finding.status in {CheckStatus.WARN, CheckStatus.FAIL}
    }

    assert expected_categories <= findings.keys()
    assert all("为什么：" in finding.message for finding in findings.values())
    assert all("建议：" in finding.message for finding in findings.values())
    assert findings["env"].status is CheckStatus.FAIL
    assert findings["hard-coded-secret"].status is CheckStatus.FAIL
    assert findings["large-file"].status is CheckStatus.WARN


def test_sensitive_human_cli_output_is_actionable_without_secret_content(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    report = CheckReport(
        root=tmp_path,
        findings=(
            CheckFinding(
                rule_id="hard-coded-secret",
                status=CheckStatus.FAIL,
                message="不应直接输出的 fixture secret",
                path=Path("中文/配置.py"),
                line=3,
                category="hard-coded-secret",
            ),
        ),
    )

    _print_check_plain(report)
    output = capsys.readouterr().out

    assert "hard-coded-secret" in output
    assert "为什么：" in output
    assert "建议：" in output
    assert "不应直接输出的 fixture secret" not in output


def test_sensitive_machine_output_keeps_safe_projection_and_schema_shape(
    tmp_path: Path,
) -> None:
    report = CheckReport(
        root=tmp_path,
        findings=(
            CheckFinding(
                rule_id="hard-coded-secret",
                status=CheckStatus.FAIL,
                message="safe internal message",
                path=Path("settings.py"),
                line=4,
                category="hard-coded-secret",
            ),
        ),
    )

    payload = _check_json_payload(report)

    assert set(payload["findings"][0]) == {"path", "line", "category"}
    assert "remediation" not in payload["findings"][0]


@pytest.mark.asyncio
async def test_check_screen_shows_safe_actionable_sensitive_finding_and_location(
    tmp_path: Path,
) -> None:
    sentinel = "CSBOX_SECRET_SENTINEL_actionable"
    report = CheckReport(
        root=tmp_path,
        findings=(
            CheckFinding(
                rule_id="hard-coded-secret",
                status=CheckStatus.FAIL,
                message=f"发现 {sentinel}",
                path=Path("中文") / "配置.py",
                line=3,
                category="hard-coded-secret",
            ),
        ),
    )
    app = CheckApp(report=report, locale=load_locale())

    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        text = str(app.screen.query_one("#check-narrow", CheckFindings).renderable)

    assert "hard-coded-secret" in text
    assert "中文/配置.py:3" in text
    assert "为什么：" in text
    assert "建议：" in text
    assert sentinel not in text


@pytest.mark.asyncio
async def test_check_screen_has_a_clear_empty_state_when_no_action_is_needed(
    tmp_path: Path,
) -> None:
    report = CheckReport(
        root=tmp_path,
        findings=(
            CheckFinding(
                rule_id="readme",
                status=CheckStatus.PASS,
                message="README 已找到。",
                category="README",
            ),
        ),
    )
    app = CheckApp(report=report, locale=load_locale())

    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        text = str(app.screen.query_one("#check-narrow", CheckFindings).renderable)

    assert "检查完成" in text
    assert "未发现需要处理的问题" in text


def _assert_static_lines_fit(screen: object) -> None:
    for widget in screen.query(Static):  # type: ignore[union-attr]
        if not widget.visible or widget.size.width <= 0:
            continue
        from csbox.core.display_width import display_width

        assert all(
            display_width(line) <= widget.content_region.width
            for line in str(widget.renderable).splitlines()
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("size", ((80, 24), (100, 30), (120, 35), (160, 45)))
async def test_actionable_cjk_finding_fits_all_supported_check_viewports(
    tmp_path: Path,
    size: tuple[int, int],
) -> None:
    report = CheckReport(
        root=tmp_path,
        findings=(
            CheckFinding(
                rule_id="env-file",
                status=CheckStatus.FAIL,
                message="原始 message 不用于敏感内容展示",
                path=Path("中文课程项目") / ".env",
                category="env",
            ),
        ),
    )
    app = CheckApp(report=report, locale=load_locale())

    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        _assert_static_lines_fit(app.screen)
        text = str(app.screen.query_one("#check-narrow", CheckFindings).renderable)

    assert "为什么：" in text
    assert "建议：" in text
