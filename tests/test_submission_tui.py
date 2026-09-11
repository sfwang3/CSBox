from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from textual.screen import Screen
from textual.widgets import Button, Static

from csbox.check.models import CheckStatus
from csbox.core.models import EnvironmentSnapshot
from csbox.evidence.models import EvidenceSetSummary
from csbox.lab.home_data import RealHomeDataSource
from csbox.lab.repository import SessionRepository
from csbox.locales import load_locale
from csbox.submission.models import SubmissionReadiness
from csbox.tui.app import CSBoxApp
from csbox.tui.screens.home import HomeScreen
from csbox.tui.screens.submission import SubmissionScreen
from tui_harness import (
    focus_and_press,
    wait_for_busy,
    wait_for_rendered,
    wait_for_screen,
    wait_for_widget,
)


def _environment() -> EnvironmentSnapshot:
    return EnvironmentSnapshot(
        os_name="Linux",
        os_version="test",
        python_version="3.12",
        shell="Bash",
        shell_executable="bash",
        powershell_51_available=False,
        powershell_7_available=False,
        is_wsl=False,
        terminal_columns=80,
        terminal_rows=24,
    )


class _Repository:
    def list_summaries(self):
        return (EvidenceSetSummary(Path("set.json"), "set-1", "中文实验集", 3, None, True),)


class _Service:
    def __init__(
        self,
        *,
        readiness=SubmissionReadiness.WARNING,
        source_available=True,
        pack_blocked=False,
    ):
        self.readiness = readiness
        self.source_available = source_available
        self.pack_blocked = pack_blocked
        self.prepare_calls = 0
        self.prepare_force_calls: list[bool] = []
        self.verify_calls = 0
        self.phases: list[str] = []

    def plan(self, evidence_set_id: str):
        return SimpleNamespace(
            evidence_set_id=evidence_set_id,
            readiness=self.readiness,
            check=SimpleNamespace(status=CheckStatus.WARN, warning_count=1),
            warnings=("项目包含提示，请确认。",),
            blockers=("请处理检查项目。",) if self.readiness is SubmissionReadiness.BLOCKED else (),
            destination=Path("/tmp/提交材料"),
            report_filename="report.docx",
            archive_filename="student-project.zip",
            project_archive=SimpleNamespace(verified=True, contains_manifest=True),
            pack_plan=SimpleNamespace(
                blockers=("private/path.txt",) if self.pack_blocked else (),
                rejected=("private/path.txt",) if self.pack_blocked else (),
                verification_requested=True,
            ),
            resolved_sources=(
                SimpleNamespace(
                    index=1,
                    source_type="lab_capture",
                    available=self.source_available,
                    unavailable_reason="来源暂不可用" if not self.source_available else None,
                ),
            ),
        )

    def prepare(self, plan, *, force=False, phase_callback=None):
        self.prepare_calls += 1
        self.prepare_force_calls.append(force)
        for phase in ("checking", "reporting", "packing", "verifying", "publishing", "complete"):
            self.phases.append(phase)
            if phase_callback:
                phase_callback(phase)
        return SimpleNamespace(
            destination=Path("/tmp/提交材料"),
            report_path=Path("/tmp/提交材料/report.docx"),
            archive_path=Path("/tmp/提交材料/student-project.zip"),
            manifest_path=Path("/tmp/提交材料/submission-manifest.json"),
            check_status=CheckStatus.WARN,
            warning_count=1,
            verified=True,
            warnings=("请按课程要求手动上传。",),
        )


class _Verifier:
    def verify(self, directory):
        return SimpleNamespace(status="PASS", verified=True, warning_count=0, files=(), errors=())


def _app(tmp_path: Path, service: _Service, *, report_profile_screen_factory=None) -> CSBoxApp:
    return CSBoxApp(
        data_source=RealHomeDataSource(
            SessionRepository(tmp_path / ".csbox" / "sessions"), tmp_path
        ),
        environment=_environment(),
        locale=load_locale(),
        evidence_repository=_Repository(),
        submission_service=service,
        submission_verifier_factory=_Verifier,
        report_profile_screen_factory=report_profile_screen_factory,
    )


@pytest.mark.asyncio
async def test_submission_flow_selects_preflights_prepares_and_verifies(tmp_path: Path) -> None:
    service = _Service()
    app = _app(tmp_path, service)
    async with app.run_test(size=(80, 24)) as pilot:
        assert isinstance(app.screen, HomeScreen)
        assert "准备提交" in " ".join(str(button.label) for button in app.screen.query(Button))
        app.screen.query_one("#entry-submit", Button).focus()
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, SubmissionScreen)
        await pilot.press("enter")
        await pilot.pause()
        assert "证据来源" in _text(app.screen)
        assert "[WARN]" in _text(app.screen)
        await pilot.press("enter")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert "提交材料已准备" in _text(app.screen)
        assert "report.docx" in _text(app.screen)
        assert "submission-manifest.json" in _text(app.screen)
        await pilot.press("v")
        await pilot.pause()
        assert service.prepare_calls == 1
        assert service.prepare_force_calls == [True]
        assert isinstance(app.screen, SubmissionScreen)


@pytest.mark.asyncio
@pytest.mark.parametrize("size", ((80, 24), (100, 30), (120, 35), (160, 45)))
async def test_submission_screen_mounts_at_supported_viewports(tmp_path: Path, size) -> None:
    app = _app(tmp_path, _Service())
    async with app.run_test(size=size) as pilot:
        app.screen.query_one("#entry-submit", Button).focus()
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, SubmissionScreen)
        assert app.screen.query_one("#submission-scroll")


def _text(screen) -> str:
    return "\n".join(str(widget.renderable) for widget in screen.query(Static))


async def _open_submission_preflight(app: CSBoxApp, pilot: object) -> SubmissionScreen:
    await focus_and_press(pilot, app, "#entry-submit")
    screen = await wait_for_screen(pilot, app, SubmissionScreen)
    await focus_and_press(pilot, app, "#submission-confirm")
    await wait_for_busy(pilot, screen, False)
    await wait_for_rendered(pilot, screen, "#submission-content")
    return screen


@pytest.mark.asyncio
async def test_submission_preflight_fails_unavailable_evidence_source(tmp_path: Path) -> None:
    app = _app(tmp_path, _Service(source_available=False))
    async with app.run_test(size=(80, 24)) as pilot:
        screen = await _open_submission_preflight(app, pilot)
        rendered = _text(screen)
        assert "[FAIL] 证据来源" in rendered
        assert "证据来源不可用，请返回“整理证据”修复或移除。" in rendered


@pytest.mark.asyncio
async def test_submission_preflight_fails_pack_blocker_without_sensitive_detail(
    tmp_path: Path,
) -> None:
    app = _app(tmp_path, _Service(pack_blocked=True))
    async with app.run_test(size=(80, 24)) as pilot:
        screen = await _open_submission_preflight(app, pilot)
        rendered = _text(screen)
        assert "[FAIL] 项目打包" in rendered
        assert "项目打包存在阻塞项" in rendered
        assert "private/path.txt" not in rendered
        assert app.screen.query_one("#submission-confirm", Button).disabled


@pytest.mark.asyncio
async def test_submission_result_lists_final_names_and_destination_without_staging_path(
    tmp_path: Path,
) -> None:
    service = _Service()
    app = _app(tmp_path, service)
    async with app.run_test(size=(80, 24)) as pilot:
        screen = await _open_submission_preflight(app, pilot)
        await focus_and_press(pilot, app, "#submission-confirm")
        await wait_for_busy(pilot, screen, False)
        await wait_for_rendered(pilot, screen, "#submission-content")
        rendered = _text(screen)
        assert "提交材料已准备" in rendered
        assert "report.docx" in rendered
        assert "student-project.zip" in rendered
        assert "submission-manifest.json" in rendered
        assert "输出位置" in rendered
        assert "/tmp/提交材料/report.docx" not in rendered
        assert "手动上传" in rendered
        assert "不会替你提交" in rendered


@pytest.mark.asyncio
async def test_submission_preflight_can_open_existing_report_profile_editor(
    tmp_path: Path,
) -> None:
    opened: list[str] = []

    def profile_factory(evidence_set_id: str) -> Screen[None]:
        opened.append(evidence_set_id)
        return Screen(name="existing-report-profile-editor")

    app = _app(tmp_path, _Service(), report_profile_screen_factory=profile_factory)
    async with app.run_test(size=(80, 24)) as pilot:
        screen = await _open_submission_preflight(app, pilot)
        adjust = await wait_for_widget(pilot, screen, "#submission-adjust-report")
        adjust.focus()
        await focus_and_press(pilot, app, "#submission-adjust-report")
        await wait_for_screen(pilot, app, "existing-report-profile-editor")
        assert opened == ["set-1"]
