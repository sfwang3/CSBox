from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from time import monotonic

import pytest
from textual.widget import Widget
from textual.widgets import Button, Checkbox, Input, Static, TextArea

from csbox.core.display_width import display_width
from csbox.core.events import TerminalSize
from csbox.core.models import EnvironmentSnapshot
from csbox.evidence.exporter import (
    ReportExportError,
    ReportExportPhase,
    ReportExportResult,
)
from csbox.evidence.models import EvidenceItem, EvidenceSet, EvidenceSource
from csbox.evidence.repository import EvidencePersistenceError, EvidenceSetRepository
from csbox.lab.captures import CaptureStore
from csbox.lab.home_data import RealHomeDataSource
from csbox.lab.repository import SessionRepository
from csbox.lab.screen import TerminalCell, TerminalCursor, TerminalSnapshot
from csbox.locales import load_locale
from csbox.report.models import ReportProfile, ReportSection
from csbox.report.repository import ReportProfileRepository
from csbox.tui.app import CSBoxApp
from csbox.tui.dialogs.evidence import EvidenceItemDialog, EvidenceSetTitleDialog
from csbox.tui.dialogs.report import (
    ReportExportDialog,
    ReportExportOverwriteDialog,
    ReportProfileDialog,
)
from csbox.tui.screens.evidence import EvidenceSetEditorScreen, EvidenceSetsScreen
from csbox.tui.screens.evidence_sources import EvidenceCaptureBrowserScreen
from csbox.tui.screens.home import HomeScreen
from csbox.tui.screens.report import ReportExportResultScreen


def _environment() -> EnvironmentSnapshot:
    return EnvironmentSnapshot(
        os_name="Linux",
        os_version="test",
        python_version="3.14.0",
        shell="Bash",
        shell_executable="bash",
        powershell_51_available=False,
        powershell_7_available=False,
        is_wsl=False,
        terminal_columns=80,
        terminal_rows=24,
    )


def screen_text(screen: Widget) -> str:
    return "\n".join(
        [
            *(str(widget.renderable) for widget in screen.query(Static)),
            *(str(button.label) for button in screen.query(Button)),
            *(widget.value for widget in screen.query(Input)),
            *(widget.text for widget in screen.query(TextArea)),
        ]
    )


def _snapshot() -> TerminalSnapshot:
    return TerminalSnapshot(
        rows=1,
        columns=4,
        cells=(
            (
                TerminalCell("中", width=2),
                TerminalCell(" "),
                TerminalCell(" "),
                TerminalCell(" "),
            ),
        ),
        cursor=TerminalCursor(row=0, column=0),
        relative_time=0.0,
    )


def evidence_app(
    project_dir: Path,
    *,
    evidence_id_factory: object | None = None,
    session_repository: SessionRepository | None = None,
    evidence_repository: EvidenceSetRepository | None = None,
    report_export_action: object | None = None,
) -> CSBoxApp:
    session_repository = session_repository or SessionRepository(
        project_dir / ".csbox" / "sessions"
    )
    evidence_repository = evidence_repository or EvidenceSetRepository(
        project_dir / ".csbox" / "evidence",
        id_factory=evidence_id_factory,  # type: ignore[arg-type]
    )
    return CSBoxApp(
        data_source=RealHomeDataSource(session_repository, project_dir),
        environment=_environment(),
        locale=load_locale(),
        session_repository=session_repository,
        evidence_repository=evidence_repository,
        report_export_action=report_export_action,  # type: ignore[arg-type]
    )


async def open_evidence_list(app: CSBoxApp, pilot: object) -> None:
    app.screen.query_one("#entry-evidence", Button).focus()
    await pilot.press("enter")
    await pilot.pause()
    assert isinstance(app.screen, EvidenceSetsScreen)


async def open_empty_editor(app: CSBoxApp, pilot: object) -> None:
    await open_evidence_list(app, pilot)
    await pilot.press("n")
    await pilot.pause()
    app.screen.query_one("#evidence-set-title-input", Input).value = "证据集一"
    await pilot.press("enter")
    await pilot.pause()
    assert isinstance(app.screen, EvidenceSetEditorScreen)


async def open_saved_editor(app: CSBoxApp, pilot: object) -> None:
    await open_evidence_list(app, pilot)
    await pilot.press("enter")
    await pilot.pause()
    assert isinstance(app.screen, EvidenceSetEditorScreen)


def evidence_app_with_three_captures(
    tmp_path: Path,
) -> tuple[CSBoxApp, object, tuple[object, ...]]:
    session_repository = SessionRepository(tmp_path / ".csbox" / "sessions")
    paths = session_repository.create_starting(
        "长实验会话名称（用于展示）",
        shell="bash",
        shell_version=None,
        size=TerminalSize(columns=80, rows=24),
        cwd=tmp_path,
        session_id="session-1",
        started_at=datetime(2026, 8, 31, 0, 0, tzinfo=UTC),
    )
    paths.cast.write_bytes(b"canonical cast")
    capture_ids = iter(("capture-1", "capture-2", "capture-3"))
    store = CaptureStore(
        paths.captures,
        id_factory=capture_ids.__next__,
        clock=lambda: datetime(2026, 8, 31, 0, 0, tzinfo=UTC),
    )
    captures = tuple(
        store.create_capture(
            _snapshot(),
            cwd=tmp_path,
            title=title,
        )
        for title in ("第一张", "第二张", "第三张")
    )
    session_repository.finish(
        paths,
        "completed",
        ended_at=datetime(2026, 8, 31, 0, 1, tzinfo=UTC),
    )
    return evidence_app(tmp_path), paths, captures


def make_completed_empty_session(tmp_path: Path) -> SessionRepository:
    repository = SessionRepository(tmp_path / ".csbox" / "sessions")
    paths = repository.create_starting(
        "空 Capture 会话",
        shell="bash",
        shell_version=None,
        size=TerminalSize(columns=80, rows=24),
        cwd=tmp_path,
        session_id="empty-session",
        started_at=datetime(2026, 8, 31, 0, 0, tzinfo=UTC),
    )
    repository.finish(
        paths,
        "completed",
        ended_at=datetime(2026, 8, 31, 0, 1, tzinfo=UTC),
    )
    return repository


def make_running_session(tmp_path: Path) -> SessionRepository:
    repository = SessionRepository(tmp_path / ".csbox" / "sessions")
    repository.create_starting(
        "仍在录制的会话",
        shell="bash",
        shell_version=None,
        size=TerminalSize(columns=80, rows=24),
        cwd=tmp_path,
        session_id="running-session",
        started_at=datetime(2026, 8, 31, 0, 0, tzinfo=UTC),
    )
    return repository


class GuardedSessionRepository(SessionRepository):
    def list_summaries(self) -> tuple[object, ...]:
        raise AssertionError("Evidence Set list must not resolve Lab sessions")

    def list_sessions(self) -> tuple[object, ...]:
        raise AssertionError("Evidence Set list must not enumerate Lab sessions")

    def read_summary(self, session_id: str) -> object:
        raise AssertionError(f"Evidence Set list must not read {session_id}")


async def add_capture(app: CSBoxApp, pilot: object, index: int) -> None:
    await pilot.press("a")
    await pilot.pause()
    assert isinstance(app.screen, EvidenceCaptureBrowserScreen)
    await pilot.press("enter")
    await pilot.pause()
    for _ in range(index):
        await pilot.press("down")
    await pilot.press("enter")
    await pilot.pause()
    assert isinstance(app.screen, EvidenceSetEditorScreen)


async def open_editor_with_first_capture(app: CSBoxApp, pilot: object) -> None:
    await open_empty_editor(app, pilot)
    await add_capture(app, pilot, 0)


async def open_editor_with_three_captures(app: CSBoxApp, pilot: object) -> None:
    await open_empty_editor(app, pilot)
    for index in range(3):
        await add_capture(app, pilot, index)
    await pilot.press("up", "up")


def captures_json_bytes(tmp_path: Path) -> bytes:
    return (tmp_path / ".csbox" / "sessions" / "session-1" / "captures.json").read_bytes()


def load_first_evidence_set(tmp_path: Path) -> EvidenceSet:
    repository = EvidenceSetRepository(tmp_path / ".csbox" / "evidence")
    return repository.load(next(path.stem for path in repository.root.glob("*.json")))


def assert_static_lines_fit(screen: Widget) -> None:
    for widget in screen.query(Static):
        width = widget.content_region.width
        if width <= 0:
            continue
        for line in str(widget.renderable).splitlines():
            assert display_width(line) <= width, (
                f"{type(screen).__name__} line exceeds {width} cells: {line!r}"
            )


def assert_visible_geometry(screen: Widget) -> None:
    for widget in screen.query("*"):
        if widget is screen or not widget.visible:
            continue
        assert widget.outer_size.width > 0, f"{widget!r} has no width"
        assert widget.outer_size.height > 0, f"{widget!r} has no height"
        current = widget.parent
        has_scrollable_ancestor = False
        while current is not None:
            if current.is_scrollable:
                has_scrollable_ancestor = True
                break
            current = current.parent
        if not has_scrollable_ancestor:
            assert widget.region.x >= 0
            assert widget.region.y >= 0
            assert widget.region.right <= screen.size.width
            assert widget.region.bottom <= screen.size.height


async def wait_for_report_result_text(
    app: CSBoxApp,
    pilot: object,
    expected: str,
    *,
    timeout: float = 5.0,
) -> None:
    """Wait for the result screen's deferred layout refresh to publish text."""

    deadline = monotonic() + timeout
    while monotonic() < deadline:
        if isinstance(app.screen, ReportExportResultScreen) and expected in screen_text(app.screen):
            return
        await pilot.pause()
    assert isinstance(app.screen, ReportExportResultScreen)
    assert expected in screen_text(app.screen)


@pytest.mark.asyncio
async def test_home_evidence_entry_opens_real_list_and_returns(tmp_path: Path) -> None:
    app = evidence_app(tmp_path)

    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        app.screen.query_one("#entry-evidence", Button).focus()
        await pilot.press("enter")
        await pilot.pause()

        assert isinstance(app.screen, EvidenceSetsScreen)
        assert "暂无证据集" in screen_text(app.screen)
        assert "新建证据集" in screen_text(app.screen)
        await pilot.press("escape")
        await pilot.pause()
        assert isinstance(app.screen, HomeScreen)


@pytest.mark.asyncio
async def test_focused_enter_activates_empty_list_new_button(tmp_path: Path) -> None:
    app = evidence_app(tmp_path)

    async with app.run_test(size=(80, 24)) as pilot:
        await open_evidence_list(app, pilot)
        assert app.screen.focused is app.screen.query_one("#evidence-new", Button)

        await pilot.press("enter")
        await pilot.pause()

        assert isinstance(app.screen, EvidenceSetTitleDialog)


@pytest.mark.asyncio
async def test_focused_enter_activates_empty_editor_add_button(tmp_path: Path) -> None:
    app = evidence_app(tmp_path)

    async with app.run_test(size=(100, 30)) as pilot:
        await open_empty_editor(app, pilot)
        assert app.screen.focused is app.screen.query_one("#evidence-add", Button)

        await pilot.press("enter")
        await pilot.pause()

        assert isinstance(app.screen, EvidenceCaptureBrowserScreen)


@pytest.mark.asyncio
async def test_create_cancel_does_not_write_and_create_uses_stable_id(tmp_path: Path) -> None:
    app = evidence_app(tmp_path, evidence_id_factory=iter(("set-1",)).__next__)

    async with app.run_test(size=(100, 30)) as pilot:
        await open_evidence_list(app, pilot)
        await pilot.press("n")
        await pilot.pause()
        assert isinstance(app.screen, EvidenceSetTitleDialog)
        await pilot.press("escape")
        await pilot.pause()
        assert not tuple((tmp_path / ".csbox" / "evidence").glob("*.json"))

        await pilot.press("n")
        await pilot.pause()
        app.screen.query_one("#evidence-set-title-input", Input).value = "中文证据集"
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, EvidenceSetEditorScreen)
        assert (tmp_path / ".csbox" / "evidence" / "set-1.json").exists()


@pytest.mark.asyncio
async def test_create_failure_keeps_title_dialog_open_for_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = evidence_app(tmp_path, evidence_id_factory=iter(("set-1",)).__next__)
    original_create = app.evidence_repository.create
    attempts = 0

    def fail_once(title: str) -> EvidenceSet:
        nonlocal attempts
        if attempts == 0:
            attempts += 1
            raise EvidencePersistenceError("simulated create failure")
        return original_create(title)

    monkeypatch.setattr(app.evidence_repository, "create", fail_once)

    async with app.run_test(size=(100, 30)) as pilot:
        await open_evidence_list(app, pilot)
        await pilot.press("n")
        await pilot.pause()
        title = "保存失败后仍保留的标题"
        app.screen.query_one("#evidence-set-title-input", Input).value = title

        await pilot.press("enter")
        await pilot.pause()

        assert isinstance(app.screen, EvidenceSetTitleDialog)
        assert app.screen.query_one("#evidence-set-title-input", Input).value == title
        assert "保存失败" in screen_text(app.screen)

        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, EvidenceSetEditorScreen)
        assert app.screen.working_set.title == title


@pytest.mark.asyncio
async def test_add_capture_copies_title_and_defaults_caption_note(tmp_path: Path) -> None:
    app, paths, captures = evidence_app_with_three_captures(tmp_path)

    async with app.run_test(size=(120, 35)) as pilot:
        await open_empty_editor(app, pilot)
        await add_capture(app, pilot, 0)

        item = app.screen.working_set.items[0]
        assert item.source.session_id == paths.root.name
        assert item.source.capture_id == captures[0].capture_id
        assert item.title == "第一张"
        assert item.caption == ""
        assert item.note == ""


@pytest.mark.asyncio
async def test_duplicate_source_is_controlled_noop(tmp_path: Path) -> None:
    app, _, _ = evidence_app_with_three_captures(tmp_path)

    async with app.run_test(size=(100, 30)) as pilot:
        await open_editor_with_first_capture(app, pilot)
        await pilot.press("a")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()

        assert isinstance(app.screen, EvidenceCaptureBrowserScreen)
        assert "已在当前证据集中" in screen_text(app.screen)
        await pilot.press("escape", "escape")
        await pilot.pause()
        assert isinstance(app.screen, EvidenceSetEditorScreen)
        assert len(app.screen.working_set.items) == 1


@pytest.mark.asyncio
async def test_edit_reorder_remove_and_reload_preserve_source(tmp_path: Path) -> None:
    app, _, _ = evidence_app_with_three_captures(tmp_path)
    source_bytes = captures_json_bytes(tmp_path)

    async with app.run_test(size=(160, 45)) as pilot:
        await open_editor_with_three_captures(app, pilot)
        await pilot.press("e")
        await pilot.pause()
        app.screen.query_one("#evidence-item-title-input", Input).value = "最终标题"
        app.screen.query_one("#evidence-item-caption-input", Input).value = "图注由用户填写"
        app.screen.query_one("#evidence-item-note-input", TextArea).text = "补充说明"
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert app.screen.working_set.items[0].title == "最终标题"
        assert app.screen.working_set.items[0].caption == "图注由用户填写"
        assert app.screen.working_set.items[0].note == "补充说明"

        await pilot.press("down")
        await pilot.press("ctrl+up")
        await pilot.press("down", "down")
        await pilot.press("delete")
        await pilot.pause()
        await pilot.press("tab", "enter")
        await pilot.pause()
        assert len(app.screen.working_set.items) == 2
        await pilot.press("escape")
        await pilot.pause()

    reopened = load_first_evidence_set(tmp_path)
    assert [item.title for item in reopened.items] == ["第二张", "最终标题"]
    assert captures_json_bytes(tmp_path) == source_bytes


@pytest.mark.asyncio
async def test_failed_save_retains_page_state_and_retry_persists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, _, _ = evidence_app_with_three_captures(tmp_path)

    async with app.run_test(size=(120, 35)) as pilot:
        await open_editor_with_first_capture(app, pilot)
        persisted_before_failure = load_first_evidence_set(tmp_path)
        original_save = app.evidence_repository.save
        calls = 0

        def fail_once(value: EvidenceSet) -> EvidenceSet:
            nonlocal calls
            if calls == 0:
                calls += 1
                raise EvidencePersistenceError("simulated disk failure")
            return original_save(value)

        monkeypatch.setattr(app.evidence_repository, "save", fail_once)
        await pilot.press("e")
        await pilot.pause()
        app.screen.query_one("#evidence-item-title-input", Input).value = "未保存标题"
        app.screen.query_one("#evidence-item-caption-input", Input).value = "未保存图注"
        app.screen.query_one("#evidence-item-note-input", TextArea).text = "未保存备注"
        await pilot.press("ctrl+s")
        await pilot.pause()

        assert isinstance(app.screen, EvidenceSetEditorScreen)
        assert app.screen.working_set.items[0].title == "未保存标题"
        assert "未保存" in screen_text(app.screen)
        assert "保存失败" in screen_text(app.screen)
        persisted = load_first_evidence_set(tmp_path)
        assert persisted.items[0].title == "第一张"
        assert persisted.items[0].caption == ""
        assert persisted.items[0].note == ""
        assert persisted.updated_at == persisted_before_failure.updated_at

        await pilot.press("r")
        await pilot.pause()
        assert app.screen.save_failed is False
        assert app.screen.working_set.items[0].caption == "未保存图注"
        persisted = load_first_evidence_set(tmp_path)
        assert persisted.items[0].title == "未保存标题"
        assert persisted.items[0].caption == "未保存图注"
        assert persisted.items[0].note == "未保存备注"


@pytest.mark.asyncio
async def test_missing_source_is_retained_and_remove_does_not_touch_lab(
    tmp_path: Path,
) -> None:
    app, paths, captures = evidence_app_with_three_captures(tmp_path)
    valid_source = EvidenceSource(
        source_type="lab_capture",
        session_id=paths.root.name,
        capture_id=captures[0].capture_id,
    )
    missing_source = EvidenceSource(
        source_type="lab_capture",
        session_id=paths.root.name,
        capture_id="missing-capture",
    )
    created = app.evidence_repository.create("含不可用来源")
    app.evidence_repository.save(
        created.model_copy(
            update={
                "items": (
                    EvidenceItem(source=valid_source, title="可用条目"),
                    EvidenceItem(source=missing_source, title="丢失条目"),
                )
            }
        )
    )
    captures_before = captures_json_bytes(tmp_path)
    metadata_before = paths.metadata.read_bytes()

    async with app.run_test(size=(120, 35)) as pilot:
        await open_saved_editor(app, pilot)
        await pilot.press("down")
        await pilot.pause()
        assert "来源不可用" in screen_text(app.screen)
        assert "移除引用" in screen_text(app.screen)

        await pilot.press("delete")
        await pilot.pause()
        await pilot.press("tab", "enter")
        await pilot.pause()
        assert isinstance(app.screen, EvidenceSetEditorScreen)
        assert len(app.screen.working_set.items) == 1

    assert paths.root.is_dir()
    assert paths.metadata.read_bytes() == metadata_before
    assert captures_json_bytes(tmp_path) == captures_before
    assert load_first_evidence_set(tmp_path).items[0].title == "可用条目"


@pytest.mark.asyncio
async def test_all_sources_unavailable_still_allows_edit_and_remove(tmp_path: Path) -> None:
    app = evidence_app(tmp_path)
    created = app.evidence_repository.create("全部不可用")
    app.evidence_repository.save(
        created.model_copy(
            update={
                "items": (
                    EvidenceItem(
                        source=EvidenceSource(
                            source_type="lab_capture",
                            session_id="gone-session",
                            capture_id="gone-capture",
                        ),
                        title="保留的展示标题",
                    ),
                )
            }
        )
    )

    async with app.run_test(size=(100, 30)) as pilot:
        await open_saved_editor(app, pilot)
        assert "来源不可用" in screen_text(app.screen)
        await pilot.press("e")
        await pilot.pause()
        app.screen.query_one("#evidence-item-title-input", Input).value = "修改后的标题"
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert isinstance(app.screen, EvidenceSetEditorScreen)
        assert app.screen.working_set.items[0].title == "修改后的标题"
        assert "移除引用" in screen_text(app.screen)

        await pilot.press("delete")
        await pilot.pause()
        await pilot.press("tab", "enter")
        await pilot.pause()
        assert app.screen.working_set.items == ()
        assert "尚未添加证据" in screen_text(app.screen)


@pytest.mark.asyncio
async def test_corrupt_and_unknown_sets_are_isolated_with_beginner_action(
    tmp_path: Path,
) -> None:
    evidence_root = tmp_path / ".csbox" / "evidence"
    evidence_root.mkdir(parents=True)
    (evidence_root / "broken-set.json").write_text("{bad", encoding="utf-8")
    (evidence_root / "future-set.json").write_text(
        '{"version": 99, "id": "future-set"}', encoding="utf-8"
    )
    app = evidence_app(tmp_path)

    async with app.run_test(size=(80, 24)) as pilot:
        await open_evidence_list(app, pilot)
        text = screen_text(app.screen)
        assert "不可读取" in text
        assert "按 N 新建证据集" in text
        assert "Traceback" not in text
        assert app.screen.query_one("#evidence-open", Button).disabled
        assert app.screen.focused is app.screen.query_one("#evidence-new", Button)


@pytest.mark.asyncio
async def test_evidence_set_list_does_not_resolve_lab_sessions(tmp_path: Path) -> None:
    normal_repository = EvidenceSetRepository(tmp_path / ".csbox" / "evidence")
    normal_repository.create("只看集合 metadata")
    guarded_repository = GuardedSessionRepository(tmp_path / ".csbox" / "sessions")
    app = evidence_app(tmp_path, session_repository=guarded_repository)

    async with app.run_test(size=(100, 30)) as pilot:
        await open_evidence_list(app, pilot)
        assert "只看集合 metadata" in screen_text(app.screen)


@pytest.mark.asyncio
async def test_duplicate_set_titles_keep_id_based_selection(tmp_path: Path) -> None:
    repository = EvidenceSetRepository(
        tmp_path / ".csbox" / "evidence",
        id_factory=iter(("set-a", "set-b")).__next__,
        clock=lambda: datetime(2026, 8, 31, 0, 0, tzinfo=UTC),
    )
    repository.create("同名证据集")
    repository.create("同名证据集")
    app = evidence_app(tmp_path, evidence_repository=repository)

    async with app.run_test(size=(100, 30)) as pilot:
        await open_evidence_list(app, pilot)
        assert app.screen.selected_evidence_set_id == "set-a"
        await pilot.press("down")
        assert app.screen.selected_evidence_set_id == "set-b"


@pytest.mark.asyncio
async def test_running_session_is_visible_but_not_addable(tmp_path: Path) -> None:
    session_repository = make_running_session(tmp_path)
    app = evidence_app(tmp_path, session_repository=session_repository)

    async with app.run_test(size=(100, 30)) as pilot:
        await open_empty_editor(app, pilot)
        await pilot.press("a")
        await pilot.pause()
        assert isinstance(app.screen, EvidenceCaptureBrowserScreen)
        assert "尚未结束" in screen_text(app.screen)
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, EvidenceCaptureBrowserScreen)
        assert "不能添加 Capture" in screen_text(app.screen)


@pytest.mark.asyncio
async def test_completed_session_without_captures_explains_next_step(
    tmp_path: Path,
) -> None:
    session_repository = make_completed_empty_session(tmp_path)
    app = evidence_app(tmp_path, session_repository=session_repository)

    async with app.run_test(size=(100, 30)) as pilot:
        await open_empty_editor(app, pilot)
        await pilot.press("a")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, EvidenceCaptureBrowserScreen)
        assert "没有可用 Capture" in screen_text(app.screen)
        assert "按 Esc 返回" in screen_text(app.screen)


@pytest.mark.asyncio
async def test_blank_capture_title_requires_user_title(tmp_path: Path) -> None:
    session_repository = SessionRepository(tmp_path / ".csbox" / "sessions")
    paths = session_repository.create_starting(
        "空标题实验",
        shell="bash",
        shell_version=None,
        size=TerminalSize(columns=80, rows=24),
        cwd=tmp_path,
        session_id="blank-title-session",
        started_at=datetime(2026, 8, 31, 0, 0, tzinfo=UTC),
    )
    CaptureStore(paths.captures, id_factory=lambda: "blank-capture").create_capture(
        _snapshot(), cwd=tmp_path, title=""
    )
    session_repository.finish(
        paths,
        "completed",
        ended_at=datetime(2026, 8, 31, 0, 1, tzinfo=UTC),
    )
    app = evidence_app(tmp_path, session_repository=session_repository)

    async with app.run_test(size=(100, 30)) as pilot:
        await open_empty_editor(app, pilot)
        await pilot.press("a")
        await pilot.pause()
        await pilot.press("enter", "enter")
        await pilot.pause()
        assert isinstance(app.screen, EvidenceItemDialog)
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert "请输入 Evidence Item 标题" in screen_text(app.screen)
        app.screen.query_one("#evidence-item-title-input", Input).value = "用户填写标题"
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert isinstance(app.screen, EvidenceSetEditorScreen)
        assert app.screen.working_set.items[0].title == "用户填写标题"
        assert app.screen.working_set.items[0].caption == ""
        assert app.screen.working_set.items[0].note == ""


@pytest.mark.asyncio
@pytest.mark.parametrize("size", ((80, 24), (100, 30), (120, 35), (160, 45)))
async def test_evidence_screens_fit_required_cjk_viewports(
    tmp_path: Path, size: tuple[int, int]
) -> None:
    app, paths, captures = evidence_app_with_three_captures(tmp_path)
    created = app.evidence_repository.create("中文 Evidence Set 标题很长用于响应式检查 🧪")
    app.evidence_repository.save(
        created.model_copy(
            update={
                "items": (
                    EvidenceItem(
                        source=EvidenceSource(
                            source_type="lab_capture",
                            session_id=paths.root.name,
                            capture_id=captures[0].capture_id,
                        ),
                        title="中文 Capture 展示标题很长",
                        caption="这是用户填写的很长图注，用来检查窄窗口中的 display-cell 换行。",
                        note="第一行补充说明\n第二行包含中文和 emoji 🧪。",
                    ),
                )
            }
        )
    )

    async with app.run_test(size=size) as pilot:
        await open_evidence_list(app, pilot)
        assert_visible_geometry(app.screen)
        assert_static_lines_fit(app.screen)
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, EvidenceSetEditorScreen)
        assert app.screen.has_class("narrow") is (size[0] < 120)
        assert app.screen.has_class("wide") is (size[0] >= 120)
        assert_visible_geometry(app.screen)
        assert_static_lines_fit(app.screen)

        await pilot.press("e")
        await pilot.pause()
        assert isinstance(app.screen, EvidenceItemDialog)
        assert_visible_geometry(app.screen)
        assert_static_lines_fit(app.screen)
        await pilot.press("escape")
        await pilot.pause()

        await pilot.press("a")
        await pilot.pause()
        assert isinstance(app.screen, EvidenceCaptureBrowserScreen)
        assert_visible_geometry(app.screen)
        assert_static_lines_fit(app.screen)
        await pilot.press("enter")
        await pilot.pause()
        assert_visible_geometry(app.screen)
        assert_static_lines_fit(app.screen)
        await pilot.press("escape", "escape")
        await pilot.pause()
        assert isinstance(app.screen, EvidenceSetEditorScreen)


def _fake_report_action(
    calls: list[tuple[Path, bool]],
    *,
    fail_first: bool = False,
) -> object:
    attempts = 0

    def export(
        evidence_set: EvidenceSet,
        destination: Path,
        *,
        force: bool,
        phase_callback: object = None,
    ) -> ReportExportResult:
        nonlocal attempts
        attempts += 1
        calls.append((destination, force))
        if phase_callback is not None:
            phase_callback(ReportExportPhase.GENERATING)  # type: ignore[operator]
        if fail_first and attempts == 1:
            raise ReportExportError(
                "报告材料导出失败，请检查目标目录后重试。",
                destination=destination,
            )
        if phase_callback is not None:
            phase_callback(ReportExportPhase.PUBLISHING)  # type: ignore[operator]
        destination.mkdir(parents=True, exist_ok=True)
        (destination / "report.md").write_text("# result", encoding="utf-8")
        (destination / "report.docx").write_bytes(b"docx")
        images = tuple(
            destination / "assets" / f"{index:02d}.png"
            for index, _ in enumerate(
                evidence_set.items,
                start=1,
            )
        )
        (destination / "assets").mkdir(exist_ok=True)
        for image in images:
            image.write_bytes(b"png")
        if phase_callback is not None:
            phase_callback(ReportExportPhase.COMPLETE)  # type: ignore[operator]
        return ReportExportResult(
            destination=destination,
            markdown=destination / "report.md",
            docx=destination / "report.docx",
            images=images,
            evidence_item_count=len(evidence_set.items),
        )

    return export


@pytest.mark.asyncio
async def test_report_export_keyboard_flow_shows_exact_result_paths(tmp_path: Path) -> None:
    app, _paths, _captures = evidence_app_with_three_captures(tmp_path)
    calls: list[tuple[Path, bool]] = []
    app.report_export_action = _fake_report_action(calls)  # type: ignore[assignment]

    async with app.run_test(size=(140, 40)) as pilot:
        await open_editor_with_first_capture(app, pilot)
        app.screen.query_one("#evidence-export", Button).focus()
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, ReportExportDialog)

        destination = tmp_path / "中文 报告输出"
        app.screen.query_one("#report-destination-input", Input).value = str(destination)
        await pilot.press("enter")
        await pilot.pause(0.2)
        await pilot.pause()

        assert isinstance(app.screen, ReportExportResultScreen)
        text = screen_text(app.screen)
        path_text = text.replace("\n", "")
        assert "导出完成" in text
        assert f"Markdown：{destination / 'report.md'}" in path_text
        assert f"DOCX：{destination / 'report.docx'}" in path_text
        assert "Images：1" in text
        assert "Evidence items：1" in text
        assert calls == [(destination, False)]
        assert_visible_geometry(app.screen)
        assert_static_lines_fit(app.screen)


@pytest.mark.asyncio
async def test_report_export_existing_target_cancel_does_not_write(tmp_path: Path) -> None:
    app, _paths, _captures = evidence_app_with_three_captures(tmp_path)
    calls: list[tuple[Path, bool]] = []
    app.report_export_action = _fake_report_action(calls)  # type: ignore[assignment]
    destination = tmp_path / "已有报告目录"
    destination.mkdir()
    marker = destination / "keep.txt"
    marker.write_text("keep", encoding="utf-8")

    async with app.run_test(size=(120, 35)) as pilot:
        await open_editor_with_first_capture(app, pilot)
        app.screen.query_one("#evidence-export", Button).focus()
        await pilot.press("enter")
        await pilot.pause()
        app.screen.query_one("#report-destination-input", Input).value = str(destination)
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, ReportExportOverwriteDialog)
        await pilot.press("escape")
        await pilot.pause()
        assert isinstance(app.screen, EvidenceSetEditorScreen)
        assert calls == []
        assert marker.read_text(encoding="utf-8") == "keep"


@pytest.mark.asyncio
async def test_report_export_failure_retries_same_destination_and_force_choice(
    tmp_path: Path,
) -> None:
    app, _paths, _captures = evidence_app_with_three_captures(tmp_path)
    calls: list[tuple[Path, bool]] = []
    app.report_export_action = _fake_report_action(calls, fail_first=True)  # type: ignore[assignment]
    destination = tmp_path / "retry destination"

    async with app.run_test(size=(140, 40)) as pilot:
        await open_editor_with_first_capture(app, pilot)
        app.screen.query_one("#evidence-export", Button).focus()
        await pilot.press("enter")
        await pilot.pause()
        app.screen.query_one("#report-destination-input", Input).value = str(destination)
        await pilot.press("enter")
        await pilot.pause(0.2)
        await pilot.pause()
        assert isinstance(app.screen, ReportExportResultScreen)
        assert "导出失败" in screen_text(app.screen)
        assert str(destination) in screen_text(app.screen) or "重试" in screen_text(app.screen)

        await pilot.press("enter")
        await pilot.pause(0.2)
        await pilot.pause()
        assert isinstance(app.screen, ReportExportResultScreen)
        assert "导出完成" in screen_text(app.screen)
        assert calls == [(destination, False), (destination, False)]


@pytest.mark.asyncio
@pytest.mark.parametrize("size", ((80, 24), (100, 30), (120, 35), (160, 45)))
async def test_report_export_dialog_and_result_fit_cjk_viewports(
    tmp_path: Path,
    size: tuple[int, int],
) -> None:
    app, _paths, _captures = evidence_app_with_three_captures(tmp_path)
    calls: list[tuple[Path, bool]] = []
    app.report_export_action = _fake_report_action(calls)  # type: ignore[assignment]

    async with app.run_test(size=size) as pilot:
        await open_editor_with_first_capture(app, pilot)
        app.screen.query_one("#evidence-export", Button).focus()
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, ReportExportDialog)
        assert_visible_geometry(app.screen)
        assert_static_lines_fit(app.screen)
        await pilot.press("escape")
        await pilot.pause()
        assert isinstance(app.screen, EvidenceSetEditorScreen)


@pytest.mark.asyncio
async def test_report_export_result_fits_narrow_cjk_viewport(tmp_path: Path) -> None:
    app, _paths, _captures = evidence_app_with_three_captures(tmp_path)
    calls: list[tuple[Path, bool]] = []
    app.report_export_action = _fake_report_action(calls)  # type: ignore[assignment]

    async with app.run_test(size=(80, 24)) as pilot:
        await open_editor_with_first_capture(app, pilot)
        await pilot.press("p")
        await pilot.pause()
        assert isinstance(app.screen, ReportExportDialog)
        await pilot.press("enter")
        await pilot.pause(0.2)
        await pilot.pause()
        assert isinstance(app.screen, ReportExportResultScreen)
        assert_visible_geometry(app.screen)
        assert_static_lines_fit(app.screen)


@pytest.mark.asyncio
async def test_realistic_report_handoff_uses_configured_renderer_and_publishes_bundle(
    tmp_path: Path,
) -> None:
    app, paths, captures = evidence_app_with_three_captures(tmp_path)
    created = app.evidence_repository.create("计算机网络实验一")
    app.evidence_repository.save(
        created.model_copy(
            update={
                "items": (
                    EvidenceItem(
                        source=EvidenceSource(
                            source_type="lab_capture",
                            session_id=paths.root.name,
                            capture_id=captures[0].capture_id,
                        ),
                        title="查看网络接口",
                    ),
                    EvidenceItem(
                        source=EvidenceSource(
                            source_type="lab_capture",
                            session_id=paths.root.name,
                            capture_id=captures[1].capture_id,
                        ),
                        title="查看路由",
                        caption="自定义路由输出",
                    ),
                    EvidenceItem(
                        source=EvidenceSource(
                            source_type="lab_capture",
                            session_id=paths.root.name,
                            capture_id=captures[2].capture_id,
                        ),
                        title="DNS 查询",
                        note="第一行备注\n第二行备注",
                    ),
                )
            }
        )
    )
    destination = tmp_path / "中文 报告材料"
    ReportProfileRepository.from_cwd(tmp_path).save(
        created.evidence_set_id,
        ReportProfile(
            report_title="我的课程报告",
            sections=(
                ReportSection(
                    heading="用户章节",
                    body="用户填写的正文",
                    include_evidence=True,
                ),
            ),
        ),
    )
    source_files = (paths.captures, paths.metadata, paths.cast)
    evidence_file = tmp_path / ".csbox" / "evidence" / f"{created.evidence_set_id}.json"
    source_before = {path: path.read_bytes() for path in source_files}
    evidence_before = evidence_file.read_bytes()

    async with app.run_test(size=(140, 40)) as pilot:
        await open_saved_editor(app, pilot)
        await pilot.press("p")
        await pilot.pause()
        assert isinstance(app.screen, ReportExportDialog)
        app.screen.query_one("#report-destination-input", Input).value = str(destination)
        await pilot.press("enter")
        await pilot.pause(0.3)
        await pilot.pause()

        assert isinstance(app.screen, ReportExportResultScreen)
        await wait_for_report_result_text(app, pilot, "导出完成")

    markdown = (destination / "report.md").read_text(encoding="utf-8")
    assert markdown.startswith("# 我的课程报告\n")
    assert "## 用户章节" in markdown
    assert "用户填写的正文" in markdown
    assert "图 1 查看网络接口" in markdown
    assert "图 2 自定义路由输出" in markdown
    assert "第一行备注<br>第二行备注" in markdown
    assert len(tuple((destination / "assets").glob("*.png"))) == 3
    assert (destination / "report.docx").is_file()
    assert {path: path.read_bytes() for path in source_files} == source_before
    assert evidence_file.read_bytes() == evidence_before


@pytest.mark.asyncio
async def test_report_profile_dialog_saves_metadata_and_authored_section(
    tmp_path: Path,
) -> None:
    app, _paths, _captures = evidence_app_with_three_captures(tmp_path)

    async with app.run_test(size=(120, 35)) as pilot:
        await open_editor_with_first_capture(app, pilot)
        evidence_set_id = app.screen.working_set.evidence_set_id  # type: ignore[attr-defined]
        await pilot.press("c")
        await pilot.pause()
        assert isinstance(app.screen, ReportProfileDialog)

        app.screen.query_one("#report-profile-course-name-input", Input).value = "计算机网络"
        app.screen.query_one("#report-profile-section-1-heading-input", Input).value = "实验目的"
        app.screen.query_one(
            "#report-profile-section-1-body-input", TextArea
        ).text = "用户填写的目的"
        app.screen.query_one("#report-profile-section-1-evidence-input", Checkbox).value = False
        await pilot.click("#report-profile-save")
        await pilot.pause()

        assert isinstance(app.screen, EvidenceSetEditorScreen)
        saved = ReportProfileRepository.from_cwd(tmp_path).load(evidence_set_id)
        assert saved.course_name == "计算机网络"
        assert saved.sections[0].heading == "实验目的"
        assert saved.sections[0].body == "用户填写的目的"


@pytest.mark.asyncio
async def test_report_profile_cancel_does_not_write(tmp_path: Path) -> None:
    app, _paths, _captures = evidence_app_with_three_captures(tmp_path)

    async with app.run_test(size=(100, 30)) as pilot:
        await open_editor_with_first_capture(app, pilot)
        evidence_set_id = app.screen.working_set.evidence_set_id  # type: ignore[attr-defined]
        await pilot.press("c")
        await pilot.pause()
        assert isinstance(app.screen, ReportProfileDialog)
        app.screen.query_one("#report-profile-course-name-input", Input).value = "不应保存"
        await pilot.press("escape")
        await pilot.pause()

        assert isinstance(app.screen, EvidenceSetEditorScreen)
        assert not ReportProfileRepository.from_cwd(tmp_path).path_for(evidence_set_id).exists()


@pytest.mark.asyncio
async def test_report_export_with_invalid_profile_is_retryable(
    tmp_path: Path,
) -> None:
    app, _paths, _captures = evidence_app_with_three_captures(tmp_path)

    async with app.run_test(size=(100, 30)) as pilot:
        await open_editor_with_first_capture(app, pilot)
        evidence_set_id = app.screen.working_set.evidence_set_id  # type: ignore[attr-defined]
        profile_repository = ReportProfileRepository.from_cwd(tmp_path)
        profile_path = profile_repository.path_for(evidence_set_id)
        profile_path.parent.mkdir(parents=True)
        profile_path.write_text('{"version": 99}', encoding="utf-8")

        await pilot.press("p")
        await pilot.pause()
        assert isinstance(app.screen, ReportExportDialog)
        await pilot.press("enter")
        await pilot.pause()

        assert isinstance(app.screen, ReportExportResultScreen)
        assert "报告结构不可读取" in screen_text(app.screen)
        assert not (tmp_path / f"{evidence_set_id}-report").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("size", ((80, 24), (100, 30), (120, 35)))
async def test_report_profile_dialog_fits_cjk_viewports(
    tmp_path: Path,
    size: tuple[int, int],
) -> None:
    app, _paths, _captures = evidence_app_with_three_captures(tmp_path)

    async with app.run_test(size=size) as pilot:
        await open_editor_with_first_capture(app, pilot)
        await pilot.press("c")
        await pilot.pause()
        assert isinstance(app.screen, ReportProfileDialog)
        assert_visible_geometry(app.screen)
        assert_static_lines_fit(app.screen)
