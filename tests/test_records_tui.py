from __future__ import annotations

import importlib
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from time import monotonic

import pytest
from textual.widget import Widget
from textual.widgets import Button, Input, Static

from csbox.core.display_width import display_width
from csbox.core.events import TerminalSize
from csbox.core.models import EnvironmentSnapshot
from csbox.lab.captures import CaptureStore
from csbox.lab.exporter import LabExporter, LabExportError, LabExportResult
from csbox.lab.home_data import RealHomeDataSource
from csbox.lab.models import SessionPaths
from csbox.lab.renderer import RenderTheme
from csbox.lab.repository import SessionRepository
from csbox.lab.screen import TerminalCell, TerminalCursor, TerminalSnapshot
from csbox.locales import load_locale
from csbox.tui.app import CSBoxApp
from csbox.tui.dialogs.capture_title import CaptureTitleDialog
from csbox.tui.dialogs.lab_start import LabStartDialog
from csbox.tui.lab_workflow import ShellOption
from csbox.tui.screens.home import HomeScreen

SECRET = "CSBOX_SECRET_SENTINEL_records"
LAYOUTS = ((80, 24), (100, 30), (120, 35), (160, 45))


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


def _snapshot(relative_time: float = 0.0) -> TerminalSnapshot:
    return TerminalSnapshot(
        rows=1,
        columns=6,
        cells=(
            (
                TerminalCell("中", width=2),
                TerminalCell("", width=0),
                TerminalCell("e\N{COMBINING ACUTE ACCENT}"),
                TerminalCell(" "),
                TerminalCell(" "),
                TerminalCell(" "),
            ),
        ),
        cursor=TerminalCursor(),
        relative_time=relative_time,
    )


def _create_session(
    repository: SessionRepository,
    *,
    session_id: str,
    name: str,
    started_at: datetime,
    status: str = "completed",
    shell: str = "bash",
    capture_count: int = 0,
    reason: str | None = None,
) -> SessionPaths:
    paths = repository.create_starting(
        name,
        shell=shell,
        shell_version=None,
        size=TerminalSize(columns=80, rows=24),
        cwd=repository.root.parent.parent,
        session_id=session_id,
        started_at=started_at,
    )
    paths.cast.write_text('{"version":3,"term":{"cols":80,"rows":24}}\n', encoding="utf-8")
    store = CaptureStore(paths.captures)
    for index in range(capture_count):
        store.create_capture(
            _snapshot(float(index)),
            timestamp=float(index),
            cwd=repository.root.parent.parent,
            title=f"Capture {index + 1}",
        )
    if status != "running":
        repository.finish(
            paths,
            status,
            reason=reason,
            ended_at=started_at + timedelta(minutes=5),
        )
    return paths


class CountingSessionRepository(SessionRepository):
    def __init__(self, sessions_root: Path | str) -> None:
        super().__init__(sessions_root)
        self.list_summary_calls = 0
        self.read_summary_calls: list[str] = []

    def list_summaries(self):
        self.list_summary_calls += 1
        return super().list_summaries()

    def read_summary(self, session_id: str):
        self.read_summary_calls.append(session_id)
        return super().read_summary(session_id)


def _app(
    project_dir: Path,
    repository: SessionRepository,
    *,
    export_action: object | None = None,
) -> CSBoxApp:
    return CSBoxApp(
        data_source=RealHomeDataSource(repository, project_dir),
        environment=_environment(),
        locale=load_locale(),
        session_repository=repository,
        shell_options=(
            ShellOption("bash", "Bash"),
            ShellOption("zsh", "Zsh"),
        ),
        export_action=export_action,  # type: ignore[arg-type]
    )


def _app_with_export_action(
    project_dir: Path,
    repository: SessionRepository,
    export_action: object,
) -> CSBoxApp:
    return _app(project_dir, repository, export_action=export_action)


def _screen_text(screen: Widget) -> str:
    return "\n".join(
        [
            *(str(widget.renderable) for widget in screen.query(Static)),
            *(str(button.label) for button in screen.query(Button)),
            *(widget.value for widget in screen.query(Input)),
        ]
    )


async def _open_records(app: CSBoxApp, pilot: object) -> object:
    app.screen.query_one("#entry-records", Button).focus()
    await pilot.press("enter")
    await pilot.pause()
    assert app.screen.name == "records"
    return app.screen


async def _wait_until(
    app: CSBoxApp,
    records: object,
    pilot: object,
    condition: Callable[[], bool],
    *,
    description: str,
    timeout: float = 5.0,
) -> None:
    """Wait for an observable export condition without guessing worker duration."""

    deadline = monotonic() + timeout
    while not condition():
        if monotonic() >= deadline:
            screen = app.screen
            workers = tuple(
                f"{worker.name}:{worker.state.name}"
                for worker in app.workers
                if worker.group == "lab-export"
            )
            result_state = (
                "failure"
                if getattr(screen, "failure_message", None) is not None
                else "success"
                if getattr(screen, "result", None) is not None
                else "none"
            )
            raise AssertionError(
                f"Timed out waiting for {description}: "
                f"screen={screen.name!r}, result={result_state!r}, "
                f"exporting={getattr(records, '_exporting', None)!r}, "
                f"selected={getattr(records, 'selected_session_id', None)!r}, "
                f"workers={workers!r}"
            )
        await pilot.pause()


async def _wait_for_export_result(app: CSBoxApp, records: object, pilot: object) -> None:
    await _wait_until(
        app,
        records,
        pilot,
        lambda: (
            app.screen.name == "export-result" and getattr(records, "_exporting", True) is False
        ),
        description="export result",
    )


def _is_rendered(widget: Widget) -> bool:
    current: Widget | None = widget
    while current is not None:
        if not current.display:
            return False
        current = current.parent
    return widget.visible


def _assert_static_lines_fit(screen: Widget) -> None:
    for widget in screen.query(Static):
        if not _is_rendered(widget) or widget.content_region.width <= 0:
            continue
        for line in str(widget.renderable).splitlines():
            assert display_width(line) <= widget.content_region.width


def _assert_buttons_inside_screen(screen: Widget, *button_ids: str) -> None:
    for button_id in button_ids:
        button = screen.query_one(f"#{button_id}", Button)
        assert button.display is True
        assert button.region.y >= 0
        assert button.region.bottom <= screen.size.height


def test_records_view_formatters_use_friendly_status_shell_and_local_time() -> None:
    records = importlib.import_module("csbox.tui.screens.records")

    assert records.status_display_name("completed") == "已完成"
    assert records.status_display_name("interrupted") == "已中断"
    assert records.status_display_name("failed") == "失败"
    assert records.status_display_name("running") == "录制中"
    assert records.shell_display_name("powershell_7") == "PowerShell 7"
    assert records.shell_display_name("powershell_51") == "Windows PowerShell 5.1"
    assert records.shell_display_name("bash") == "Bash"
    assert records.shell_display_name("zsh") == "Zsh"
    assert records.shell_display_name(r"C:\tools\fish.exe") == "fish.exe"
    assert records.shell_display_name("") == "旧记录 Shell"
    assert (
        records.format_local_time(
            datetime(2026, 8, 19, 10, 20, tzinfo=UTC),
            local_timezone=timezone(timedelta(hours=8)),
        )
        == "2026-08-19 18:20"
    )
    assert (
        records.format_local_time(
            datetime.max.replace(tzinfo=UTC),
            local_timezone=timezone(timedelta(hours=8)),
        )
        == "旧记录时间"
    )


@pytest.mark.asyncio
async def test_home_records_entry_opens_real_records_screen_and_returns_with_escape_or_q(
    tmp_path: Path,
) -> None:
    repository = SessionRepository.from_cwd(tmp_path)
    app = _app(tmp_path, repository)

    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        await _open_records(app, pilot)
        assert "下一阶段" not in _screen_text(app.screen)
        await pilot.press("escape")
        await pilot.pause()
        assert isinstance(app.screen, HomeScreen)

        await _open_records(app, pilot)
        await pilot.press("q")
        await pilot.pause()
        assert isinstance(app.screen, HomeScreen)


@pytest.mark.asyncio
async def test_records_empty_state_starts_the_existing_lab_workflow(tmp_path: Path) -> None:
    repository = SessionRepository.from_cwd(tmp_path)
    app = _app(tmp_path, repository)

    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        await _open_records(app, pilot)
        text = _screen_text(app.screen)
        assert "暂无实验记录" in text
        assert "完成一次实验后会显示在这里" in text
        assert app.screen.focused is not None
        assert app.screen.focused.id == "records-start"

        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, LabStartDialog)
        await pilot.press("escape")
        await pilot.pause()
        assert app.screen.name == "records"


@pytest.mark.asyncio
async def test_records_render_all_lifecycle_shell_and_capture_summaries(tmp_path: Path) -> None:
    repository = SessionRepository.from_cwd(tmp_path)
    base = datetime(2026, 8, 19, 8, 0, tzinfo=UTC)
    _create_session(
        repository,
        session_id="completed",
        name="网络实验三",
        started_at=base,
        status="completed",
        shell="powershell_7",
        capture_count=6,
    )
    _create_session(
        repository,
        session_id="interrupted",
        name="Linux 实验",
        started_at=base + timedelta(hours=1),
        status="interrupted",
        shell="bash",
        capture_count=3,
    )
    _create_session(
        repository,
        session_id="failed",
        name="数据结构实验",
        started_at=base + timedelta(hours=2),
        status="failed",
        shell="zsh",
        capture_count=1,
    )
    running = _create_session(
        repository,
        session_id="running",
        name="正在录制实验",
        started_at=base + timedelta(hours=3),
        status="running",
        shell="powershell_51",
    )
    app = _app(tmp_path, repository)

    try:
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            await _open_records(app, pilot)
            text = _screen_text(app.screen)
            for expected in (
                "录制中",
                "已完成",
                "已中断",
                "失败",
                "PowerShell 7",
                "Windows PowerShell 5.1",
                "Bash",
                "Zsh",
                "Capture 6",
                "Capture 3",
                "Capture 1",
            ):
                assert expected in text
    finally:
        repository.release_owner(running)


@pytest.mark.asyncio
async def test_records_selection_uses_session_id_and_movement_does_not_rescan(
    tmp_path: Path,
) -> None:
    repository = CountingSessionRepository(tmp_path / ".csbox" / "sessions")
    tied = datetime(2026, 8, 19, 8, 0, tzinfo=UTC)
    _create_session(repository, session_id="zeta", name="同名实验", started_at=tied)
    _create_session(repository, session_id="alpha", name="同名实验", started_at=tied)
    _create_session(
        repository,
        session_id="newest",
        name="最新实验",
        started_at=tied + timedelta(hours=1),
    )
    app = _app(tmp_path, repository)

    async with app.run_test(size=(120, 35)) as pilot:
        await pilot.pause()
        screen = await _open_records(app, pilot)
        assert [summary.metadata.session_id for summary in screen.summaries] == [
            "newest",
            "alpha",
            "zeta",
        ]
        assert screen.selected_session_id == "newest"
        assert repository.list_summary_calls == 1

        await pilot.press("down")
        assert screen.selected_session_id == "alpha"
        await pilot.press("down")
        assert screen.selected_session_id == "zeta"
        await pilot.press("up")
        assert screen.selected_session_id == "alpha"
        assert repository.list_summary_calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ("completed", "interrupted", "failed"))
async def test_records_opens_review_for_every_terminal_status(
    tmp_path: Path,
    status: str,
) -> None:
    repository = SessionRepository.from_cwd(tmp_path)
    _create_session(
        repository,
        session_id=status,
        name=f"{status} 实验",
        started_at=datetime(2026, 8, 19, 8, 0, tzinfo=UTC),
        status=status,
    )
    app = _app(tmp_path, repository)

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await _open_records(app, pilot)
        await pilot.press("enter")
        await pilot.pause()
        assert app.screen.name == "review"
        await pilot.press("escape")
        await pilot.pause()
        assert app.screen.name == "records"


@pytest.mark.asyncio
async def test_running_session_disables_review_with_beginner_friendly_guidance(
    tmp_path: Path,
) -> None:
    repository = SessionRepository.from_cwd(tmp_path)
    running = _create_session(
        repository,
        session_id="running",
        name="运行中的实验",
        started_at=datetime(2026, 8, 19, 8, 0, tzinfo=UTC),
        status="running",
    )
    app = _app(tmp_path, repository)

    try:
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            await _open_records(app, pilot)
            assert app.screen.query_one("#records-review", Button).disabled is True
            await pilot.press("enter")
            await pilot.pause()
            assert app.screen.name == "records"
            assert "实验结束后可回看" in _screen_text(app.screen)
    finally:
        repository.release_owner(running)


@pytest.mark.asyncio
async def test_review_return_retains_session_and_refreshes_only_its_capture_count(
    tmp_path: Path,
) -> None:
    repository = CountingSessionRepository(tmp_path / ".csbox" / "sessions")
    base = datetime(2026, 8, 19, 8, 0, tzinfo=UTC)
    _create_session(
        repository,
        session_id="newest",
        name="最新实验",
        started_at=base + timedelta(hours=1),
    )
    _create_session(repository, session_id="target", name="目标实验", started_at=base)
    app = _app(tmp_path, repository)

    async with app.run_test(size=(120, 35)) as pilot:
        await pilot.pause()
        records = await _open_records(app, pilot)
        await pilot.press("down")
        assert records.selected_session_id == "target"
        assert records.selected_summary.capture_count == 0
        focused_id = records.focused.id if records.focused is not None else None

        await pilot.press("enter")
        await pilot.pause()
        assert app.screen.name == "review"
        await pilot.press("c")
        await pilot.pause()
        assert isinstance(app.screen, CaptureTitleDialog)
        app.screen.query_one("#capture-title-input", Input).value = "补充 Capture"
        await pilot.press("enter")
        await pilot.pause()
        assert app.screen.name == "review"
        await pilot.press("escape")
        await pilot.pause()

        assert app.screen is records
        assert records.selected_session_id == "target"
        assert records.selected_summary.capture_count == 1
        assert repository.list_summary_calls == 1
        assert repository.read_summary_calls == ["target"]
        assert records.focused is not None
        assert records.focused.id == focused_id


@pytest.mark.asyncio
async def test_review_return_restores_nonzero_scroll_across_layout_transitions(
    tmp_path: Path,
) -> None:
    repository = SessionRepository.from_cwd(tmp_path)
    base = datetime(2026, 8, 19, 8, 0, tzinfo=UTC)
    for index in range(18):
        _create_session(
            repository,
            session_id=f"session-{index:02d}",
            name=f"滚动记录 {index:02d}",
            started_at=base + timedelta(minutes=index),
        )
    app = _app(tmp_path, repository)

    async with app.run_test(size=(160, 45)) as pilot:
        await pilot.pause()
        records = await _open_records(app, pilot)
        await pilot.press(*(["down"] * 17))
        await pilot.pause()
        selected_id = records.selected_session_id

        await pilot.resize_terminal(80, 24)
        await pilot.pause()
        await pilot.resize_terminal(160, 45)
        await pilot.pause()

        list_panel = records.query_one("#records-list", Static)
        narrow_panel = records.query_one("#records-narrow", Static)
        saved_scroll = (float(list_panel.scroll_y), float(narrow_panel.scroll_y))
        assert saved_scroll[0] > 0
        assert saved_scroll[1] > 0

        await pilot.press("enter")
        await pilot.pause()
        assert app.screen.name == "review"
        await pilot.press("escape")
        await pilot.pause()

        assert app.screen is records
        assert records.selected_session_id == selected_id
        assert float(list_panel.scroll_y) == pytest.approx(saved_scroll[0])
        assert float(narrow_panel.scroll_y) == pytest.approx(saved_scroll[1])


@pytest.mark.asyncio
async def test_missing_selected_session_chooses_the_next_stable_record_after_review(
    tmp_path: Path,
) -> None:
    repository = CountingSessionRepository(tmp_path / ".csbox" / "sessions")
    base = datetime(2026, 8, 19, 8, 0, tzinfo=UTC)
    _create_session(
        repository,
        session_id="first",
        name="第一条",
        started_at=base + timedelta(hours=2),
    )
    target = _create_session(
        repository,
        session_id="target",
        name="将消失的记录",
        started_at=base + timedelta(hours=1),
    )
    _create_session(repository, session_id="next", name="下一条", started_at=base)
    app = _app(tmp_path, repository)

    async with app.run_test(size=(120, 35)) as pilot:
        await pilot.pause()
        records = await _open_records(app, pilot)
        await pilot.press("down", "enter")
        await pilot.pause()
        assert app.screen.name == "review"
        target.metadata.unlink()
        await pilot.press("escape")
        await pilot.pause()

        assert app.screen is records
        assert records.selected_session_id == "next"
        assert [item.metadata.session_id for item in records.summaries] == ["first", "next"]


@pytest.mark.asyncio
async def test_corrupt_session_does_not_break_valid_records_or_leak_details(tmp_path: Path) -> None:
    repository = SessionRepository.from_cwd(tmp_path)
    _create_session(
        repository,
        session_id="valid-a",
        name="中文有效记录 A",
        started_at=datetime(2026, 8, 19, 8, 0, tzinfo=UTC),
    )
    corrupt = repository.root / "corrupt"
    corrupt.mkdir(parents=True)
    corrupt.joinpath("metadata.json").write_text(f'{{"id":"token={SECRET}"', encoding="utf-8")
    _create_session(
        repository,
        session_id="valid-c",
        name="中文有效记录 C",
        started_at=datetime(2026, 8, 19, 9, 0, tzinfo=UTC),
    )
    app = _app(tmp_path, repository)

    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        await _open_records(app, pilot)
        text = _screen_text(app.screen)
        assert "中文有效记录 A" in text
        assert "中文有效记录 C" in text
        assert SECRET not in text
        assert "Traceback" not in text


@pytest.mark.asyncio
async def test_records_to_review_redacts_sensitive_metadata_from_the_ui(tmp_path: Path) -> None:
    repository = SessionRepository.from_cwd(tmp_path)
    _create_session(
        repository,
        session_id="sensitive-metadata",
        name=f"token={SECRET}",
        started_at=datetime(2026, 8, 19, 8, 0, tzinfo=UTC),
        status="failed",
        reason=f"token={SECRET}",
    )
    app = _app(tmp_path, repository)

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await _open_records(app, pilot)
        assert SECRET not in _screen_text(app.screen)

        await pilot.press("enter")
        await pilot.pause()

        assert app.screen.name == "review"
        review_text = _screen_text(app.screen)
        assert SECRET not in review_text
        assert "Traceback" not in review_text


@pytest.mark.asyncio
@pytest.mark.parametrize("size", LAYOUTS)
async def test_records_cjk_layout_is_usable_at_supported_sizes(
    tmp_path: Path,
    size: tuple[int, int],
) -> None:
    repository = SessionRepository.from_cwd(tmp_path)
    _create_session(
        repository,
        session_id="cjk",
        name=("计算机网络 Lab e\N{COMBINING ACUTE ACCENT} 超长实验名称" * 12),
        started_at=datetime(2026, 8, 19, 8, 0, tzinfo=UTC),
        capture_count=2,
    )
    app = _app(tmp_path, repository)

    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        await _open_records(app, pilot)
        assert app.screen.is_wide is (size[0] >= 120)
        _assert_static_lines_fit(app.screen)
        for widget in app.screen.query("*"):
            if _is_rendered(widget) and widget is not app.screen:
                assert widget.region.x >= 0
                assert widget.region.y >= 0
                assert widget.region.right <= size[0]
                assert widget.region.bottom <= size[1]


@pytest.mark.asyncio
async def test_records_continuous_resize_preserves_selection_focus_and_active_pane(
    tmp_path: Path,
) -> None:
    repository = SessionRepository.from_cwd(tmp_path)
    base = datetime(2026, 8, 19, 8, 0, tzinfo=UTC)
    _create_session(
        repository,
        session_id="first",
        name="第一个实验",
        started_at=base + timedelta(hours=1),
    )
    _create_session(repository, session_id="selected", name="选中的实验", started_at=base)
    app = _app(tmp_path, repository)

    async with app.run_test(size=(160, 45)) as pilot:
        await pilot.pause()
        screen = await _open_records(app, pilot)
        await pilot.press("down")
        selected_id = screen.selected_session_id
        focused_id = screen.focused.id if screen.focused is not None else None
        active_pane = screen.active_pane

        for size in ((80, 24), (100, 30), (120, 35), (160, 45)):
            await pilot.resize_terminal(*size)
            await pilot.pause()
            assert screen.selected_session_id == selected_id
            assert screen.active_pane == active_pane
            assert screen.focused is not None
            assert screen.focused.id == focused_id
            _assert_static_lines_fit(screen)


@pytest.mark.asyncio
async def test_records_exposes_export_action_and_e_opens_export_dialog(
    tmp_path: Path,
) -> None:
    repository = SessionRepository.from_cwd(tmp_path)
    _create_session(
        repository,
        session_id="export-target",
        name="计算机网络实验三",
        started_at=datetime(2026, 8, 19, 8, 0, tzinfo=UTC),
        capture_count=2,
    )
    app = _app(tmp_path, repository)

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        records = await _open_records(app, pilot)

        export_button = records.query_one("#records-export", Button)
        assert str(export_button.label) == "导出材料"

        await pilot.click("#records-export")
        await pilot.pause()
        assert app.screen.name == "export"
        await pilot.press("escape")
        await pilot.pause()
        assert app.screen is records

        await pilot.press("e")
        await pilot.pause()

        assert app.screen.name == "export"
        assert records.selected_session_id == "export-target"


@pytest.mark.asyncio
async def test_running_records_disable_review_and_export_with_end_guidance(
    tmp_path: Path,
) -> None:
    repository = SessionRepository.from_cwd(tmp_path)
    running = _create_session(
        repository,
        session_id="running-export",
        name="正在录制实验",
        started_at=datetime(2026, 8, 19, 8, 0, tzinfo=UTC),
        status="running",
    )
    app = _app(tmp_path, repository)

    try:
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            records = await _open_records(app, pilot)

            assert records.query_one("#records-review", Button).disabled is True
            assert records.query_one("#records-export", Button).disabled is True
            assert "实验结束后可回看和导出" in _screen_text(records)

            await pilot.press("e")
            await pilot.pause()
            assert app.screen is records
    finally:
        repository.release_owner(running)


@pytest.mark.asyncio
async def test_cancel_export_returns_to_records_with_selected_session_unchanged(
    tmp_path: Path,
) -> None:
    repository = SessionRepository.from_cwd(tmp_path)
    base = datetime(2026, 8, 19, 8, 0, tzinfo=UTC)
    _create_session(
        repository,
        session_id="newest",
        name="最新实验",
        started_at=base + timedelta(hours=1),
    )
    _create_session(
        repository,
        session_id="selected-export",
        name="选中的实验",
        started_at=base,
    )
    app = _app(tmp_path, repository)

    async with app.run_test(size=(120, 35)) as pilot:
        await pilot.pause()
        records = await _open_records(app, pilot)
        await pilot.press("down")
        selected_id = records.selected_session_id

        await pilot.press("e")
        await pilot.pause()
        assert app.screen.name == "export"
        await pilot.press("escape")
        await pilot.pause()

        assert app.screen is records
        assert records.selected_session_id == selected_id


@pytest.mark.asyncio
async def test_export_dialog_shows_default_target_and_existing_directory_confirmation(
    tmp_path: Path,
) -> None:
    repository = SessionRepository.from_cwd(tmp_path)
    _create_session(
        repository,
        session_id="existing-export",
        name="中文实验",
        started_at=datetime(2026, 8, 19, 8, 0, tzinfo=UTC),
    )
    (tmp_path / "existing-export-evidence").mkdir()
    app = _app(tmp_path, repository)

    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        records = await _open_records(app, pilot)
        await pilot.press("e")
        await pilot.pause()

        assert app.screen.name == "export"
        destination_input = app.screen.query_one("#export-destination-input", Input)
        assert destination_input.value == str(tmp_path / "existing-export-evidence")
        assert "existing-export-evidence" in _screen_text(app.screen)
        assert "深色" in _screen_text(app.screen)

        await pilot.press("enter")
        await pilot.pause()
        assert app.screen.name == "export-overwrite"
        assert "该导出目录已经存在" in _screen_text(app.screen)
        assert "刷新已有导出" in _screen_text(app.screen)

        await pilot.press("escape")
        await pilot.pause()
        assert app.screen is records


@pytest.mark.asyncio
async def test_export_dialog_resize_does_not_rescan_records(
    tmp_path: Path,
) -> None:
    repository = CountingSessionRepository(tmp_path / ".csbox" / "sessions")
    _create_session(
        repository,
        session_id="read-only-export",
        name="只读目标实验",
        started_at=datetime(2026, 8, 19, 8, 0, tzinfo=UTC),
    )
    app = _app(tmp_path, repository)

    async with app.run_test(size=(160, 45)) as pilot:
        await pilot.pause()
        records = await _open_records(app, pilot)
        assert repository.list_summary_calls == 1
        await pilot.press("e")
        await pilot.pause()
        destination_input = app.screen.query_one("#export-destination-input", Input)
        default_destination = str(tmp_path / "read-only-export-evidence")
        assert destination_input.value == default_destination

        for size in ((80, 24), (120, 35), (100, 30), (160, 45)):
            await pilot.resize_terminal(*size)
            await pilot.pause()
            assert app.screen.name == "export"
            assert repository.list_summary_calls == 1
            assert destination_input.value == default_destination

        await pilot.press("escape")
        await pilot.pause()
        assert app.screen is records


@pytest.mark.asyncio
async def test_export_dialog_restore_default_returns_the_exact_session_destination(
    tmp_path: Path,
) -> None:
    repository = SessionRepository.from_cwd(tmp_path)
    _create_session(
        repository,
        session_id="restore-default",
        name="恢复默认实验",
        started_at=datetime(2026, 8, 19, 8, 0, tzinfo=UTC),
    )
    app = _app(tmp_path, repository)

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await _open_records(app, pilot)
        await pilot.press("e")
        await pilot.pause()

        destination_input = app.screen.query_one("#export-destination-input", Input)
        default_destination = str(tmp_path / "restore-default-evidence")
        custom_destination = str(tmp_path / "课程资料" / "实验 三")
        destination_input.value = custom_destination
        await pilot.pause()
        assert destination_input.value == custom_destination

        await pilot.click("#export-restore-default")
        await pilot.pause()

        assert destination_input.value == default_destination
        assert app.screen.focused is not None
        assert app.screen.focused.id == "export-restore-default"


CUSTOM_DESTINATIONS = (
    "custom output/课程实验/实验 三",
    "/home/测试用户/实验材料",
    "/mnt/e/课程实验/操作系统实验",
    r"D:\课程资料\计算机网络\实验三",
    r"C:\Users\测试用户\Desktop\实验材料",
    "课程资料/实验三 " + "混合宽度" * 40,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("destination_text", CUSTOM_DESTINATIONS)
async def test_export_dialog_passes_custom_destination_as_the_exact_final_directory(
    tmp_path: Path,
    destination_text: str,
) -> None:
    repository = SessionRepository.from_cwd(tmp_path)
    _create_session(
        repository,
        session_id="custom-destination",
        name="自定义目录实验",
        started_at=datetime(2026, 8, 19, 8, 0, tzinfo=UTC),
    )
    calls: list[tuple[str, Path, str, bool]] = []

    def fake_export(
        session: str,
        destination: Path,
        *,
        theme: str,
        force: bool,
    ) -> LabExportResult:
        calls.append((session, destination, theme, force))
        return LabExportResult(
            destination=destination,
            evidence=(),
            markdown=destination / "evidence.md",
            cast=destination / "session.cast",
            commands=None,
        )

    app = _app_with_export_action(tmp_path, repository, fake_export)

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        records = await _open_records(app, pilot)
        await pilot.press("e")
        await pilot.pause()
        destination_input = app.screen.query_one("#export-destination-input", Input)
        destination_input.value = destination_text
        await pilot.pause()
        await pilot.click("#export-confirm")
        await _wait_for_export_result(app, records, pilot)

        assert calls == [("custom-destination", Path(destination_text), "dark", False)]
        assert calls[0][1] == Path(destination_text)
        assert calls[0][1].name != "custom-destination-evidence"


@pytest.mark.asyncio
async def test_export_dialog_empty_destination_is_disabled_and_never_exports(
    tmp_path: Path,
) -> None:
    repository = SessionRepository.from_cwd(tmp_path)
    _create_session(
        repository,
        session_id="empty-destination",
        name="空路径实验",
        started_at=datetime(2026, 8, 19, 8, 0, tzinfo=UTC),
    )
    calls: list[object] = []

    def unexpected_export(*args: object, **kwargs: object) -> LabExportResult:
        calls.append((args, kwargs))
        raise AssertionError("empty destination must not call the exporter")

    app = _app_with_export_action(tmp_path, repository, unexpected_export)

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await _open_records(app, pilot)
        await pilot.press("e")
        await pilot.pause()
        destination_input = app.screen.query_one("#export-destination-input", Input)
        destination_input.focus()
        await pilot.press("ctrl+a", "end", "ctrl+u")
        await pilot.pause()

        assert destination_input.value == ""
        assert app.screen.query_one("#export-confirm", Button).disabled is True
        assert "请输入导出位置" in _screen_text(app.screen)

        await pilot.press("enter")
        await pilot.pause()

        assert app.screen.name == "export"
        assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "message"),
    (
        (LabExportError("导出路径不是目录：secret-regular-file"), "导出位置不是目录"),
        (PermissionError("secret-permission"), "无法写入导出目录"),
    ),
)
async def test_export_destination_failures_are_controlled_and_redacted(
    tmp_path: Path,
    error: Exception,
    message: str,
) -> None:
    repository = SessionRepository.from_cwd(tmp_path)
    _create_session(
        repository,
        session_id="destination-failure",
        name="目录失败实验",
        started_at=datetime(2026, 8, 19, 8, 0, tzinfo=UTC),
    )

    def fail_export(*args: object, **kwargs: object) -> LabExportResult:
        del args, kwargs
        raise error

    app = _app_with_export_action(tmp_path, repository, fail_export)

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        records = await _open_records(app, pilot)
        await pilot.press("e")
        await pilot.pause()
        destination_input = app.screen.query_one("#export-destination-input", Input)
        destination_input.value = str(tmp_path / "report.txt")
        await pilot.press("enter")
        await _wait_for_export_result(app, records, pilot)

        assert app.screen.name == "export-result"
        text = _screen_text(app.screen)
        assert message in text
        assert "secret-" not in text
        assert "Traceback" not in text
        assert records.selected_session_id == "destination-failure"


@pytest.mark.asyncio
async def test_export_regular_file_destination_fails_without_overwrite(
    tmp_path: Path,
) -> None:
    repository = SessionRepository.from_cwd(tmp_path)
    _create_session(
        repository,
        session_id="regular-file-destination",
        name="普通文件目标实验",
        started_at=datetime(2026, 8, 19, 8, 0, tzinfo=UTC),
    )
    destination = tmp_path / "report.txt"
    destination.write_text("用户文件", encoding="utf-8")
    app = _app(tmp_path, repository)

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        records = await _open_records(app, pilot)
        await pilot.press("e")
        await pilot.pause()
        app.screen.query_one("#export-destination-input", Input).value = str(destination)
        await pilot.press("enter")
        await _wait_for_export_result(app, records, pilot)

        assert app.screen.name == "export-result"
        text = _screen_text(app.screen)
        assert "导出位置不是目录" in text
        assert "Traceback" not in text
        assert destination.read_text(encoding="utf-8") == "用户文件"


@pytest.mark.asyncio
async def test_export_destination_input_changed_does_not_cross_heavy_boundaries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = CountingSessionRepository(tmp_path / ".csbox" / "sessions")
    _create_session(
        repository,
        session_id="lightweight-destination",
        name="轻量输入实验",
        started_at=datetime(2026, 8, 19, 8, 0, tzinfo=UTC),
    )
    app = _app(tmp_path, repository)

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await _open_records(app, pilot)
        await pilot.press("e")
        await pilot.pause()

        def forbidden(*args: object, **kwargs: object) -> object:
            del args, kwargs
            raise AssertionError("destination typing crossed a heavy boundary")

        monkeypatch.setattr("csbox.lab.service.LabService.export", forbidden)
        monkeypatch.setattr(SessionRepository, "list_summaries", forbidden)
        monkeypatch.setattr(CaptureStore, "load", forbidden)
        monkeypatch.setattr(Path, "mkdir", forbidden)
        monkeypatch.setattr(Path, "rglob", forbidden)

        destination_input = app.screen.query_one("#export-destination-input", Input)
        destination_input.value = "课程资料/实验三"
        destination_input.insert_text_at_cursor("/追加")
        await pilot.pause()

        assert destination_input.value.endswith("课程资料/实验三/追加")
        assert repository.list_summary_calls == 1


@pytest.mark.asyncio
async def test_export_dialog_keyboard_focus_and_theme_navigation_keep_input_native(
    tmp_path: Path,
) -> None:
    repository = SessionRepository.from_cwd(tmp_path)
    _create_session(
        repository,
        session_id="keyboard-destination",
        name="键盘目录实验",
        started_at=datetime(2026, 8, 19, 8, 0, tzinfo=UTC),
    )
    calls: list[tuple[Path, str]] = []

    def fake_export(
        session: str,
        destination: Path,
        *,
        theme: str,
        force: bool,
    ) -> LabExportResult:
        del session, force
        calls.append((destination, theme))
        return LabExportResult(
            destination=destination,
            evidence=(),
            markdown=destination / "evidence.md",
            cast=destination / "session.cast",
            commands=None,
        )

    app = _app_with_export_action(tmp_path, repository, fake_export)

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        records = await _open_records(app, pilot)
        await pilot.press("e")
        await pilot.pause()
        assert app.screen.focused is not None
        assert app.screen.focused.id == "export-destination-input"

        await pilot.press("down", "down")
        assert app.screen.focused is not None
        assert app.screen.focused.id == "export-advanced"
        await pilot.press("enter")
        await pilot.pause()
        await pilot.press("down")
        assert app.screen.focused is not None
        assert app.screen.focused.id == "export-theme-select"
        await pilot.press("enter")
        await pilot.press("down", "enter")
        await pilot.pause()
        assert app.screen.query_one("#export-theme-select").value == "light"
        await pilot.press("down", "enter")
        await _wait_for_export_result(app, records, pilot)

        assert calls == [(tmp_path / "keyboard-destination-evidence", "light")]


@pytest.mark.asyncio
async def test_custom_destination_and_cursor_survive_continuous_resize(
    tmp_path: Path,
) -> None:
    project_dir = tmp_path.joinpath(*(["中文课程项目"] * 5))
    repository = SessionRepository.from_cwd(project_dir)
    _create_session(
        repository,
        session_id="resize-destination",
        name="resize 路径实验",
        started_at=datetime(2026, 8, 19, 8, 0, tzinfo=UTC),
    )
    app = _app(project_dir, repository)

    async with app.run_test(size=(160, 45)) as pilot:
        await pilot.pause()
        records = await _open_records(app, pilot)
        await pilot.press("e")
        await pilot.pause()
        destination_input = app.screen.query_one("#export-destination-input", Input)
        value = "/mnt/e/课程资料/操作系统实验/" + "长路径" * 20
        destination_input.value = value
        destination_input.focus()
        await pilot.press("end", "left", "left")
        cursor_position = destination_input.cursor_position

        for size in ((80, 24), (120, 35), (100, 30), (160, 45)):
            await pilot.resize_terminal(*size)
            await pilot.pause()
            assert destination_input.value == value
            assert destination_input.cursor_position == cursor_position
            assert app.screen.focused is destination_input
            assert records.selected_session_id == "resize-destination"


@pytest.mark.asyncio
async def test_existing_directory_cancel_does_not_submit_export(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = SessionRepository.from_cwd(tmp_path)
    _create_session(
        repository,
        session_id="cancel-existing",
        name="已有目录实验",
        started_at=datetime(2026, 8, 19, 8, 0, tzinfo=UTC),
    )
    (tmp_path / "cancel-existing-evidence").mkdir()
    calls: list[tuple[object, ...]] = []

    def unexpected_export(*args: object, **kwargs: object) -> LabExportResult:
        calls.append((*args, kwargs))
        raise AssertionError("cancelled overwrite must not submit export")

    monkeypatch.setattr("csbox.lab.service.LabService.export", unexpected_export)
    app = _app(tmp_path, repository)

    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        await _open_records(app, pilot)
        await pilot.press("e", "enter")
        await pilot.pause()
        assert app.screen.name == "export-overwrite"
        await pilot.press("escape")
        await pilot.pause()

        assert app.screen.name == "records"
        assert calls == []


@pytest.mark.asyncio
async def test_confirmed_existing_directory_uses_lab_service_force_semantics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = SessionRepository.from_cwd(tmp_path)
    _create_session(
        repository,
        session_id="force-existing",
        name="刷新实验",
        started_at=datetime(2026, 8, 19, 8, 0, tzinfo=UTC),
    )
    destination = tmp_path / "课程资料" / "实验三"
    destination.parent.mkdir()
    destination.mkdir()
    calls: list[tuple[str, Path, str, bool]] = []

    def fake_export(
        self: object,
        session: str,
        output: Path,
        *,
        theme: str | None = None,
        force: bool = False,
    ) -> LabExportResult:
        del self
        calls.append((session, output, theme or "", force))
        return LabExportResult(
            destination=output,
            evidence=(),
            markdown=output / "evidence.md",
            cast=output / "session.cast",
            commands=None,
        )

    monkeypatch.setattr("csbox.lab.service.LabService.export", fake_export)
    app = _app(tmp_path, repository)

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        records = await _open_records(app, pilot)
        await pilot.press("e")
        await pilot.pause()
        app.screen.query_one("#export-destination-input", Input).value = str(destination)
        await pilot.press("enter")
        await pilot.pause()
        assert app.screen.name == "export-overwrite"
        await pilot.press("enter")
        await _wait_for_export_result(app, records, pilot)

        assert calls == [("force-existing", destination, "dark", True)]
        assert app.screen.name == "export-result"


@pytest.mark.asyncio
async def test_export_worker_shows_actual_artifacts_and_keeps_optional_commands_hidden(
    tmp_path: Path,
) -> None:
    repository = SessionRepository.from_cwd(tmp_path)
    _create_session(
        repository,
        session_id="worker-export",
        name="后台导出实验",
        started_at=datetime(2026, 8, 19, 8, 0, tzinfo=UTC),
    )
    app = _app(tmp_path, repository)

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        records = await _open_records(app, pilot)
        await pilot.press("e", "enter")
        await _wait_for_export_result(app, records, pilot)

        assert app.screen.name == "export-result"
        text = _screen_text(app.screen)
        assert "导出完成" in text
        assert "evidence.md" in text
        assert "session.cast" in text
        assert "commands.txt" not in text

        await pilot.press("enter")
        await pilot.pause()
        assert app.screen is records
        assert records.selected_session_id == "worker-export"


@pytest.mark.asyncio
async def test_export_failure_is_safe_and_returns_a_retryable_result_screen(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "CSBOX_SECRET_SENTINEL_export_failure"
    repository = SessionRepository.from_cwd(tmp_path)
    _create_session(
        repository,
        session_id="failed-export",
        name="失败实验",
        started_at=datetime(2026, 8, 19, 8, 0, tzinfo=UTC),
    )

    def fail_export(*args: object, **kwargs: object) -> LabExportResult:
        del args, kwargs
        raise RuntimeError(secret)

    monkeypatch.setattr("csbox.lab.service.LabService.export", fail_export)
    app = _app(tmp_path, repository)

    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        records = await _open_records(app, pilot)
        selected_id = records.selected_session_id
        await pilot.press("e", "enter")
        await _wait_for_export_result(app, records, pilot)

        assert app.screen.name == "export-result"
        text = _screen_text(app.screen)
        assert "导出失败" in text
        assert "重新导出" in text
        assert secret not in text
        assert "Traceback" not in text

        await pilot.press("escape")
        await pilot.pause()
        assert app.screen is records
        assert records.selected_session_id == selected_id


@pytest.mark.asyncio
async def test_export_failure_retry_uses_a_fresh_worker_and_succeeds(
    tmp_path: Path,
) -> None:
    repository = SessionRepository.from_cwd(tmp_path)
    _create_session(
        repository,
        session_id="retry-export",
        name="重试实验",
        started_at=datetime(2026, 8, 19, 8, 0, tzinfo=UTC),
    )
    attempts: list[int] = []
    destinations: list[Path] = []
    custom_destination = tmp_path / "课程资料" / "重试导出"

    def fail_once_then_export(
        session: str,
        destination: Path,
        *,
        theme: str,
        force: bool,
    ) -> LabExportResult:
        del session, theme, force
        attempts.append(len(attempts) + 1)
        destinations.append(destination)
        if len(attempts) == 1:
            raise RuntimeError("first attempt failed")
        return LabExportResult(
            destination=destination,
            evidence=(),
            markdown=destination / "evidence.md",
            cast=destination / "session.cast",
            commands=None,
        )

    app = _app_with_export_action(tmp_path, repository, fail_once_then_export)

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        records = await _open_records(app, pilot)
        await pilot.press("e")
        await pilot.pause()
        app.screen.query_one("#export-destination-input", Input).value = str(custom_destination)
        await pilot.press("enter")
        await _wait_for_export_result(app, records, pilot)
        assert "导出失败" in _screen_text(app.screen)
        assert records._exporting is False

        await pilot.press("enter")
        await pilot.pause()
        assert app.screen.name == "export"
        app.screen.query_one("#export-destination-input", Input).value = str(custom_destination)
        await pilot.press("enter")
        await _wait_for_export_result(app, records, pilot)

        assert attempts == [1, 2]
        assert destinations == [custom_destination, custom_destination]
        assert "导出完成" in _screen_text(app.screen)
        assert records.selected_session_id == "retry-export"


@pytest.mark.asyncio
async def test_records_tab_focus_and_enter_reach_export_without_mouse(
    tmp_path: Path,
) -> None:
    repository = SessionRepository.from_cwd(tmp_path)
    _create_session(
        repository,
        session_id="keyboard-export",
        name="键盘导出实验",
        started_at=datetime(2026, 8, 19, 8, 0, tzinfo=UTC),
    )
    app = _app(tmp_path, repository)

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        records = await _open_records(app, pilot)
        assert records.focused is not None
        assert records.focused.id == "records-review"

        await pilot.press("tab")
        await pilot.pause()
        assert records.focused is not None
        assert records.focused.id == "records-export"

        await pilot.press("shift+tab")
        await pilot.pause()
        assert records.focused is not None
        assert records.focused.id == "records-review"

        await pilot.press("tab", "enter")
        await pilot.pause()
        assert app.screen.name == "export"


@pytest.mark.asyncio
async def test_export_theme_and_default_destination_are_passed_to_lab_service(
    tmp_path: Path,
) -> None:
    repository = SessionRepository.from_cwd(tmp_path)
    _create_session(
        repository,
        session_id="theme-export",
        name="主题实验",
        started_at=datetime(2026, 8, 19, 8, 0, tzinfo=UTC),
    )
    calls: list[tuple[str, Path, str, bool]] = []

    def fake_export(
        session: str,
        destination: Path,
        *,
        theme: str,
        force: bool,
    ) -> LabExportResult:
        calls.append((session, destination, theme, force))
        return LabExportResult(
            destination=destination,
            evidence=(),
            markdown=destination / "evidence.md",
            cast=destination / "session.cast",
            commands=None,
        )

    app = _app_with_export_action(tmp_path, repository, fake_export)

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        records = await _open_records(app, pilot)
        await pilot.press("e")
        await pilot.pause()
        await pilot.click("#export-advanced")
        await pilot.pause()
        app.screen.query_one("#export-theme-select").value = "light"
        await pilot.pause()
        await pilot.click("#export-confirm")
        await _wait_for_export_result(app, records, pilot)

        assert calls == [("theme-export", tmp_path / "theme-export-evidence", "light", False)]


@pytest.mark.asyncio
async def test_success_with_warnings_is_not_rendered_as_failure_and_lists_real_artifacts(
    tmp_path: Path,
) -> None:
    repository = SessionRepository.from_cwd(tmp_path)
    _create_session(
        repository,
        session_id="warning-export",
        name="提示实验",
        started_at=datetime(2026, 8, 19, 8, 0, tzinfo=UTC),
    )

    def export_with_warning(
        session: str,
        output: Path,
        *,
        theme: str,
        force: bool,
    ) -> LabExportResult:
        del session, theme, force
        return LabExportResult(
            destination=output,
            evidence=(output / "evidence" / "01-记录.png",),
            markdown=output / "evidence.md",
            cast=output / "session.cast",
            commands=output / "commands.txt",
            warnings=("命令识别置信度不足",),
        )

    app = _app_with_export_action(tmp_path, repository, export_with_warning)

    async with app.run_test(size=(120, 35)) as pilot:
        await pilot.pause()
        records = await _open_records(app, pilot)
        await pilot.press("e", "enter")
        await _wait_for_export_result(app, records, pilot)

        text = _screen_text(app.screen)
        assert "导出完成，但有 1 条提示" in text
        assert "导出失败" not in text
        assert "evidence/01-记录.png" in text
        assert "evidence.md" in text
        assert "session.cast" in text
        assert "commands.txt" in text
        assert "命令识别置信度不足" in text


@pytest.mark.asyncio
async def test_review_edited_capture_title_is_read_fresh_by_export_action(
    tmp_path: Path,
) -> None:
    repository = SessionRepository.from_cwd(tmp_path)
    _create_session(
        repository,
        session_id="fresh-title-export",
        name="标题刷新实验",
        started_at=datetime(2026, 8, 19, 8, 0, tzinfo=UTC),
        capture_count=1,
    )

    class StubRenderer:
        def render(self, snapshot: object, destination: Path, theme: object) -> Path:
            del snapshot, theme
            destination.write_bytes(b"png")
            return destination

    def export_from_canonical_store(
        session: str,
        output: Path,
        *,
        theme: str,
        force: bool,
    ) -> LabExportResult:
        del theme
        resolved = repository.resolve(session)
        return LabExporter(
            StubRenderer(),  # type: ignore[arg-type]
            theme=RenderTheme.dark(),
        ).export(resolved, output, force=force)

    app = _app_with_export_action(tmp_path, repository, export_from_canonical_store)

    async with app.run_test(size=(120, 35)) as pilot:
        await pilot.pause()
        records = await _open_records(app, pilot)
        await pilot.press("enter")
        await pilot.pause()
        await pilot.press("e")
        await pilot.pause()
        app.screen.query_one("#capture-title-input", Input).value = "路由器配置完成"
        await pilot.press("enter")
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        assert app.screen is records

        await pilot.press("e", "enter")
        await _wait_for_export_result(app, records, pilot)

        assert app.screen.name == "export-result"
        markdown = (tmp_path / "fresh-title-export-evidence" / "evidence.md").read_text(
            encoding="utf-8"
        )
        assert "路由器配置完成" in markdown


@pytest.mark.asyncio
async def test_corrupt_capture_export_keeps_success_warning_boundary(
    tmp_path: Path,
) -> None:
    repository = SessionRepository.from_cwd(tmp_path)
    paths = _create_session(
        repository,
        session_id="corrupt-capture-export",
        name="损坏 Capture 实验",
        started_at=datetime(2026, 8, 19, 8, 0, tzinfo=UTC),
        capture_count=0,
    )
    paths.captures.write_text(
        '{"version": 1, "captures": [{"id": "broken"}]}',
        encoding="utf-8",
    )
    app = _app(tmp_path, repository)

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        records = await _open_records(app, pilot)
        await pilot.press("e", "enter")
        await _wait_for_export_result(app, records, pilot)

        text = _screen_text(app.screen)
        assert "导出完成，但有 1 条提示" in text
        assert "导出失败" not in text
        assert "invalid capture 1 ignored" in text


@pytest.mark.asyncio
async def test_export_worker_rejects_duplicate_submission_while_busy(
    tmp_path: Path,
) -> None:
    repository = SessionRepository.from_cwd(tmp_path)
    _create_session(
        repository,
        session_id="duplicate-export",
        name="重复导出实验",
        started_at=datetime(2026, 8, 19, 8, 0, tzinfo=UTC),
    )
    started = threading.Event()
    release = threading.Event()
    calls: list[str] = []

    def blocking_export(
        session: str,
        destination: Path,
        *,
        theme: str,
        force: bool,
    ) -> LabExportResult:
        del destination, theme, force
        calls.append(session)
        started.set()
        release.wait(timeout=5)
        return LabExportResult(
            destination=tmp_path / "duplicate-export-evidence",
            evidence=(),
            markdown=tmp_path / "evidence.md",
            cast=tmp_path / "session.cast",
            commands=None,
        )

    app = _app_with_export_action(tmp_path, repository, blocking_export)

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        records = await _open_records(app, pilot)
        await pilot.press("e")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        await _wait_until(
            app,
            records,
            pilot,
            lambda: started.is_set() and getattr(records, "_exporting", False),
            description="slow export to enter running state",
        )
        selected_id = records.selected_session_id
        assert app.screen is records
        assert "正在导出" in _screen_text(records)
        await pilot.resize_terminal(80, 24)
        assert app.screen is records
        assert records.selected_session_id == selected_id
        await pilot.press("escape")
        await pilot.pause()
        assert app.screen is records
        await pilot.press("e")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert calls == ["duplicate-export"]
        release.set()
        await _wait_for_export_result(app, records, pilot)


@pytest.mark.asyncio
@pytest.mark.parametrize("size", LAYOUTS)
async def test_export_dialog_and_result_are_usable_with_cjk_at_supported_sizes(
    tmp_path: Path,
    size: tuple[int, int],
) -> None:
    project_dir = tmp_path.joinpath(*(["中文课程项目"] * 10))
    repository = SessionRepository.from_cwd(project_dir)
    _create_session(
        repository,
        session_id="cjk-export",
        name=("计算机网络实验 e\N{COMBINING ACUTE ACCENT} " * 12),
        started_at=datetime(2026, 8, 19, 8, 0, tzinfo=UTC),
    )
    app = _app(project_dir, repository)

    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        records = await _open_records(app, pilot)
        await pilot.press("e")
        await pilot.pause()
        assert app.screen.name == "export"
        _assert_static_lines_fit(app.screen)
        await pilot.click("#export-advanced")
        await pilot.pause()
        _assert_buttons_inside_screen(app.screen, "export-confirm", "export-cancel")
        selected_id = records.selected_session_id

        for next_size in ((160, 45), (80, 24), (120, 35), (100, 30), size):
            await pilot.resize_terminal(*next_size)
            await pilot.pause()
            assert app.screen.name == "export"
            assert records.selected_session_id == selected_id
            _assert_static_lines_fit(app.screen)
            _assert_buttons_inside_screen(app.screen, "export-confirm", "export-cancel")

        await pilot.press("escape")
        await pilot.pause()
        await pilot.press("e", "enter")
        await _wait_for_export_result(app, records, pilot)
        _assert_static_lines_fit(app.screen)
        assert records.selected_session_id == selected_id
