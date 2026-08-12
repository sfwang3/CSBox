from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from textual.widgets import Button, Static

from csbox.api.errors import ApiTransportError
from csbox.api.models import (
    ApiAssertion,
    ApiAssertionResult,
    ApiRequest,
    ApiResponse,
    ApiRun,
    ApiRunResult,
    ApiScenario,
    ApiStep,
)
from csbox.api.repository import ApiRunRepository
from csbox.api.scenario import ScenarioLoader
from csbox.check.models import CheckReport
from csbox.core.display_width import display_width
from csbox.core.models import EnvironmentSnapshot
from csbox.lab.fake_data import FakeHomeDataSource
from csbox.lab.home_data import RealHomeDataSource
from csbox.lab.repository import SessionRepository
from csbox.locales import load_locale
from csbox.tui.app import ApiApp, CSBoxApp
from csbox.tui.screens.api import ApiScreen
from csbox.tui.screens.home import HomeScreen

SECRET = "CSBOX_SECRET_SENTINEL_api_tui"


def _screen_text(screen: Any) -> str:
    widgets = screen.query(Static)
    buttons = screen.query(Button)
    return "\n".join(
        [
            *(str(widget.renderable) for widget in widgets),
            *(str(button.label) for button in buttons),
        ]
    )


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


def _scenario(path: Path, *, name: str = "中文 API 场景") -> Path:
    path.write_text(
        f'''name = "{name}"

[variables]
token = "{SECRET}"

[[steps]]
name = "查询中文 JSON"
method = "GET"
url = "https://example.test/profile?token={{{{token}}}}"

[steps.json]
message = "响应中文"
secret = "{{{{token}}}}"

[[steps.assertions]]
type = "status"
expected = 200
''',
        encoding="utf-8",
    )
    return path


def _run(
    run_id: str,
    *,
    status: str = "PASS",
    scenario_name: str = "中文 API 场景",
    error: str | None = None,
) -> ApiRun:
    request = ApiRequest(
        method="GET",
        url="https://example.test/profile?token=••••••••",
        headers={"Authorization": "Bearer ••••••••"},
        json_body={"message": "响应中文", "secret": "••••••••"},
    )
    assertion = ApiAssertion(kind="status", expected=200)
    result = ApiRunResult(
        step_name="查询中文 JSON",
        status=status,  # type: ignore[arg-type]
        response=(
            None
            if error is not None
            else ApiResponse(
                status_code=200,
                headers={"Content-Type": "application/json"},
                body=json.dumps({"message": "响应中文", "token": "••••••••"}, ensure_ascii=False),
                url=request.url,
                content_type="application/json",
                elapsed_ms=4.5,
            )
        ),
        assertions=(
            ApiAssertionResult(
                assertion=assertion,
                status="PASS" if status == "PASS" else "SKIP",
                message="Status = 200" if status == "PASS" else "未收到响应",
            ),
        ),
        error=error,
        elapsed_ms=4.5,
    )
    return ApiRun(
        id=run_id,
        scenario=ApiScenario(
            name=scenario_name,
            variables={"token": SECRET},
            source="scenarios/中文场景.toml",
            steps=(ApiStep(name="查询中文 JSON", request=request, assertions=(assertion,)),),
        ),
        started_at=datetime(2026, 8, 11, tzinfo=UTC),
        ended_at=datetime(2026, 8, 11, tzinfo=UTC),
        status=status,  # type: ignore[arg-type]
        results=(result,),
        elapsed_ms=4.5,
    )


class FakeRunner:
    def __init__(self, run: ApiRun) -> None:
        self.run_result = run
        self.calls: list[tuple[str, dict[str, str]]] = []

    async def run(self, scenario: ApiScenario, variables: dict[str, str], **_: object) -> ApiRun:
        self.calls.append((scenario.name, variables))
        return self.run_result


class TransportFailingRunner:
    async def run(self, _scenario: ApiScenario, _variables: dict[str, str], **_: object) -> ApiRun:
        raise ApiTransportError(
            "发生了什么：接口请求未完成。在哪里：网络传输。怎么处理：检查网络后重试。",
            debug_message="httpx.ConnectError",
        )


@dataclass(frozen=True)
class _FailingExporter:
    message: str = "CSBOX_SECRET_SENTINEL_export_failure"

    def export(self, *_args: object, **_kwargs: object) -> None:
        raise OSError(self.message)


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [(80, 24), (100, 30), (120, 35), (160, 45)])
async def test_api_app_has_actionable_empty_scenario_and_run_states(
    tmp_path: Path, size: tuple[int, int]
) -> None:
    repository = ApiRunRepository.from_cwd(tmp_path)
    app = ApiApp(repository, ScenarioLoader(), lambda: FakeRunner(_run("unused")), load_locale())

    async with app.run_test(size=size) as pilot:
        await pilot.pause()

        assert isinstance(app.screen, ApiScreen)
        assert "暂无 API 场景" in str(app.screen.query_one("#api-scenarios").renderable)
        assert "暂无 API 运行记录" in str(app.screen.query_one("#api-runs").renderable)
        assert app.screen.active_pane == "scenarios"

        await pilot.press("tab")
        assert app.screen.active_pane == "runs"
        await pilot.press("tab")
        assert app.screen.active_pane == "detail"
        assert "如何" in str(app.screen.query_one("#api-detail").renderable)


@pytest.mark.asyncio
async def test_api_scenario_enter_runs_persists_reloads_and_renders_safe_result(
    tmp_path: Path,
) -> None:
    scenario_dir = tmp_path / ".csbox" / "api" / "scenarios"
    scenario_dir.mkdir(parents=True)
    _scenario(scenario_dir / "中文场景.toml")
    repository = ApiRunRepository.from_cwd(tmp_path)
    run = _run("20260811T080000-aaaaaaaaaaaa")
    fake_runner = FakeRunner(run)
    app = ApiApp(repository, ScenarioLoader(), lambda *_args: fake_runner, load_locale())

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        scenario_button = app.screen.query_one("#scenario-0")
        scenario_button.focus()
        await pilot.press("enter")
        await pilot.pause()

        assert fake_runner.calls
        assert app.screen.selected_run is not None
        assert app.screen.selected_run.id == run.id
        detail = str(app.screen.query_one("#api-detail").renderable)
        assert "通过 (PASS)" in detail
        assert "Status = 200" in detail
        assert "响应中文" in detail
        assert SECRET not in _screen_text(app.screen)
        assert repository.load(run.id).id == run.id


@pytest.mark.asyncio
async def test_api_arrow_keys_move_focus_and_preview_rows(tmp_path: Path) -> None:
    scenario_dir = tmp_path / ".csbox" / "api" / "scenarios"
    scenario_dir.mkdir(parents=True)
    _scenario(scenario_dir / "a.toml", name="场景 A")
    _scenario(scenario_dir / "b.toml", name="场景 B")
    repository = ApiRunRepository.from_cwd(tmp_path)
    app = ApiApp(repository, ScenarioLoader(), lambda: FakeRunner(_run("unused")), load_locale())

    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        assert app.screen.focused.id == "scenario-0"
        await pilot.press("down")
        await pilot.pause()
        assert app.screen.focused.id == "scenario-1"
        assert "场景 B" in str(app.screen.query_one("#api-detail").renderable)
        await pilot.press("up")
        await pilot.pause()
        assert app.screen.focused.id == "scenario-0"
        await pilot.press("tab")
        await pilot.pause()
        assert app.screen.active_pane == "runs"
        await pilot.press("left")
        await pilot.pause()
        assert app.screen.active_pane == "scenarios"


@pytest.mark.asyncio
async def test_api_run_list_renders_fail_and_runtime_error_without_raw_cause(
    tmp_path: Path,
) -> None:
    repository = ApiRunRepository.from_cwd(tmp_path)
    repository.save(_run("20260811T080001-bbbbbbbbbbbb", status="FAIL"))
    repository.save(
        _run(
            "20260811T080002-cccccccccccc",
            status="RUNTIME_ERROR",
            error="发生了什么：请求失败。在哪里：HTTP transport。怎么处理：检查网络后重试。",
        )
    )
    app = ApiApp(repository, ScenarioLoader(), lambda: FakeRunner(_run("unused")), load_locale())

    async with app.run_test(size=(120, 35)) as pilot:
        await pilot.pause()

        run_list = str(app.screen.query_one("#api-runs").renderable)
        assert "失败 (FAIL)" in run_list
        assert "运行错误 (RUNTIME_ERROR)" in run_list
        assert SECRET not in _screen_text(app.screen)
        run_buttons = list(app.screen.query("#run-0"))
        assert run_buttons
        run_buttons[0].focus()
        await pilot.press("enter")
        await pilot.pause()
        assert "断言" in str(app.screen.query_one("#api-detail").renderable)


@pytest.mark.asyncio
async def test_api_export_success_and_failure_are_safe_user_actions(tmp_path: Path) -> None:
    repository = ApiRunRepository.from_cwd(tmp_path)
    run = _run("20260811T080003-dddddddddddd")
    repository.save(run)
    loaded = repository.load(run.id)

    app = ApiApp(repository, ScenarioLoader(), lambda: FakeRunner(run), load_locale())
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        app.screen.select_run(0)
        await pilot.press("e")
        await pilot.pause()
        assert (tmp_path / "evidence" / "api-evidence.md").is_file()
        assert "已导出 API 证据" in str(app.screen.query_one("#api-status").renderable)
        assert SECRET not in _screen_text(app.screen)

    failing = ApiApp(
        repository,
        ScenarioLoader(),
        lambda: FakeRunner(loaded),
        load_locale(),
        exporter_factory=lambda: _FailingExporter(),
    )
    async with failing.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        failing.screen.select_run(0)
        await pilot.press("e")
        await pilot.pause()
        text = _screen_text(failing.screen)
        assert "导出失败" in text
        assert "发生了什么" in text
        assert "在哪里" in text
        assert "怎么处理" in text
        assert SECRET not in text


@pytest.mark.asyncio
async def test_corrupted_run_is_not_rendered_as_valid_data_and_config_error_is_actionable(
    tmp_path: Path,
) -> None:
    scenario_dir = tmp_path / ".csbox" / "api" / "scenarios"
    scenario_dir.mkdir(parents=True)
    (scenario_dir / "坏场景.toml").write_text(
        'name = "坏场景"\n[[steps]\nname = "secret"\n', encoding="utf-8"
    )
    repository = ApiRunRepository.from_cwd(tmp_path)
    corrupted = repository.root / "20260811T080004-eeeeeeeeeeee"
    corrupted.mkdir(parents=True)
    (corrupted / "metadata.json").write_text("{broken", encoding="utf-8")
    app = ApiApp(repository, ScenarioLoader(), lambda: FakeRunner(_run("unused")), load_locale())

    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        scenarios = str(app.screen.query_one("#api-scenarios").renderable)
        runs = str(app.screen.query_one("#api-runs").renderable)
        assert "配置错误" in scenarios
        assert "不可用" not in runs
        assert SECRET not in _screen_text(app.screen)
        app.screen.select_scenario(0)
        assert "发生了什么" in str(app.screen.query_one("#api-detail").renderable)


@pytest.mark.asyncio
async def test_api_variable_and_transport_errors_stay_safe_in_tui(tmp_path: Path) -> None:
    scenario_dir = tmp_path / ".csbox" / "api" / "scenarios"
    scenario_dir.mkdir(parents=True)
    missing = scenario_dir / "缺少变量.toml"
    missing.write_text(
        """name = "缺少变量"

[[steps]]
name = "请求"
method = "GET"
url = "https://example.test/{{missing_url}}"
""",
        encoding="utf-8",
    )
    repository = ApiRunRepository.from_cwd(tmp_path)
    app = ApiApp(repository, ScenarioLoader(), lambda: TransportFailingRunner(), load_locale())

    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        app.screen.query_one("#scenario-0").focus()
        await pilot.press("enter")
        await pilot.pause()
        assert "变量" in _screen_text(app.screen)
        assert SECRET not in _screen_text(app.screen)

    transport_dir = tmp_path / "transport" / ".csbox" / "api" / "scenarios"
    transport_dir.mkdir(parents=True)
    _scenario(transport_dir / "transport.toml", name="传输错误")
    transport_repository = ApiRunRepository.from_cwd(tmp_path / "transport")
    transport_app = ApiApp(
        transport_repository,
        ScenarioLoader(),
        lambda: TransportFailingRunner(),
        load_locale(),
    )
    async with transport_app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        transport_app.screen.query_one("#scenario-0").focus()
        await pilot.press("enter")
        await pilot.pause()
        text = _screen_text(transport_app.screen)
        assert "网络传输" in text
        assert "httpx.ConnectError" not in text
        assert SECRET not in text


@pytest.mark.asyncio
async def test_api_screen_escape_returns_home_and_standalone_q_exits(tmp_path: Path) -> None:
    embedded = CSBoxApp(
        data_source=FakeHomeDataSource(),
        environment=_environment(),
        locale=load_locale(),
        api_repository=ApiRunRepository.from_cwd(tmp_path),
        api_runner_factory=lambda *_args: FakeRunner(_run("unused")),
    )
    async with embedded.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        embedded.screen.query_one("#entry-api").focus()
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(embedded.screen, ApiScreen)
        await pilot.press("escape")
        await pilot.pause()
        assert isinstance(embedded.screen, HomeScreen)

    standalone = ApiApp(
        ApiRunRepository.from_cwd(tmp_path),
        ScenarioLoader(),
        lambda: FakeRunner(_run("unused")),
        load_locale(),
    )
    async with standalone.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        await pilot.press("q")
        await pilot.pause()
        assert standalone.is_running is False


@pytest.mark.asyncio
async def test_home_renders_real_api_run_and_check_status(tmp_path: Path) -> None:
    repository = ApiRunRepository.from_cwd(tmp_path)
    repository.save(_run("20260811T080006-home12345678"))

    class CheckStub:
        def run(self, root: Path) -> CheckReport:
            return CheckReport(root=root)

    data_source = RealHomeDataSource(
        SessionRepository(tmp_path / ".csbox" / "sessions"),
        tmp_path,
        api_repository=repository,
        check_service=CheckStub(),  # type: ignore[arg-type]
    )
    app = CSBoxApp(
        data_source=data_source,
        environment=_environment(),
        locale=load_locale(),
        api_repository=repository,
        api_runner_factory=lambda *_args: FakeRunner(_run("unused")),
    )

    async with app.run_test(size=(120, 35)) as pilot:
        await pilot.pause()
        api_text = str(app.screen.query_one("#recent-api-content").renderable)
        check_text = str(app.screen.query_one("#home-check-status").renderable)
        assert "中文 API 场景" in api_text
        assert "通过" in api_text
        assert "项目检查：通过" in check_text
        assert SECRET not in _screen_text(app.screen)


@pytest.mark.asyncio
async def test_api_detail_uses_display_cells_for_cjk_and_ascii_mixed_content(
    tmp_path: Path,
) -> None:
    repository = ApiRunRepository.from_cwd(tmp_path)
    repository.save(_run("20260811T080005-ffffffffffff", scenario_name="计算机网络实验 abc"))
    app = ApiApp(repository, ScenarioLoader(), lambda: FakeRunner(_run("unused")), load_locale())

    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        app.screen.select_run(0)
        await pilot.pause()
        detail = app.screen.query_one("#api-detail")
        for line in str(detail.renderable).splitlines():
            assert display_width(line) <= max(2, detail.size.width - 2)
        assert "计算机网络实验" in _screen_text(app.screen)
        assert "😀" not in _screen_text(app.screen)
