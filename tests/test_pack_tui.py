from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from time import monotonic
from typing import Any
from zipfile import ZipFile

import pytest
from textual.widgets import Button, Input, Static

from csbox.core.display_width import display_width
from csbox.core.models import EnvironmentSnapshot
from csbox.lab.fake_data import FakeHomeDataSource
from csbox.lab.home_data import RealHomeDataSource
from csbox.lab.repository import SessionRepository
from csbox.locales import load_locale
from csbox.pack.models import PackReport
from csbox.pack.service import PackService, PackServiceError
from csbox.tui.app import CSBoxApp
from csbox.tui.screens.pack import PackConfirmationScreen, PackOverwriteDialog, PackResultScreen


def _screen_text(screen: Any) -> str:
    widgets = screen.query(Static)
    buttons = screen.query(Button)
    return "\n".join(
        [
            *(str(widget.renderable) for widget in widgets),
            *(str(button.label) for button in buttons),
            *(input_widget.value for input_widget in screen.query(Input)),
        ]
    )


async def _wait_until(pilot: Any, predicate, *, timeout: float = 5.0) -> None:
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
        is_wsl=True,
        terminal_columns=80,
        terminal_rows=24,
    )


@dataclass(frozen=True)
class PackPlan:
    source_root: Path
    included: tuple[str, ...] = ("README.md", "src/main.py")
    excluded: tuple[str, ...] = (".git:directory", ".env:pattern")
    rejected: tuple[str, ...] = ()
    source_bytes: int = 128
    project_type: str = "python"
    destination: Path | None = None
    output_exists: bool = False
    force: bool = False
    verification_requested: bool = True


def _destination_plan(plan: PackPlan, destination: Path | None = None) -> PackPlan:
    output = destination or plan.destination or (plan.source_root / "student-project-course.zip")
    return replace(
        plan,
        destination=output,
        output_exists=output.exists(),
    )


@pytest.mark.asyncio
async def test_home_pack_entry_opens_confirmation_and_escape_has_no_side_effect(
    tmp_path: Path,
) -> None:
    calls: list[str] = []
    app = CSBoxApp(
        data_source=FakeHomeDataSource(),
        environment=_environment(),
        locale=load_locale(),
        pack_plan_factory=lambda: PackPlan(tmp_path),
        pack_action=lambda _plan: calls.append("packed"),
    )

    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        app.screen.query_one("#entry-pack").focus()
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, PackConfirmationScreen)
        assert "included" in _screen_text(app.screen)
        assert "README.md" in _screen_text(app.screen)
        await pilot.press("escape")
        await pilot.pause()
        assert calls == []


@pytest.mark.asyncio
async def test_pack_confirmation_requires_enter_before_pack_action(tmp_path: Path) -> None:
    calls: list[str] = []
    app = CSBoxApp(
        data_source=FakeHomeDataSource(),
        environment=_environment(),
        locale=load_locale(),
        pack_plan_factory=lambda: PackPlan(tmp_path),
        pack_action=lambda _plan: calls.append("packed"),
    )

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        app.screen.query_one("#entry-pack").focus()
        await pilot.press("enter")
        await pilot.pause()
        app.screen.query_one("#pack-confirm").focus()
        await pilot.press("enter")
        await pilot.pause()
        assert calls == ["packed"]
        assert "打包完成" in _screen_text(app.screen)
        assert "未返回交付报告" in _screen_text(app.screen)


@pytest.mark.asyncio
async def test_pack_rejected_plan_has_actionable_error_and_never_leaks_values(
    tmp_path: Path,
) -> None:
    secret = "CSBOX_SECRET_SENTINEL_pack_tui"
    plan = PackPlan(tmp_path, rejected=(f"secrets/{secret}:private-key",))
    app = CSBoxApp(
        data_source=FakeHomeDataSource(),
        environment=_environment(),
        locale=load_locale(),
        pack_plan_factory=lambda: plan,
    )

    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        app.screen.query_one("#entry-pack").focus()
        await pilot.press("enter")
        await pilot.pause()
        text = _screen_text(app.screen)
        assert "拒绝" in text
        assert "关键类型" in text
        assert "Enter" in text
        assert secret not in text


@pytest.mark.asyncio
async def test_pack_confirmation_wraps_cjk_items_at_narrow_width(tmp_path: Path) -> None:
    plan = PackPlan(
        tmp_path,
        included=("课程实验/" + "中文路径" * 40,),
        excluded=("构建产物/" + "输出" * 40,),
    )
    app = CSBoxApp(
        data_source=FakeHomeDataSource(),
        environment=_environment(),
        locale=load_locale(),
        pack_plan_factory=lambda: plan,
    )

    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        app.screen.query_one("#entry-pack").focus()
        await pilot.press("enter")
        await pilot.pause()
        panel = app.screen.query_one("#pack-items")
        assert all(
            display_width(line) <= panel.size.width - 2
            for line in str(panel.renderable).splitlines()
        )


@pytest.mark.asyncio
async def test_real_pack_adapter_uses_plan_and_publishes_only_after_enter(tmp_path: Path) -> None:
    source = tmp_path / "中文项目"
    source.mkdir()
    (source / "README.md").write_text("# demo\n", encoding="utf-8")
    repository = SessionRepository(source / ".csbox" / "sessions")
    app = CSBoxApp(
        data_source=RealHomeDataSource(repository, source),
        environment=_environment(),
        locale=load_locale(),
    )

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        app.screen.query_one("#entry-pack").focus()
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, PackConfirmationScreen)
        assert "README.md" in _screen_text(app.screen)
        assert not tuple(source.glob("*.zip"))
        app.screen.query_one("#pack-confirm").focus()
        await pilot.press("enter")
        await _wait_until(pilot, lambda: "打包完成" in _screen_text(app.screen))

    archives = tuple(source.glob("*.zip"))
    assert len(archives) == 1


@pytest.mark.asyncio
async def test_pack_preview_exposes_destination_and_restore_default_keeps_full_target(
    tmp_path: Path,
) -> None:
    source = tmp_path / "中文项目"
    source.mkdir()
    default = source / "student-project-course.zip"
    custom = tmp_path / "课程 提交" / "最终 版本 中文.zip"
    base_plan = PackPlan(source, destination=default)
    requested: list[Path | None] = []

    def plan_factory(destination: Path | None = None) -> PackPlan:
        requested.append(destination)
        return _destination_plan(base_plan, destination)

    app = CSBoxApp(
        data_source=FakeHomeDataSource(),
        environment=_environment(),
        locale=load_locale(),
        pack_plan_factory=plan_factory,
    )

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        app.screen.query_one("#entry-pack").focus()
        await pilot.press("enter")
        await pilot.pause()

        destination_input = app.screen.query_one("#pack-destination-input", Input)
        assert destination_input.value == str(default)
        preview = _screen_text(app.screen)
        assert "输出" in preview
        assert "included" in preview
        assert "excluded" in preview
        assert "rejected" in preview
        assert str(default) in preview

        destination_input.focus()
        destination_input.value = str(custom)
        await pilot.press("enter")
        await pilot.pause()
        assert requested[-1] == custom
        assert str(custom) in _screen_text(app.screen)

        await pilot.click("#pack-restore-default")
        await pilot.pause()
        assert destination_input.value == str(default)
        assert str(default) in _screen_text(app.screen)


@pytest.mark.asyncio
async def test_pack_preview_cancel_never_submits_custom_destination(tmp_path: Path) -> None:
    source = tmp_path / "项目 with spaces"
    source.mkdir()
    custom = tmp_path / "提交" / "结果.zip"
    plan = PackPlan(source, destination=source / "student-project-course.zip")
    packed: list[object] = []

    app = CSBoxApp(
        data_source=FakeHomeDataSource(),
        environment=_environment(),
        locale=load_locale(),
        pack_plan_factory=lambda destination=None: _destination_plan(plan, destination),
        pack_action=packed.append,
    )

    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        app.screen.query_one("#entry-pack").focus()
        await pilot.press("enter")
        await pilot.pause()
        destination_input = app.screen.query_one("#pack-destination-input", Input)
        destination_input.value = str(custom)
        await pilot.click("#pack-cancel")
        await pilot.pause()

        assert packed == []


@pytest.mark.asyncio
async def test_pack_destination_can_be_updated_and_confirmed_by_keyboard(tmp_path: Path) -> None:
    source = tmp_path / "项目"
    source.mkdir()
    destination = tmp_path / "课程 提交" / "最终.zip"
    plan = PackPlan(source, destination=source / "student-project-course.zip")
    packed: list[object] = []

    app = CSBoxApp(
        data_source=FakeHomeDataSource(),
        environment=_environment(),
        locale=load_locale(),
        pack_plan_factory=lambda destination=None: _destination_plan(plan, destination),
        pack_action=packed.append,
    )

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        app.screen.query_one("#entry-pack").focus()
        await pilot.press("enter")
        await pilot.pause()
        destination_input = app.screen.query_one("#pack-destination-input", Input)
        destination_input.value = str(destination)
        await pilot.press("enter")
        await pilot.pause()

        assert app.screen.focused is app.screen.query_one("#pack-confirm")
        assert app.screen.destination == destination.absolute()
        await pilot.press("enter")
        await pilot.pause()

        assert len(packed) == 1
        assert packed[0].destination == destination.absolute()


@pytest.mark.asyncio
async def test_pack_destination_controls_support_tab_navigation(tmp_path: Path) -> None:
    app = CSBoxApp(
        data_source=FakeHomeDataSource(),
        environment=_environment(),
        locale=load_locale(),
        pack_plan_factory=lambda: PackPlan(tmp_path),
    )

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        app.screen.query_one("#entry-pack").focus()
        await pilot.press("enter")
        await pilot.pause()
        assert app.screen.focused is not None
        assert app.screen.focused.id == "pack-destination-input"

        await pilot.press("tab")
        assert app.screen.focused is not None
        assert app.screen.focused.id == "pack-edit-destination"
        await pilot.press("shift+tab")
        assert app.screen.focused is not None
        assert app.screen.focused.id == "pack-destination-input"


@pytest.mark.asyncio
async def test_existing_target_requires_confirmation_and_cancel_retains_destination(
    tmp_path: Path,
) -> None:
    source = tmp_path / "project"
    source.mkdir()
    destination = tmp_path / "提交.zip"
    destination.write_bytes(b"keep")
    plan = PackPlan(source, destination=destination)
    packed: list[object] = []

    app = CSBoxApp(
        data_source=FakeHomeDataSource(),
        environment=_environment(),
        locale=load_locale(),
        pack_plan_factory=lambda destination=None: _destination_plan(plan, destination),
        pack_action=packed.append,
    )

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        app.screen.query_one("#entry-pack").focus()
        await pilot.press("enter")
        await pilot.pause()
        app.screen.query_one("#pack-confirm").focus()
        await pilot.press("enter")
        await pilot.pause()

        assert isinstance(app.screen, PackOverwriteDialog)
        assert str(destination) in _screen_text(app.screen).replace("\n", "")
        assert packed == []

        await pilot.press("escape")
        await pilot.pause()
        assert isinstance(app.screen, PackConfirmationScreen)
        assert app.screen.query_one("#pack-destination-input", Input).value == str(destination)

        app.screen.query_one("#pack-confirm").focus()
        while app.screen.query_one("#pack-confirm").has_class("-active"):
            await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        await pilot.click("#pack-overwrite-confirm")
        await pilot.pause()

        assert len(packed) == 1
        assert packed[0].force is True
        assert destination.read_bytes() == b"keep"


@pytest.mark.asyncio
async def test_pack_race_conflict_opens_same_overwrite_recovery_dialog(tmp_path: Path) -> None:
    source = tmp_path / "project"
    source.mkdir()
    plan = PackPlan(source, destination=source / "student-project-course.zip")

    def race(_plan: object) -> None:
        raise FileExistsError("destination appeared during publish")

    app = CSBoxApp(
        data_source=FakeHomeDataSource(),
        environment=_environment(),
        locale=load_locale(),
        pack_plan_factory=lambda destination=None: _destination_plan(plan, destination),
        pack_action=race,
    )

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        app.screen.query_one("#entry-pack").focus()
        await pilot.press("enter")
        await pilot.pause()
        await pilot.click("#pack-confirm")
        await _wait_until(pilot, lambda: isinstance(app.screen, PackOverwriteDialog))
        assert "目标文件已存在" in _screen_text(app.screen)
        await pilot.press("escape")
        await pilot.pause()
        assert isinstance(app.screen, PackConfirmationScreen)
        assert "已取消覆盖" in _screen_text(app.screen)


@pytest.mark.asyncio
async def test_real_pack_race_target_is_recoverable_with_exact_force_publish(
    tmp_path: Path,
) -> None:
    source = tmp_path / "项目"
    source.mkdir()
    (source / "README.md").write_text("# 项目\n", encoding="utf-8")
    destination = tmp_path / "课程 提交" / "最终.zip"
    service = PackService()

    def plan_factory(destination_value: Path | None = None) -> object:
        return service.plan(source, destination=destination_value or destination)

    app = CSBoxApp(
        data_source=RealHomeDataSource(
            SessionRepository(source / ".csbox" / "sessions"),
            source,
        ),
        environment=_environment(),
        locale=load_locale(),
        pack_plan_factory=plan_factory,
        pack_action=lambda plan: service.pack_plan(plan, verify=True),
    )

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        app.screen.query_one("#entry-pack").focus()
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, PackConfirmationScreen)
        destination.parent.mkdir(parents=True)
        destination.write_bytes(b"user archive")
        sibling = destination.parent / "其他.zip"
        sibling.write_bytes(b"keep sibling")

        await pilot.click("#pack-confirm")
        await pilot.pause()
        assert isinstance(app.screen, PackOverwriteDialog)
        await pilot.click("#pack-overwrite-confirm")
        await pilot.pause()

        assert isinstance(app.screen, PackResultScreen)
        assert destination.read_bytes() != b"user archive"
        assert sibling.read_bytes() == b"keep sibling"


@pytest.mark.asyncio
async def test_pack_overwrite_dialog_keeps_long_path_and_actions_visible(tmp_path: Path) -> None:
    source = tmp_path / "project"
    source.mkdir()
    long_parent = tmp_path.joinpath(*("中文" * 40 for _ in range(8)))
    long_parent.mkdir(parents=True)
    destination = long_parent / "提交.zip"
    destination.write_bytes(b"keep")
    plan = PackPlan(source, destination=destination)
    app = CSBoxApp(
        data_source=FakeHomeDataSource(),
        environment=_environment(),
        locale=load_locale(),
        pack_plan_factory=lambda destination=None: _destination_plan(plan, destination),
        pack_action=lambda _plan: None,
    )

    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        app.screen.query_one("#entry-pack").focus()
        await pilot.press("enter")
        await pilot.pause()
        await pilot.click("#pack-confirm")
        await pilot.pause()

        assert isinstance(app.screen, PackOverwriteDialog)
        scroll = app.screen.query_one("#pack-overwrite-scroll")
        confirm = app.screen.query_one("#pack-overwrite-confirm", Button)
        assert scroll.is_scrollable
        assert confirm.visible
        assert confirm.region.bottom <= 24
        await pilot.press("escape")
        await pilot.pause()
        assert isinstance(app.screen, PackConfirmationScreen)


@pytest.mark.asyncio
async def test_invalid_directory_destination_can_be_corrected_without_leaving_pack_flow(
    tmp_path: Path,
) -> None:
    source = tmp_path / "项目"
    source.mkdir()
    invalid = tmp_path / "输出目录"
    invalid.mkdir()
    corrected = tmp_path / "输出目录" / "提交.zip"
    plan = PackPlan(source, destination=source / "student-project-course.zip")
    packed: list[object] = []

    app = CSBoxApp(
        data_source=FakeHomeDataSource(),
        environment=_environment(),
        locale=load_locale(),
        pack_plan_factory=lambda destination=None: _destination_plan(plan, destination),
        pack_action=packed.append,
    )

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        app.screen.query_one("#entry-pack").focus()
        await pilot.press("enter")
        await pilot.pause()
        destination_input = app.screen.query_one("#pack-destination-input", Input)
        destination_input.value = str(invalid)
        await pilot.click("#pack-update-preview")
        await pilot.pause()
        assert "目录" in _screen_text(app.screen)
        assert packed == []
        await _wait_until(
            pilot,
            lambda: not app.screen.query_one("#pack-update-preview", Button).has_class("-active"),
        )

        destination_input.value = str(corrected)
        await pilot.click("#pack-update-preview")
        await _wait_until(
            pilot,
            lambda: (
                isinstance(app.screen, PackConfirmationScreen)
                and app.screen.destination == corrected.absolute()
                and app.screen.focused is not None
                and app.screen.focused.id == "pack-confirm"
            ),
        )
        await pilot.click("#pack-confirm")
        await _wait_until(pilot, lambda: packed)
        assert packed[0].destination == corrected


@pytest.mark.asyncio
async def test_pack_publish_failure_keeps_destination_and_allows_retry(tmp_path: Path) -> None:
    source = tmp_path / "project"
    source.mkdir()
    destination = tmp_path / "交付" / "提交.zip"
    plan = PackPlan(source, destination=source / "student-project-course.zip")
    attempts = 0

    def pack_action(_plan: object) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise PermissionError("permission denied: CSBOX_SECRET_SENTINEL_publish")

    app = CSBoxApp(
        data_source=FakeHomeDataSource(),
        environment=_environment(),
        locale=load_locale(),
        pack_plan_factory=lambda destination=None: _destination_plan(plan, destination),
        pack_action=pack_action,
    )

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        app.screen.query_one("#entry-pack").focus()
        await pilot.press("enter")
        await pilot.pause()
        destination_input = app.screen.query_one("#pack-destination-input", Input)
        destination_input.value = str(destination)
        await pilot.click("#pack-confirm")
        await _wait_until(
            pilot,
            lambda: (
                isinstance(app.screen, PackConfirmationScreen)
                and app.screen.destination == destination.absolute()
                and app.screen.focused is not None
                and app.screen.focused.id == "pack-confirm"
            ),
        )
        await _wait_until(
            pilot,
            lambda: not app.screen.query_one("#pack-confirm", Button).has_class("-active"),
        )
        await pilot.press("enter")
        await _wait_until(pilot, lambda: "打包失败" in _screen_text(app.screen))
        assert "打包失败" in _screen_text(app.screen)
        assert "输出权限" in _screen_text(app.screen)
        assert "CSBOX_SECRET_SENTINEL_publish" not in _screen_text(app.screen)
        assert destination_input.value == str(destination)

        app.screen.query_one("#pack-confirm").focus()
        while app.screen.query_one("#pack-confirm").has_class("-active"):
            await pilot.pause()
        await pilot.press("enter")
        await _wait_until(pilot, lambda: attempts == 2)
        assert attempts == 2


@pytest.mark.asyncio
async def test_pack_symlink_destination_is_rejected_without_following_target(
    tmp_path: Path,
) -> None:
    source = tmp_path / "project"
    source.mkdir()
    protected = tmp_path / "protected.zip"
    protected.write_bytes(b"keep")
    symlink_destination = tmp_path / "link.zip"
    try:
        symlink_destination.symlink_to(protected)
    except OSError:
        pytest.skip("symlink creation is unavailable")
    plan = PackPlan(source, destination=source / "student-project-course.zip")
    packed: list[object] = []

    app = CSBoxApp(
        data_source=FakeHomeDataSource(),
        environment=_environment(),
        locale=load_locale(),
        pack_plan_factory=lambda destination=None: _destination_plan(plan, destination),
        pack_action=packed.append,
    )

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        app.screen.query_one("#entry-pack").focus()
        await pilot.press("enter")
        await pilot.pause()
        destination_input = app.screen.query_one("#pack-destination-input", Input)
        destination_input.value = str(symlink_destination)
        await pilot.click("#pack-update-preview")
        await pilot.pause()
        assert "符号链接" in _screen_text(app.screen) or "安全" in _screen_text(app.screen)
        assert packed == []
        assert protected.read_bytes() == b"keep"


@pytest.mark.asyncio
async def test_pack_verification_failure_is_distinguished_from_publish_failure(
    tmp_path: Path,
) -> None:
    source = tmp_path / "project"
    source.mkdir()
    destination = tmp_path / "提交.zip"
    plan = PackPlan(source, destination=destination)

    def fail_verification(_plan: object) -> None:
        raise PackServiceError("ZIP 校验失败。")

    app = CSBoxApp(
        data_source=FakeHomeDataSource(),
        environment=_environment(),
        locale=load_locale(),
        pack_plan_factory=lambda destination=None: _destination_plan(plan, destination),
        pack_action=fail_verification,
    )

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        app.screen.query_one("#entry-pack").focus()
        await pilot.press("enter")
        await pilot.pause()
        app.screen.query_one("#pack-confirm").focus()
        await pilot.press("enter")
        await pilot.pause()
        text = _screen_text(app.screen)
        assert "验证未通过" in text
        assert "未发布" in text
        assert not destination.exists()


@pytest.mark.asyncio
async def test_pack_destination_typing_does_not_rebuild_preview_per_keystroke(
    tmp_path: Path,
) -> None:
    source = tmp_path / "中文项目"
    source.mkdir()
    plan = PackPlan(source, destination=source / "student-project-course.zip")
    requested: list[Path | None] = []

    def plan_factory(destination: Path | None = None) -> PackPlan:
        requested.append(destination)
        return _destination_plan(plan, destination)

    app = CSBoxApp(
        data_source=FakeHomeDataSource(),
        environment=_environment(),
        locale=load_locale(),
        pack_plan_factory=plan_factory,
    )

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        app.screen.query_one("#entry-pack").focus()
        await pilot.press("enter")
        await pilot.pause()
        destination_input = app.screen.query_one("#pack-destination-input", Input)
        requested_before_typing = list(requested)
        destination_input.focus()
        await pilot.press(*"课程 提交/最终 中文.zip")
        await pilot.pause()
        assert requested == requested_before_typing


@pytest.mark.asyncio
@pytest.mark.parametrize("size", ((80, 24), (100, 30), (120, 35), (160, 45)))
async def test_pack_result_shows_exact_path_size_verify_and_summary(
    tmp_path: Path, size: tuple[int, int]
) -> None:
    source = tmp_path / "中文项目"
    source.mkdir()
    destination = tmp_path / "课程 提交" / "最终.zip"
    plan = PackPlan(source, destination=source / "student-project-course.zip")
    report = PackReport(
        source_root=source,
        destination=destination,
        entries=("README.md", "src/主程序.py"),
        excluded=("build:directory",),
        rejected=(),
        source_bytes=12,
        archive_bytes=321,
        verified=True,
        verification_status="verified",
    )

    app = CSBoxApp(
        data_source=FakeHomeDataSource(),
        environment=_environment(),
        locale=load_locale(),
        pack_plan_factory=lambda destination=None: _destination_plan(plan, destination),
        pack_action=lambda _plan: report,
    )

    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        app.screen.query_one("#entry-pack").focus()
        await pilot.press("enter")
        await pilot.pause()
        destination_input = app.screen.query_one("#pack-destination-input", Input)
        destination_input.value = str(destination)
        await pilot.click("#pack-confirm")
        await _wait_until(
            pilot,
            lambda: (
                isinstance(app.screen, PackConfirmationScreen)
                and app.screen.destination == destination.absolute()
                and app.screen.focused is not None
                and app.screen.focused.id == "pack-confirm"
            ),
        )
        await _wait_until(
            pilot,
            lambda: not app.screen.query_one("#pack-confirm", Button).has_class("-active"),
        )
        await pilot.press("enter")
        await _wait_until(pilot, lambda: isinstance(app.screen, PackResultScreen))

        assert isinstance(app.screen, PackResultScreen)
        result_text = _screen_text(app.screen)
        assert str(destination) in result_text.replace("\n", "")
        assert "最终.zip" in result_text
        assert "321 bytes" in result_text
        assert "验证：通过" in result_text
        assert "包含：2" in result_text
        assert "排除：1" in result_text
        for widget in app.screen.query(Static):
            if widget.size.width <= 0:
                continue
            assert all(
                display_width(line) <= widget.content_region.width
                for line in str(widget.renderable).splitlines()
            )
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, PackConfirmationScreen)
        assert app.screen.query_one("#pack-destination-input", Input).value == str(destination)
        assert "正在打包并验证" not in _screen_text(app.screen)


@pytest.mark.asyncio
async def test_real_pack_delivery_keeps_source_and_excludes_build_cache_and_self(
    tmp_path: Path,
) -> None:
    source = tmp_path / "中文课程项目"
    source.mkdir()
    (source / "README.md").write_text("# 课程项目\n", encoding="utf-8")
    (source / "pyproject.toml").write_text(
        "[project]\nname = 'cjk-course'\nversion = '0.1.0'\n",
        encoding="utf-8",
    )
    (source / "src" / "课程").mkdir(parents=True)
    (source / "src" / "课程" / "主程序.py").write_text(
        "print('课程项目')\n",
        encoding="utf-8",
    )
    (source / ".env.example").write_text('TOKEN="replace-me-token"\n', encoding="utf-8")
    (source / "build").mkdir()
    (source / "build" / "artifact.bin").write_bytes(b"excluded-build")
    (source / ".cache").mkdir()
    (source / ".cache" / "trace.log").write_text("excluded-cache", encoding="utf-8")
    before = {
        path.relative_to(source): path.read_bytes() for path in source.rglob("*") if path.is_file()
    }
    destination = tmp_path / "课程 提交" / "最终 版本 中文.zip"
    app = CSBoxApp(
        data_source=RealHomeDataSource(
            SessionRepository(source / ".csbox" / "sessions"),
            source,
        ),
        environment=_environment(),
        locale=load_locale(),
    )

    async with app.run_test(size=(120, 35)) as pilot:
        await pilot.pause()
        app.screen.query_one("#entry-pack").focus()
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, PackConfirmationScreen)
        assert ".env.example" in _screen_text(app.screen)
        destination_input = app.screen.query_one("#pack-destination-input", Input)
        destination_input.value = str(destination)
        await pilot.press("enter")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()

        assert isinstance(app.screen, PackResultScreen)
        result_text = _screen_text(app.screen)
        assert str(destination) in result_text.replace("\n", "")
        assert f"{destination.stat().st_size} bytes" in result_text
        assert "验证：通过" in result_text
        assert "包含：4" in result_text

    assert destination.is_file()
    assert {
        path.relative_to(source): path.read_bytes() for path in source.rglob("*") if path.is_file()
    } == before
    with ZipFile(destination) as archive:
        names = tuple(archive.namelist())
        assert archive.testzip() is None
        assert ".env.example" in names
        assert "README.md" in names
        assert "src/课程/主程序.py" in names
        assert all(not name.startswith("build/") for name in names)
        assert all(not name.startswith(".cache/") for name in names)
        assert destination.name not in names


@pytest.mark.asyncio
async def test_real_pack_secret_rejection_is_actionable_without_leaking_secret(
    tmp_path: Path,
) -> None:
    source = tmp_path / "项目"
    source.mkdir()
    secret = "sk_live_51N3aB7xQ2mL9pR4vC8dE0fG"
    (source / "README.md").write_text("# 项目\n", encoding="utf-8")
    (source / "settings.py").write_text(
        f'token = "{secret}"\n',
        encoding="utf-8",
    )
    destination = tmp_path / "提交.zip"
    app = CSBoxApp(
        data_source=RealHomeDataSource(
            SessionRepository(source / ".csbox" / "sessions"),
            source,
        ),
        environment=_environment(),
        locale=load_locale(),
    )

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        app.screen.query_one("#entry-pack").focus()
        await pilot.press("enter")
        await pilot.pause()

        assert isinstance(app.screen, PackConfirmationScreen)
        text = _screen_text(app.screen)
        assert "暂时不能打包" in text
        assert "硬编码 secret" in text
        assert secret not in text
        assert app.screen.query_one("#pack-confirm", Button).disabled is True
        await pilot.press("enter")
        await pilot.pause()

    assert not destination.exists()
