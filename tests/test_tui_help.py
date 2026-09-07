from __future__ import annotations

import html
import json
import re
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from time import monotonic
from types import SimpleNamespace

import pytest
from textual.containers import VerticalScroll
from textual.widget import Widget
from textual.widgets import Button, Input, Static, TextArea

from csbox.api.errors import ApiPersistenceError
from csbox.api.exporter import ApiExportResult
from csbox.api.repository import ApiRunRepository
from csbox.api.scenario import ScenarioLoader
from csbox.core.display_width import display_width
from csbox.core.events import TerminalSize
from csbox.core.models import EnvironmentSnapshot, HomeSnapshot
from csbox.lab.exporter import LabExportResult
from csbox.lab.fake_data import FakeHomeDataSource
from csbox.lab.repository import SessionRepository
from csbox.locales import load_locale
from csbox.report.models import ReportProfile
from csbox.tui.app import CSBoxApp, ReviewApp
from csbox.tui.dialogs.api import ApiExportRequest, ApiQuickCreateDialog
from csbox.tui.dialogs.lab_start import LabStartDialog
from csbox.tui.dialogs.report import ReportProfileDialog
from csbox.tui.help import HelpDialog
from csbox.tui.screens.api import ApiScreen
from csbox.tui.screens.home import HomeScreen
from csbox.tui.screens.records import ExportRequest, RecordsScreen
from csbox.tui.screens.review import ReviewController, ReviewScreen

LAYOUTS = ((80, 24), (100, 30), (120, 35), (160, 45))


async def _wait_until(pilot: object, predicate, *, timeout: float = 5.0) -> None:
    deadline = monotonic() + timeout
    while monotonic() < deadline:
        if predicate():
            return
        await pilot.pause()
    assert predicate()


def _environment() -> EnvironmentSnapshot:
    return EnvironmentSnapshot(
        os_name="Linux",
        os_version="test",
        python_version="3.12.3",
        shell="Bash",
        shell_executable="bash",
        powershell_51_available=False,
        powershell_7_available=False,
        is_wsl=False,
        terminal_columns=80,
        terminal_rows=24,
    )


class LongPathHomeDataSource:
    source_id = "long-path-help-test"

    def __init__(self, project_dir: Path) -> None:
        self.project_dir = project_dir

    def get_home_snapshot(self, environment: EnvironmentSnapshot) -> HomeSnapshot:
        return HomeSnapshot(environment=environment, project_dir=self.project_dir)


def _app(*, project_dir: Path | None = None) -> CSBoxApp:
    data_source = (
        FakeHomeDataSource() if project_dir is None else LongPathHomeDataSource(project_dir)
    )
    return CSBoxApp(
        data_source=data_source,
        environment=_environment(),
        locale=load_locale(),
    )


def _screen_text(screen: Widget) -> str:
    return "\n".join(
        [
            *(str(widget.renderable) for widget in screen.query(Static)),
            *(str(button.label) for button in screen.query(Button)),
            *(input_widget.value for input_widget in screen.query(Input)),
        ]
    )


def _screenshot_text(app: CSBoxApp) -> str:
    without_tags = re.sub(r"<[^>]+>", "", app.export_screenshot())
    return "".join(html.unescape(without_tags).splitlines()).replace("\xa0", " ")


def _assert_static_lines_fit(screen: Widget) -> None:
    for widget in screen.query(Static):
        if not widget.visible or widget.size.width <= 0:
            continue
        for line in str(widget.renderable).splitlines():
            assert display_width(line) <= widget.content_region.width, (
                f"{widget!r} rendered {display_width(line)} cells "
                f"in {widget.content_region.width} cells"
            )


def _review_session(tmp_path: Path):
    from csbox.lab.models import SessionPaths

    paths = SessionPaths(tmp_path / "review-session")
    paths.root.mkdir(parents=True, exist_ok=True)
    paths.cast.write_text(
        "\n".join(
            (
                json.dumps({"version": 3, "term": {"cols": 12, "rows": 3}}),
                json.dumps([1.0, "o", "hello\n中文实验"]),
                json.dumps([1.0, "x", "0"]),
            )
        )
        + "\n",
        encoding="utf-8",
    )
    paths.metadata.write_text(
        json.dumps({"id": "review-session", "name": "中文回看", "status": "completed"}),
        encoding="utf-8",
    )
    return paths


@pytest.mark.asyncio
async def test_home_explains_the_six_student_tasks_and_discoverable_help() -> None:
    app = _app()

    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()

        assert isinstance(app.screen, HomeScreen)
        assert [button.id for button in app.screen.query(Button)] == [
            "entry-start",
            "entry-records",
            "entry-evidence",
            "entry-check",
            "entry-pack",
            "entry-api",
        ]
        text = _screen_text(app.screen)
        assert "开始实验" in text and "记录终端过程" in text
        assert "实验记录" in text and "回看实验过程" in text
        assert "整理证据" in text and "报告材料" in text
        assert "检查项目" in text and "交作业前检查" in text
        assert "安全打包" in text and "生成 ZIP" in text
        assert "API 实验" in text and "接口请求" in text
        assert "第一次使用" in text
        assert "? 帮助" in text


@pytest.mark.asyncio
async def test_home_question_mark_opens_scrollable_beginner_help_and_restores_focus() -> None:
    app = _app()

    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        home = app.screen
        home.query_one("#entry-records", Button).focus()
        await pilot.pause()
        original_focus = home.focused

        await pilot.press("?")
        await pilot.pause()

        assert app.screen.__class__.__name__ == "HelpDialog"
        text = _screen_text(app.screen)
        assert "CSBox 能帮我做什么？" in text
        assert "正在做上机实验" in text
        assert "安全打包" in text
        assert "关键画面（Capture）" in text
        assert "不会写实验结论" in text
        assert app.screen.query_one("#help-scroll", VerticalScroll)
        assert app.screen.query_one("#help-close", Button).visible

        scroll = app.screen.query_one("#help-scroll", VerticalScroll)
        assert scroll.max_scroll_y > 0
        before = scroll.scroll_y
        await pilot.press("pagedown")
        await pilot.pause()
        assert scroll.scroll_y > before

        await pilot.press("escape")
        await pilot.pause()
        assert app.screen is home
        assert app.screen.focused is original_focus


@pytest.mark.asyncio
async def test_help_backdrop_hides_underlying_panel_borders() -> None:
    app = _app()

    async with app.run_test(size=(120, 35)) as pilot:
        await pilot.pause()
        await pilot.press("?")
        await pilot.pause()

        assert app.screen.styles.background.a == 1
        layout = app.screen._compositor.render_update(
            full=True,
            screen_stack=app._background_screens,
            simplify=False,
        )
        assert all("─" not in strip.text[98:118] for strip in layout.strips)


@pytest.mark.asyncio
async def test_f1_is_a_help_alias_and_contextual_help_explains_lab_records() -> None:
    app = _app()

    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        await pilot.press("f1")
        await pilot.pause()
        assert app.screen.__class__.__name__ == "HelpDialog"
        assert "第一次使用" in _screen_text(app.screen)

        await pilot.press("escape")
        await pilot.pause()
        app.screen.query_one("#entry-records", Button).focus()
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, RecordsScreen)

        await pilot.press("?")
        await pilot.pause()
        text = _screen_text(app.screen)
        assert "这个页面是做什么的？" in text
        assert "我现在最常做什么？" in text
        assert "主要按键/操作是什么？" in text
        assert "完成后会得到什么？" in text
        assert "怎么返回？" in text
        assert "回看" in text
        assert "关键画面" in text
        assert "Esc/Q" in text


@pytest.mark.asyncio
async def test_beginner_keyboard_journey_finds_start_review_check_and_pack() -> None:
    app = _app()

    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        assert isinstance(app.screen, HomeScreen)
        home = app.screen

        # Help is discoverable from the first focused Home entry.
        assert home.focused is home.query_one("#entry-start", Button)
        await pilot.press("?")
        await pilot.pause()
        assert isinstance(app.screen, HelpDialog)
        assert "正在做上机实验" in _screen_text(app.screen)
        await pilot.press("escape")
        await pilot.pause()
        assert app.screen is home
        assert home.focused is home.query_one("#entry-start", Button)

        # Start Lab is the first actionable workflow and remains cancellable.
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, LabStartDialog)
        await pilot.press("escape")
        await pilot.pause()
        assert app.screen is home

        # Keyboard navigation exposes Records/Review, then its contextual Help.
        await pilot.press("down")
        await pilot.pause()
        assert home.focused is home.query_one("#entry-records", Button)
        await pilot.press("enter")
        await pilot.pause()
        records = app.screen
        assert isinstance(records, RecordsScreen)
        await pilot.press("f1")
        await pilot.pause()
        assert isinstance(app.screen, HelpDialog)
        assert "回看" in _screen_text(app.screen)
        await pilot.press("escape")
        await pilot.pause()
        assert app.screen is records
        await pilot.press("escape")
        await pilot.pause()
        assert app.screen is home

        # Continue down the same Home list to the delivery workflows.
        for entry_id in ("entry-evidence", "entry-check", "entry-pack"):
            await pilot.press("down")
            await pilot.pause()
            assert home.focused is home.query_one(f"#{entry_id}", Button)


@pytest.mark.asyncio
async def test_question_mark_is_text_in_input_but_f1_opens_help_and_restores_input_focus() -> None:
    app = _app()

    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, LabStartDialog)
        name_input = app.screen.query_one("#lab-start-name", Input)
        assert app.screen.focused is name_input

        await pilot.press("?")
        assert name_input.value == "?"
        await pilot.press("f1")
        await pilot.pause()
        assert app.screen.__class__.__name__ == "HelpDialog"
        await pilot.press("escape")
        await pilot.pause()
        assert isinstance(app.screen, LabStartDialog)
        assert app.screen.query_one("#lab-start-name", Input).value == "?"
        assert app.screen.focused is name_input


@pytest.mark.asyncio
async def test_question_mark_is_text_in_report_textarea_but_f1_opens_help() -> None:
    app = _app()

    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        profile_dialog = ReportProfileDialog(
            locale=load_locale(),
            profile=ReportProfile.default(),
        )
        app.push_screen(profile_dialog)
        await pilot.pause()
        body = profile_dialog.query_one("#report-profile-section-1-body-input", TextArea)
        body.focus()

        await pilot.press("?")
        assert body.text == "?"
        await pilot.press("f1")
        await pilot.pause()
        assert app.screen.__class__.__name__ == "HelpDialog"
        await pilot.press("escape")
        await pilot.pause()
        assert app.screen is profile_dialog
        assert body.text == "?"
        assert profile_dialog.focused is body


@pytest.mark.asyncio
async def test_f1_works_in_api_quick_create_while_question_mark_remains_text() -> None:
    app = _app()

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        dialog = ApiQuickCreateDialog(locale=load_locale())
        app.push_screen(dialog)
        await _wait_until(
            pilot,
            lambda: bool(tuple(dialog.query("#api-quick-create-name"))),
        )
        name_input = dialog.query_one("#api-quick-create-name", Input)
        await _wait_until(pilot, lambda: dialog.focused is name_input)

        await pilot.press("?")
        assert name_input.value == "?"
        await pilot.press("f1")
        await pilot.pause()
        assert isinstance(app.screen, HelpDialog)
        await pilot.press("escape")
        await _wait_until(
            pilot,
            lambda: app.screen is dialog and dialog.focused is name_input,
        )
        assert name_input.value == "?"


@pytest.mark.asyncio
async def test_review_help_preserves_controller_position_and_capture_selection(
    tmp_path: Path,
) -> None:
    controller = ReviewController.from_session(_review_session(tmp_path))
    controller.seek(0.6)
    capture = controller.create_capture("关键画面")
    controller.selected_capture = 0
    original_time = controller.current_time
    original_capture_id = capture.capture_id
    app = ReviewApp(controller=controller, locale=load_locale())

    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        review = app.screen
        assert isinstance(review, ReviewScreen)
        await pilot.press("?")
        await pilot.pause()
        assert isinstance(app.screen, HelpDialog)
        await pilot.press("escape")
        await pilot.pause()

        assert app.screen is review
        assert controller.current_time == original_time
        assert controller.selected_capture == 0
        assert controller.captures[0].capture_id == original_capture_id


@pytest.mark.asyncio
async def test_review_help_freezes_and_restores_playback_state(tmp_path: Path) -> None:
    controller = ReviewController.from_session(_review_session(tmp_path))
    controller.seek(0.2)
    controller.play()
    app = ReviewApp(controller=controller, locale=load_locale())

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        review = app.screen
        assert isinstance(review, ReviewScreen)
        await pilot.pause(0.25)
        assert controller.playing is True
        before_help = controller.current_time

        await pilot.press("?")
        await pilot.pause()
        assert isinstance(app.screen, HelpDialog)
        await pilot.pause(0.35)
        assert controller.current_time == pytest.approx(before_help)
        assert controller.playing is False

        await pilot.press("escape")
        await pilot.pause()
        assert app.screen is review
        assert controller.playing is True
        resumed_at = controller.current_time
        await pilot.pause(0.25)
        assert controller.current_time > resumed_at


@pytest.mark.asyncio
@pytest.mark.parametrize("size", LAYOUTS)
async def test_review_footer_keeps_high_frequency_actions_visible_at_all_sizes(
    size: tuple[int, int], tmp_path: Path
) -> None:
    app = ReviewApp(
        controller=ReviewController.from_session(_review_session(tmp_path)),
        locale=load_locale(),
    )

    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        assert isinstance(app.screen, ReviewScreen)
        footer = str(app.screen.query_one("#review-footer", Static).renderable)
        for hint in (
            "↑↓ 选择关键画面",
            "←→ 定位",
            "Space 播放/暂停",
            "C 补关键画面",
            "? 帮助",
            "Q 返回",
        ):
            assert hint in footer
        if size == (160, 45):
            assert "E 修改标题" in footer
        assert (
            display_width(footer)
            <= app.screen.query_one("#review-footer", Static).content_region.width
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("size", LAYOUTS)
async def test_help_is_usable_at_all_supported_beginner_viewports(
    size: tuple[int, int], tmp_path: Path
) -> None:
    project_dir = tmp_path / ("课程实验" * 12 + " API 中心 e\u0301")
    app = _app(project_dir=project_dir)

    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        assert "? 帮助" in _screenshot_text(app)
        await pilot.press("?")
        await pilot.pause()

        assert app.screen.__class__.__name__ == "HelpDialog"
        assert app.screen.query_one("#help-scroll", VerticalScroll).visible
        assert app.screen.query_one("#help-close", Button).visible
        _assert_static_lines_fit(app.screen)
        body = app.screen.query_one("#help-body", Static)
        assert body.content_region.width > 0
        assert all(
            display_width(line) <= body.content_region.width
            for line in str(body.renderable).splitlines()
        )

        await pilot.press("escape")
        await pilot.pause()
        assert isinstance(app.screen, HomeScreen)


@pytest.mark.parametrize(
    ("context_name", "expected"),
    (
        ("HOME", "开始实验"),
        ("LAB_START", "实验名称"),
        ("RECORDS", "回看"),
        ("REVIEW", "关键画面"),
        ("EVIDENCE_LIST", "证据集"),
        ("EVIDENCE_EDITOR", "配置报告"),
        ("EVIDENCE_BROWSER", "选择关键画面"),
        ("REPORT_CONFIG", "用户填写"),
        ("REPORT_EXPORT", "输出目录"),
        ("CHECK", "检查项目"),
        ("PACK", "安全打包"),
        ("API", "API 实验"),
    ),
)
def test_each_context_help_has_the_five_beginner_answers(context_name: str, expected: str) -> None:
    from csbox.tui.help import HelpContext, help_text

    text = help_text(HelpContext[context_name], load_locale())
    assert expected in text
    for prompt in (
        "这个页面是做什么的？",
        "我现在最常做什么？",
        "主要按键/操作是什么？",
        "完成后会得到什么？",
        "怎么返回？",
    ):
        assert prompt in text
    assert "schema" not in text.lower()
    assert "registry" not in text.lower()


def test_review_help_names_all_supported_secondary_controls() -> None:
    from csbox.tui.help import HelpContext, help_text

    text = help_text(HelpContext.REVIEW, load_locale())
    for binding in (
        "J 跳转关键画面",
        "PageUp/PageDown 快速定位",
        "E 修改标题",
        "Delete 删除关键画面",
        "Tab 切换区域",
    ):
        assert binding in text


def _completed_export_session(tmp_path: Path) -> SessionRepository:
    repository = SessionRepository.from_cwd(tmp_path)
    started_at = datetime(2026, 9, 3, 8, 0, tzinfo=UTC)
    paths = repository.create_starting(
        "帮助期间导出",
        shell="bash",
        shell_version=None,
        size=TerminalSize(columns=80, rows=24),
        cwd=tmp_path,
        session_id="help-export",
        started_at=started_at,
    )
    paths.cast.write_text(
        '{"version":3,"term":{"cols":80,"rows":24}}\n',
        encoding="utf-8",
    )
    repository.finish(
        paths,
        "completed",
        ended_at=started_at + timedelta(minutes=1),
    )
    return repository


class _BlockingLabExport:
    def __init__(self, destination: Path) -> None:
        self.destination = destination
        self.started = threading.Event()
        self.release = threading.Event()

    def __call__(
        self,
        _session_id: str,
        _destination: Path,
        **_: object,
    ) -> LabExportResult:
        self.started.set()
        assert self.release.wait(5)
        return LabExportResult(
            destination=self.destination,
            evidence=(),
            markdown=self.destination / "evidence.md",
            cast=self.destination / "session.cast",
            commands=None,
        )


class _BlockingApiExport:
    def __init__(self, destination: Path) -> None:
        self.destination = destination
        self.started = threading.Event()
        self.release = threading.Event()

    def export(self, *_: object, **__: object) -> ApiExportResult:
        self.started.set()
        assert self.release.wait(5)
        return ApiExportResult(
            destination=self.destination,
            evidence=(),
            markdown=self.destination / "api-evidence.md",
            results=self.destination / "results.json",
        )


class _FailingApiExport:
    def export(self, *_: object, **__: object) -> ApiExportResult:
        raise ApiPersistenceError(
            "发生了什么：API 证据导出失败。在哪里：测试导出。怎么处理：请重试。"
        )


def _api_run_stub() -> SimpleNamespace:
    return SimpleNamespace(
        id="api-run-help",
        scenario=SimpleNamespace(name="中文 API 场景"),
    )


def _api_screen(tmp_path: Path, exporter_factory: object) -> ApiScreen:
    return ApiScreen(
        repository=ApiRunRepository.from_cwd(tmp_path),
        scenario_loader=ScenarioLoader(),
        runner_factory=lambda: None,
        locale=load_locale(),
        exporter_factory=exporter_factory,  # type: ignore[arg-type]
    )


async def _wait_for_event(pilot: object, event: threading.Event) -> None:
    for _ in range(100):
        if event.is_set():
            return
        await pilot.pause()
    assert event.is_set()


@pytest.mark.asyncio
async def test_help_is_blocked_while_records_export_is_running(tmp_path: Path) -> None:
    repository = _completed_export_session(tmp_path)
    export = _BlockingLabExport(tmp_path / "exported")
    app = CSBoxApp(
        data_source=FakeHomeDataSource(),
        environment=_environment(),
        locale=load_locale(),
        session_repository=repository,
        export_action=export,
    )

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        app.screen.query_one("#entry-records", Button).focus()
        await pilot.press("enter")
        await pilot.pause()
        records = app.screen
        assert isinstance(records, RecordsScreen)
        records._handle_export_request(  # type: ignore[attr-defined]
            ExportRequest(
                session_id="help-export",
                destination=tmp_path / "exported",
            )
        )
        await _wait_for_event(pilot, export.started)
        assert records._exporting is True  # type: ignore[attr-defined]

        await pilot.press("?")
        await pilot.pause()
        assert app.screen is records

        export.release.set()
        for _ in range(100):
            if app.screen.name == "export-result":
                break
            await pilot.pause()
        assert app.screen.name == "export-result"


@pytest.mark.asyncio
async def test_export_result_waits_for_help_to_close(tmp_path: Path) -> None:
    repository = _completed_export_session(tmp_path)
    export = _BlockingLabExport(tmp_path / "exported")
    app = CSBoxApp(
        data_source=FakeHomeDataSource(),
        environment=_environment(),
        locale=load_locale(),
        session_repository=repository,
        export_action=export,
    )

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        app.screen.query_one("#entry-records", Button).focus()
        await pilot.press("enter")
        await pilot.pause()
        records = app.screen
        assert isinstance(records, RecordsScreen)
        await pilot.press("?")
        await pilot.pause()
        assert isinstance(app.screen, HelpDialog)

        records._handle_export_request(  # type: ignore[attr-defined]
            ExportRequest(
                session_id="help-export",
                destination=tmp_path / "exported",
            )
        )
        await _wait_for_event(pilot, export.started)
        export.release.set()
        await pilot.pause(0.2)
        assert isinstance(app.screen, HelpDialog)

        await pilot.press("escape")
        await pilot.pause(0.2)
        assert app.screen.name == "export-result"


@pytest.mark.asyncio
async def test_api_help_is_blocked_before_export_worker_starts(tmp_path: Path) -> None:
    export = _BlockingApiExport(tmp_path / "api-exported")
    app = _app()
    api = _api_screen(tmp_path, lambda: export)

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        app.push_screen(api)
        await pilot.pause()
        api.selected_run = _api_run_stub()  # type: ignore[assignment]
        api._handle_export_request(  # type: ignore[attr-defined]
            ApiExportRequest(destination=tmp_path / "api-exported")
        )

        # This is the scheduling window before the coroutine body runs.
        app.action_show_help()
        assert app.screen is api

        await _wait_for_event(pilot, export.started)
        export.release.set()
        for _ in range(100):
            if app.screen.name == "api-export-result":
                break
            await pilot.pause()
        assert app.screen.name == "api-export-result"


@pytest.mark.asyncio
async def test_api_export_failure_waits_for_help_to_close(tmp_path: Path) -> None:
    app = _app()
    api = _api_screen(tmp_path, _FailingApiExport)

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        app.push_screen(api)
        await pilot.pause()
        api.selected_run = _api_run_stub()  # type: ignore[assignment]
        await pilot.press("?")
        await pilot.pause()
        assert isinstance(app.screen, HelpDialog)

        api._handle_export_request(  # type: ignore[attr-defined]
            ApiExportRequest(destination=tmp_path / "api-exported")
        )
        for _ in range(100):
            if not api.is_working:
                break
            await pilot.pause()
        assert isinstance(app.screen, HelpDialog)

        await pilot.press("escape")
        for _ in range(100):
            if app.screen.name == "api-export":
                break
            await pilot.pause()
        assert app.screen.name == "api-export"
