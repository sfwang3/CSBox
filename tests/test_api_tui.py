from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from textual.widgets import Button, Input, Static

import csbox.tui.screens.api as api_screen_module
from csbox.api.errors import ApiTransportError
from csbox.api.exporter import ApiExportResult
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
from csbox.core.fonts import FontResolutionError
from csbox.core.models import EnvironmentSnapshot
from csbox.lab.fake_data import FakeHomeDataSource
from csbox.lab.home_data import RealHomeDataSource
from csbox.lab.repository import SessionRepository
from csbox.locales import load_locale
from csbox.tui.app import ApiApp, CSBoxApp
from csbox.tui.screens.api import ApiScreen
from csbox.tui.screens.home import HomeScreen
from tui_harness import (
    focus_and_press,
    wait_for_busy,
    wait_for_focus,
    wait_for_screen,
    wait_for_widget,
    wait_for_worker_start,
)
from tui_harness import (
    wait_until as _wait_until,
)

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


def _api_export_result_rendered(app: ApiApp) -> bool:
    if app.screen.name != "api-export-result":
        return False
    return any(
        widget.id == "api-export-result-body" and bool(str(widget.renderable).strip())
        for widget in app.screen.query(Static)
    )


def _api_export_dialog_ready(app: ApiApp) -> bool:
    if app.screen.name != "api-export":
        return False
    if app.screen.size.width <= 0 or app.screen.size.height <= 0:
        return False
    destinations = list(app.screen.query("#api-export-destination"))
    submits = list(app.screen.query("#api-export-submit"))
    selects = list(app.screen.query("#api-export-theme"))
    if not (destinations and submits and selects):
        return False
    submit = submits[0]
    focused = app.screen.focused
    return bool(
        list(selects[0].query("SelectOverlay"))
        and focused is not None
        and focused.id == "api-export-destination"
        and not submit.disabled
        and submit.visible
        and submit.size.width > 0
        and submit.size.height > 0
        and app.screen.region.contains_region(submit.region)
    )


def _api_export_failure_ready(app: ApiApp) -> bool:
    return _api_export_dialog_ready(app) and "导出失败" in _screen_text(app.screen)


def _api_import_result_ready(app: ApiApp) -> bool:
    if not isinstance(app.screen, ApiScreen) or len(app.screen.scenarios) != 2:
        return False
    text = _screen_text(app.screen)
    return "生成场景：2 个" in text and "保存位置" in text


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
    def __init__(self) -> None:
        self.started = threading.Event()

    async def run(self, _scenario: ApiScenario, _variables: dict[str, str], **_: object) -> ApiRun:
        self.started.set()
        raise ApiTransportError(
            "发生了什么：接口请求未完成。在哪里：网络传输。怎么处理：检查网络后重试。",
            debug_message="httpx.ConnectError",
        )


@dataclass(frozen=True)
class _FailingExporter:
    message: str = "CSBOX_SECRET_SENTINEL_export_failure"

    def export(self, *_args: object, **_kwargs: object) -> None:
        raise OSError(self.message)


class _MissingFontExporter:
    def export(self, *_args: object, **_kwargs: object) -> None:
        raise FontResolutionError("找不到支持中文的字体")


class _BlockingExporter:
    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()

    def export(
        self,
        _run: ApiRun,
        destination: Path,
        *,
        theme: str,
        force: bool,
    ) -> ApiExportResult:
        del theme, force
        self.started.set()
        assert self.release.wait(timeout=5)
        return ApiExportResult(
            destination=destination,
            evidence=(),
            markdown=destination / "api-evidence.md",
            results=destination / "results.json",
        )


@dataclass
class _RetryingExporter:
    calls: list[tuple[Path, str, bool]]
    fail_once: bool = True

    def export(
        self,
        _run: ApiRun,
        destination: Path,
        *,
        theme: str,
        force: bool,
    ) -> ApiExportResult:
        self.calls.append((destination, theme, force))
        if self.fail_once:
            self.fail_once = False
            raise OSError("temporary exporter failure")
        markdown = destination / "api-evidence.md"
        results = destination / "results.json"
        return ApiExportResult(
            destination=destination,
            evidence=(destination / "evidence" / "step-1.png",),
            markdown=markdown,
            results=results,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [(80, 24), (100, 30), (120, 35), (160, 45)])
async def test_api_app_has_actionable_empty_scenario_and_run_states(
    tmp_path: Path, size: tuple[int, int]
) -> None:
    repository = ApiRunRepository.from_cwd(tmp_path)
    app = ApiApp(repository, ScenarioLoader(), lambda: FakeRunner(_run("unused")), load_locale())

    async with app.run_test(size=size) as pilot:
        await wait_for_screen(pilot, app, ApiScreen)
        await wait_for_widget(pilot, app.screen, "#api-quick-create")
        await wait_for_widget(pilot, app.screen, "#api-openapi-import")
        await wait_for_widget(pilot, app.screen, "#api-runs")

        assert isinstance(app.screen, ApiScreen)
        assert "暂无 API 场景" in str(app.screen.query_one("#api-scenarios").renderable)
        assert str(app.screen.query_one("#api-quick-create", Button).label) == "快速创建"
        assert str(app.screen.query_one("#api-openapi-import", Button).label) == "从 OpenAPI 导入"
        assert "暂无 API 运行记录" in str(app.screen.query_one("#api-runs").renderable)
        assert app.screen.active_pane == "scenarios"

        await pilot.press("tab")
        assert app.screen.active_pane == "runs"
        await pilot.press("tab")
        assert app.screen.active_pane == "detail"
        assert "如何" in str(app.screen.query_one("#api-detail").renderable)


@pytest.mark.asyncio
async def test_api_quick_create_persists_minimal_cjk_scenario_and_selects_it(
    tmp_path: Path,
) -> None:
    repository = ApiRunRepository.from_cwd(tmp_path)
    app = ApiApp(repository, ScenarioLoader(), lambda: FakeRunner(_run("unused")), load_locale())

    async with app.run_test(size=(100, 30)) as pilot:
        await wait_for_screen(pilot, app, ApiScreen)
        await focus_and_press(pilot, app, "#api-quick-create")
        dialog = await wait_for_screen(pilot, app, "api-quick-create")
        name_input = await wait_for_widget(pilot, dialog, "#api-quick-create-name")
        url_input = await wait_for_widget(pilot, dialog, "#api-quick-create-url")
        name_input.value = "中文起步场景"
        url_input.value = "http://localhost:8080/api/test?中文=值"
        url_input.focus()
        await wait_for_focus(pilot, app, url_input)
        await pilot.press("enter")
        await wait_for_screen(pilot, app, ApiScreen)
        files = tuple((tmp_path / ".csbox/api/scenarios").glob("*.toml"))
        assert len(files) == 1
        loaded = ScenarioLoader().load(files[0])
        assert loaded.name == "中文起步场景"
        assert loaded.steps[0].name == "请求"
        assert loaded.steps[0].request.method == "GET"
        assert loaded.steps[0].request.url == "http://localhost:8080/api/test?中文=值"
        assert app.screen.selected_scenario_index == 0
        assert app.screen.scenarios[0].path == files[0]


@pytest.mark.asyncio
async def test_api_quick_create_cancel_and_invalid_submit_do_not_write(tmp_path: Path) -> None:
    repository = ApiRunRepository.from_cwd(tmp_path)
    app = ApiApp(repository, ScenarioLoader(), lambda: FakeRunner(_run("unused")), load_locale())

    async with app.run_test(size=(80, 24)) as pilot:
        await wait_for_screen(pilot, app, ApiScreen)
        await focus_and_press(pilot, app, "#api-quick-create")
        dialog = await wait_for_screen(pilot, app, "api-quick-create")
        name_input = await wait_for_widget(pilot, dialog, "#api-quick-create-name")
        await wait_for_focus(pilot, app, name_input)
        await pilot.press("enter")
        await _wait_until(pilot, lambda: "请输入" in _screen_text(dialog))
        assert "请输入" in _screen_text(dialog)
        assert not (tmp_path / ".csbox/api/scenarios").exists()

        await pilot.press("escape")
        await wait_for_screen(pilot, app, ApiScreen)
        assert not (tmp_path / ".csbox/api/scenarios").exists()


@pytest.mark.asyncio
async def test_api_quick_create_input_changes_do_not_write_or_reload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = ApiRunRepository.from_cwd(tmp_path)
    app = ApiApp(repository, ScenarioLoader(), lambda: FakeRunner(_run("unused")), load_locale())

    def unexpected_write(*_args: object, **_kwargs: object) -> tuple[Path, ...]:
        raise AssertionError("typing must not write scenario files")

    monkeypatch.setattr(api_screen_module, "write_scenario_files", unexpected_write)
    async with app.run_test(size=(80, 24)) as pilot:
        await wait_for_screen(pilot, app, ApiScreen)
        await focus_and_press(pilot, app, "#api-quick-create")
        dialog = await wait_for_screen(pilot, app, "api-quick-create")
        name_input = await wait_for_widget(pilot, dialog, "#api-quick-create-name")
        url_input = await wait_for_widget(pilot, dialog, "#api-quick-create-url")
        name_input.value = "输入中的场景"
        url_input.value = "http://localhost:8080/api/test?value=long"
        assert dialog.name == "api-quick-create"
        assert not (tmp_path / ".csbox/api/scenarios").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("url", ["ftp://example.test", "http://localhost/{{token}}"])
async def test_api_quick_create_rejects_non_concrete_url_without_write(
    tmp_path: Path, url: str
) -> None:
    repository = ApiRunRepository.from_cwd(tmp_path)
    app = ApiApp(repository, ScenarioLoader(), lambda: FakeRunner(_run("unused")), load_locale())

    async with app.run_test(size=(80, 24)) as pilot:
        await wait_for_screen(pilot, app, ApiScreen)
        await focus_and_press(pilot, app, "#api-quick-create")
        dialog = await wait_for_screen(pilot, app, "api-quick-create")
        name_input = await wait_for_widget(pilot, dialog, "#api-quick-create-name")
        url_input = await wait_for_widget(pilot, dialog, "#api-quick-create-url")
        name_input.value = "坏 URL 场景"
        url_input.value = url
        url_input.focus()
        await wait_for_focus(pilot, app, url_input)
        await pilot.press("enter")
        await _wait_until(pilot, lambda: "URL" in _screen_text(dialog))
        assert "URL" in _screen_text(dialog)
        assert not (tmp_path / ".csbox/api/scenarios").exists()


@pytest.mark.asyncio
async def test_api_invalid_only_scenarios_keep_beginner_bootstrap_actions(tmp_path: Path) -> None:
    scenario_dir = tmp_path / ".csbox" / "api" / "scenarios"
    scenario_dir.mkdir(parents=True)
    (scenario_dir / "坏场景.toml").write_text('name = "坏场景"\n', encoding="utf-8")
    app = ApiApp(
        ApiRunRepository.from_cwd(tmp_path),
        ScenarioLoader(),
        lambda: FakeRunner(_run("unused")),
        load_locale(),
    )

    async with app.run_test(size=(80, 24)) as pilot:
        await wait_for_screen(pilot, app, ApiScreen)
        await wait_for_widget(pilot, app.screen, "#api-quick-create")
        await wait_for_widget(pilot, app.screen, "#api-openapi-import")
        assert app.screen.query_one("#api-quick-create", Button)
        assert app.screen.query_one("#api-openapi-import", Button)
        assert app.screen.focused is not None
        assert app.screen.focused.id == "api-quick-create"
        assert "配置错误" in _screen_text(app.screen)


def _write_openapi_fixture(path: Path) -> Path:
    path.write_text(
        json.dumps(
            {
                "openapi": "3.0.3",
                "info": {"title": "本地 API", "version": "1"},
                "paths": {
                    "/zeta/{item}": {
                        "get": {
                            "responses": {"200": {"description": "ok"}},
                        }
                    },
                    "/alpha": {
                        "post": {
                            "responses": {"201": {"description": "created"}},
                        }
                    },
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return path


@pytest.mark.asyncio
async def test_api_openapi_import_opens_from_empty_state_and_cancel_writes_nothing(
    tmp_path: Path,
) -> None:
    repository = ApiRunRepository.from_cwd(tmp_path)
    app = ApiApp(repository, ScenarioLoader(), lambda: FakeRunner(_run("unused")), load_locale())

    async with app.run_test(size=(80, 24)) as pilot:
        await wait_for_screen(pilot, app, ApiScreen)
        await focus_and_press(pilot, app, "#api-openapi-import")
        dialog = await wait_for_screen(pilot, app, "api-openapi-import")
        await wait_for_widget(pilot, dialog, "#api-openapi-path")
        await pilot.press("escape")
        await wait_for_screen(pilot, app, ApiScreen)
        assert not (tmp_path / ".csbox/api/scenarios").exists()


@pytest.mark.asyncio
async def test_api_openapi_import_success_reports_count_todo_and_deterministic_selection(
    tmp_path: Path,
) -> None:
    source = _write_openapi_fixture(tmp_path / "课程 资料.json")
    repository = ApiRunRepository.from_cwd(tmp_path)
    fake_runner = FakeRunner(_run("unused"))
    app = ApiApp(repository, ScenarioLoader(), lambda: fake_runner, load_locale())

    async with app.run_test(size=(100, 30)) as pilot:
        await wait_for_screen(pilot, app, ApiScreen)
        await focus_and_press(pilot, app, "#api-openapi-import")
        dialog = await wait_for_screen(pilot, app, "api-openapi-import")
        path_input = await wait_for_widget(pilot, dialog, "#api-openapi-path")
        path_input.value = str(source)
        path_input.focus()
        await wait_for_focus(pilot, app, path_input)
        await pilot.press("enter")
        await _wait_until(
            pilot,
            lambda: _api_import_result_ready(app),
        )

        assert isinstance(app.screen, ApiScreen)
        text = _screen_text(app.screen)
        assert "生成场景：2 个" in text
        assert "保存位置" in text
        assert "补充参数" in text or "TODO" in text
        assert len(app.screen.scenarios) == 2
        assert app.screen.selected_scenario_index == 0
        assert app.screen.scenarios[0].path.name.casefold() == "get-zeta-item.toml"
        assert fake_runner.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("empty_write", "expected_count", "expected_selection"),
    [
        (True, 0, None),
        (False, 1, 0),
    ],
)
async def test_api_openapi_import_zero_or_one_selection_is_deterministic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    empty_write: bool,
    expected_count: int,
    expected_selection: int | None,
) -> None:
    source = tmp_path / "中文 OpenAPI.json"
    source.write_text(
        json.dumps(
            {
                "openapi": "3.0.3",
                "info": {"title": "test"},
                "paths": {
                    "/health": {
                        "get": {"responses": {"200": {"description": "ok"}}},
                    }
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    if empty_write:
        monkeypatch.setattr(
            api_screen_module,
            "write_scenario_templates",
            lambda *_args, **_kwargs: (),
        )
    repository = ApiRunRepository.from_cwd(tmp_path)
    fake_runner = FakeRunner(_run("unused"))
    app = ApiApp(repository, ScenarioLoader(), lambda: fake_runner, load_locale())

    async with app.run_test(size=(100, 30)) as pilot:
        await wait_for_screen(pilot, app, ApiScreen)
        await focus_and_press(pilot, app, "#api-openapi-import")
        dialog = await wait_for_screen(pilot, app, "api-openapi-import")
        path_input = await wait_for_widget(pilot, dialog, "#api-openapi-path")
        path_input.value = str(source)
        path_input.focus()
        await wait_for_focus(pilot, app, path_input)
        await pilot.press("enter")
        await _wait_until(
            pilot,
            lambda: (
                isinstance(app.screen, ApiScreen)
                and len(app.screen.scenarios) == expected_count
                and app.screen.selected_scenario_index == expected_selection
                and f"生成场景：{expected_count} 个" in _screen_text(app.screen)
            ),
        )

        assert app.screen.selected_scenario_index == expected_selection
        assert f"生成场景：{expected_count} 个" in _screen_text(app.screen)
        assert fake_runner.calls == []
        if expected_count == 0:
            assert "没有生成可运行场景" in _screen_text(app.screen)


@pytest.mark.asyncio
async def test_api_openapi_import_zero_keeps_existing_selection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "empty-result.json"
    source.write_text(
        json.dumps(
            {
                "openapi": "3.0.3",
                "info": {"title": "test"},
                "paths": {"/health": {"get": {"responses": {"200": {}}}}},
            }
        ),
        encoding="utf-8",
    )
    scenario_dir = tmp_path / ".csbox" / "api" / "scenarios"
    scenario_dir.mkdir(parents=True)
    existing = scenario_dir / "existing.toml"
    _scenario(existing, name="已有场景")
    monkeypatch.setattr(api_screen_module, "write_scenario_templates", lambda *_args, **_kwargs: ())
    repository = ApiRunRepository.from_cwd(tmp_path)
    app = ApiApp(repository, ScenarioLoader(), lambda: FakeRunner(_run("unused")), load_locale())

    async with app.run_test(size=(100, 30)) as pilot:
        await wait_for_screen(pilot, app, ApiScreen)
        assert app.screen.selected_scenario_index == 0
        app.screen._open_openapi_import()  # type: ignore[attr-defined]
        dialog = await wait_for_screen(pilot, app, "api-openapi-import")
        path_input = await wait_for_widget(pilot, dialog, "#api-openapi-path")
        path_input.value = str(source)
        path_input.focus()
        await wait_for_focus(pilot, app, path_input)
        await pilot.press("enter")
        await _wait_until(
            pilot,
            lambda: (
                isinstance(app.screen, ApiScreen)
                and not app.screen.is_working
                and app.screen.selected_scenario_index == 0
                and app.screen.scenarios[0].path == existing
                and "生成场景：0 个" in _screen_text(app.screen)
            ),
        )
        assert app.screen.selected_scenario_index == 0
        assert app.screen.scenarios[0].path == existing


@pytest.mark.asyncio
async def test_api_openapi_import_invalid_path_keeps_dialog_and_path(tmp_path: Path) -> None:
    repository = ApiRunRepository.from_cwd(tmp_path)
    app = ApiApp(repository, ScenarioLoader(), lambda: FakeRunner(_run("unused")), load_locale())
    missing = tmp_path / "不存在 文件.yaml"

    async with app.run_test(size=(80, 24)) as pilot:
        await wait_for_screen(pilot, app, ApiScreen)
        await focus_and_press(pilot, app, "#api-openapi-import")
        dialog = await wait_for_screen(pilot, app, "api-openapi-import")
        path_input = await wait_for_widget(pilot, dialog, "#api-openapi-path")
        path_input.value = str(missing)
        path_input.focus()
        await wait_for_focus(pilot, app, path_input)
        await pilot.press("enter")
        await _wait_until(
            pilot,
            lambda: (
                app.screen.name == "api-openapi-import"
                and bool(list(app.screen.query("#api-openapi-path")))
                and ("失败" in _screen_text(app.screen) or "无法" in _screen_text(app.screen))
            ),
        )

        assert app.screen.query_one("#api-openapi-path", Input).value == str(missing)
        assert "失败" in _screen_text(app.screen) or "无法" in _screen_text(app.screen)
        assert not (tmp_path / ".csbox/api/scenarios").exists()


@pytest.mark.asyncio
async def test_api_openapi_import_conflict_requires_confirmation_and_cancel_keeps_path(
    tmp_path: Path,
) -> None:
    source = _write_openapi_fixture(tmp_path / "课程 资料.json")
    scenario_dir = tmp_path / ".csbox" / "api" / "scenarios"
    scenario_dir.mkdir(parents=True)
    existing = scenario_dir / "GET-zeta-item.toml"
    existing.write_text(
        'name = "旧场景"\n\n[[steps]]\nname = "请求"\nmethod = "GET"\n'
        'url = "http://localhost/old"\n',
        encoding="utf-8",
    )
    repository = ApiRunRepository.from_cwd(tmp_path)
    app = ApiApp(repository, ScenarioLoader(), lambda: FakeRunner(_run("unused")), load_locale())

    async with app.run_test(size=(100, 30)) as pilot:
        await wait_for_screen(pilot, app, ApiScreen)
        app.screen._open_openapi_import()  # type: ignore[attr-defined]
        dialog = await wait_for_screen(pilot, app, "api-openapi-import")
        path_input = await wait_for_widget(pilot, dialog, "#api-openapi-path")
        path_input.value = str(source)
        path_input.focus()
        await wait_for_focus(pilot, app, path_input)
        await pilot.press("enter")
        await _wait_until(
            pilot,
            lambda: (
                app.screen.name == "api-openapi-overwrite"
                and bool(list(app.screen.query("#api-openapi-overwrite-confirm")))
            ),
        )

        assert "替换" in _screen_text(app.screen)
        await pilot.press("escape")
        await _wait_until(pilot, lambda: app.screen.name == "api-openapi-import")
        assert app.screen.query_one("#api-openapi-path", Input).value == str(source)
        assert "旧场景" in existing.read_text(encoding="utf-8")


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
        await wait_for_screen(pilot, app, ApiScreen)
        scenario_button = await wait_for_widget(pilot, app.screen, "#scenario-0")
        scenario_button.focus()
        await wait_for_focus(pilot, app, scenario_button)
        await pilot.press("enter")
        await wait_for_worker_start(pilot, lambda: bool(fake_runner.calls))
        await _wait_until(
            pilot,
            lambda: (
                not app.screen.is_working
                and app.screen.selected_run is not None
                and app.screen.selected_run.id == run.id
            ),
        )

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
        await wait_for_screen(pilot, app, ApiScreen)
        first = await wait_for_widget(pilot, app.screen, "#scenario-0")
        second = await wait_for_widget(pilot, app.screen, "#scenario-1")
        await wait_for_focus(pilot, app, first)
        assert app.screen.focused.id == "scenario-0"
        await pilot.press("down")
        await wait_for_focus(pilot, app, second)
        await _wait_until(
            pilot,
            lambda: "场景 B" in str(app.screen.query_one("#api-detail").renderable),
        )
        assert app.screen.focused.id == "scenario-1"
        assert "场景 B" in str(app.screen.query_one("#api-detail").renderable)
        await pilot.press("up")
        await wait_for_focus(pilot, app, first)
        assert app.screen.focused.id == "scenario-0"
        await pilot.press("tab")
        await _wait_until(pilot, lambda: app.screen.active_pane == "runs")
        assert app.screen.active_pane == "runs"
        await pilot.press("left")
        await _wait_until(pilot, lambda: app.screen.active_pane == "scenarios")
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
        await wait_for_screen(pilot, app, ApiScreen)
        await wait_for_widget(pilot, app.screen, "#run-0")

        run_list = str(app.screen.query_one("#api-runs").renderable)
        assert "失败 (FAIL)" in run_list
        assert "运行错误 (RUNTIME_ERROR)" in run_list
        assert SECRET not in _screen_text(app.screen)
        await focus_and_press(pilot, app, "#run-0")
        await _wait_until(
            pilot,
            lambda: "断言" in str(app.screen.query_one("#api-detail").renderable),
        )
        assert "断言" in str(app.screen.query_one("#api-detail").renderable)


@pytest.mark.asyncio
async def test_api_export_success_and_failure_are_safe_user_actions(tmp_path: Path) -> None:
    repository = ApiRunRepository.from_cwd(tmp_path)
    run = _run("20260811T080003-dddddddddddd")
    repository.save(run)
    loaded = repository.load(run.id)

    app = ApiApp(repository, ScenarioLoader(), lambda: FakeRunner(run), load_locale())
    async with app.run_test(size=(100, 30)) as pilot:
        await wait_for_screen(pilot, app, ApiScreen)
        await wait_for_widget(pilot, app.screen, "#run-0")
        app.screen.select_run(0)
        await pilot.press("e")
        await _wait_until(pilot, lambda: _api_export_dialog_ready(app))
        await focus_and_press(pilot, app, "#api-export-submit")
        await _wait_until(pilot, lambda: _api_export_result_rendered(app), timeout=5.0)
        assert (tmp_path / "evidence" / "api-evidence.md").is_file()
        assert "导出完成" in _screen_text(app.screen)
        assert SECRET not in _screen_text(app.screen)

    failing = ApiApp(
        repository,
        ScenarioLoader(),
        lambda: FakeRunner(loaded),
        load_locale(),
        exporter_factory=lambda: _FailingExporter(),
    )
    async with failing.run_test(size=(100, 30)) as pilot:
        await wait_for_screen(pilot, failing, ApiScreen)
        await wait_for_widget(pilot, failing.screen, "#run-0")
        failing.screen.select_run(0)
        await pilot.press("e")
        await _wait_until(pilot, lambda: _api_export_dialog_ready(failing))
        await focus_and_press(pilot, failing, "#api-export-submit")
        await _wait_until(pilot, lambda: _api_export_failure_ready(failing))
        text = _screen_text(failing.screen)
        assert "导出失败" in text
        assert failing.screen.name == "api-export"
        assert SECRET not in text

    missing_font = ApiApp(
        repository,
        ScenarioLoader(),
        lambda: FakeRunner(loaded),
        load_locale(),
        exporter_factory=lambda: _MissingFontExporter(),
    )
    async with missing_font.run_test(size=(100, 30)) as pilot:
        await wait_for_screen(pilot, missing_font, ApiScreen)
        await wait_for_widget(pilot, missing_font.screen, "#run-0")
        missing_font.screen.select_run(0)
        await pilot.press("e")
        await _wait_until(pilot, lambda: _api_export_dialog_ready(missing_font))
        await focus_and_press(pilot, missing_font, "#api-export-submit")
        await _wait_until(pilot, lambda: _api_export_failure_ready(missing_font))
        text = _screen_text(missing_font.screen)
        assert "导出失败" in text
        assert missing_font.screen.name == "api-export"
        assert SECRET not in text


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [(80, 24), (100, 30), (120, 35), (160, 45)])
async def test_api_export_dialog_default_custom_restore_and_exact_result(
    tmp_path: Path, size: tuple[int, int]
) -> None:
    repository = ApiRunRepository.from_cwd(tmp_path)
    run = _run("20260811T080010-eeeeeeeeeeee")
    repository.save(run)
    custom = tmp_path / ("课程资料" * 10) / "API 证据"

    app = ApiApp(repository, ScenarioLoader(), lambda: FakeRunner(run), load_locale())
    async with app.run_test(size=size) as pilot:
        await wait_for_screen(pilot, app, ApiScreen)
        await wait_for_widget(pilot, app.screen, "#run-0")
        app.screen.select_run(0)
        await pilot.press("e")
        await _wait_until(pilot, lambda: _api_export_dialog_ready(app))

        destination_input = app.screen.query_one("#api-export-destination", Input)
        assert run.id in _screen_text(app.screen)
        assert destination_input.value == str(tmp_path / "evidence")
        destination_input.value = str(custom)
        await focus_and_press(pilot, app, "#api-export-restore-default")
        assert destination_input.value == str(tmp_path / "evidence")
        destination_input.value = str(custom)
        await focus_and_press(pilot, app, "#api-export-submit")
        await _wait_until(pilot, lambda: _api_export_result_rendered(app), timeout=5.0)

        text = _screen_text(app.screen)
        assert run.id in text
        assert str(custom) in text.replace("\n", "")
        assert "evidence/01-查询中文-JSON.png" in text
        assert "api-evidence.md" in text
        assert "results.json" in text
        assert SECRET not in text


@pytest.mark.asyncio
async def test_api_export_dialog_supports_keyboard_submit_and_return(tmp_path: Path) -> None:
    repository = ApiRunRepository.from_cwd(tmp_path)
    run = _run("20260811T080015-444444444444")
    repository.save(run)
    calls: list[tuple[Path, str, bool]] = []
    app = ApiApp(
        repository,
        ScenarioLoader(),
        lambda: FakeRunner(run),
        load_locale(),
        exporter_factory=lambda: _RetryingExporter(calls, fail_once=False),
    )

    async with app.run_test(size=(80, 24)) as pilot:
        await wait_for_screen(pilot, app, ApiScreen)
        await wait_for_widget(pilot, app.screen, "#run-0")
        app.screen.select_run(0)
        await pilot.press("e")
        await _wait_until(pilot, lambda: _api_export_dialog_ready(app))
        assert app.screen.focused.id == "api-export-destination"
        await pilot.press("enter")
        await _wait_until(pilot, lambda: _api_export_result_rendered(app), timeout=5.0)
        await pilot.press("escape")
        await _wait_until(pilot, lambda: app.screen.name == "api")
        assert calls == [(tmp_path / "evidence", "dark", False)]


@pytest.mark.asyncio
async def test_api_export_keeps_api_screen_visible_while_worker_is_busy(tmp_path: Path) -> None:
    repository = ApiRunRepository.from_cwd(tmp_path)
    run = _run("20260811T080016-busybusybusy")
    repository.save(run)
    exporter = _BlockingExporter()
    app = ApiApp(
        repository,
        ScenarioLoader(),
        lambda: FakeRunner(run),
        load_locale(),
        exporter_factory=lambda: exporter,
    )

    async with app.run_test(size=(100, 30)) as pilot:
        await wait_for_screen(pilot, app, ApiScreen)
        await wait_for_widget(pilot, app.screen, "#run-0")
        app.screen.select_run(0)
        await pilot.press("e")
        await _wait_until(pilot, lambda: _api_export_dialog_ready(app))
        await focus_and_press(pilot, app, "#api-export-submit")
        await wait_for_worker_start(pilot, exporter.started.is_set)
        await wait_for_busy(pilot, app.screen, True)

        await pilot.press("escape", "q")
        await pilot.pause()
        assert app.is_running
        assert app.screen.name == "api"
        assert app.screen.is_working
        assert "正在导出 API 证据" in _screen_text(app.screen)
        await pilot.press("ctrl+c")
        await pilot.pause()
        assert app.is_running
        assert app.screen.name == "api"

        exporter.release.set()
        await _wait_until(pilot, lambda: _api_export_result_rendered(app), timeout=5.0)


@pytest.mark.asyncio
async def test_api_busy_return_preserves_non_export_operation_status(tmp_path: Path) -> None:
    repository = ApiRunRepository.from_cwd(tmp_path)
    app = ApiApp(repository, ScenarioLoader(), lambda: FakeRunner(_run("unused")), load_locale())

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        screen = app.screen
        screen._busy = True
        screen._refresh_status("正在导入 OpenAPI……")

        await pilot.press("escape", "q")
        await pilot.pause()

        assert app.is_running
        assert app.screen is screen
        assert "正在导入 OpenAPI" in _screen_text(screen)
        screen._busy = False


@pytest.mark.asyncio
async def test_api_export_existing_target_cancel_preserves_custom_destination(
    tmp_path: Path,
) -> None:
    repository = ApiRunRepository.from_cwd(tmp_path)
    run = _run("20260811T080011-ffffffffffff")
    repository.save(run)
    destination = tmp_path / "已有导出"
    destination.mkdir()
    app = ApiApp(repository, ScenarioLoader(), lambda: FakeRunner(run), load_locale())

    async with app.run_test(size=(100, 30)) as pilot:
        await wait_for_screen(pilot, app, ApiScreen)
        await wait_for_widget(pilot, app.screen, "#run-0")
        app.screen.select_run(0)
        await pilot.press("e")
        await _wait_until(pilot, lambda: _api_export_dialog_ready(app))
        app.screen.query_one("#api-export-destination", Input).value = str(destination)
        await focus_and_press(pilot, app, "#api-export-submit")
        await _wait_until(
            pilot,
            lambda: (
                app.screen.name == "api-export-overwrite"
                and bool(list(app.screen.query("#api-export-overwrite-confirm")))
            ),
        )

        assert "覆盖" in _screen_text(app.screen)
        await pilot.press("escape")
        await _wait_until(pilot, lambda: _api_export_dialog_ready(app))
        assert app.screen.query_one("#api-export-destination", Input).value == str(destination)


@pytest.mark.asyncio
async def test_api_export_existing_target_requires_confirmation_then_forces_export(
    tmp_path: Path,
) -> None:
    repository = ApiRunRepository.from_cwd(tmp_path)
    run = _run("20260811T080013-222222222222")
    repository.save(run)
    destination = tmp_path / "已有 API 证据"
    destination.mkdir()
    (destination / "student-notes.txt").write_text("保留用户文件", encoding="utf-8")
    app = ApiApp(repository, ScenarioLoader(), lambda: FakeRunner(run), load_locale())

    async with app.run_test(size=(100, 30)) as pilot:
        await wait_for_screen(pilot, app, ApiScreen)
        await wait_for_widget(pilot, app.screen, "#run-0")
        app.screen.select_run(0)
        await pilot.press("e")
        await _wait_until(pilot, lambda: _api_export_dialog_ready(app))
        app.screen.query_one("#api-export-destination", Input).value = str(destination)
        await focus_and_press(pilot, app, "#api-export-submit")
        await _wait_until(
            pilot,
            lambda: (
                app.screen.name == "api-export-overwrite"
                and bool(list(app.screen.query("#api-export-overwrite-confirm")))
            ),
        )
        await focus_and_press(pilot, app, "#api-export-overwrite-confirm")
        await _wait_until(pilot, lambda: _api_export_result_rendered(app), timeout=5.0)

        assert (destination / "api-evidence.md").is_file()
        assert (destination / "results.json").is_file()
        assert (destination / "student-notes.txt").read_text(encoding="utf-8") == "保留用户文件"


@pytest.mark.asyncio
async def test_api_export_regular_file_failure_is_controlled_and_untouched(tmp_path: Path) -> None:
    repository = ApiRunRepository.from_cwd(tmp_path)
    run = _run("20260811T080014-333333333333")
    repository.save(run)
    destination = tmp_path / "不是目录"
    destination.write_text("user file", encoding="utf-8")
    app = ApiApp(repository, ScenarioLoader(), lambda: FakeRunner(run), load_locale())

    async with app.run_test(size=(100, 30)) as pilot:
        await wait_for_screen(pilot, app, ApiScreen)
        await wait_for_widget(pilot, app.screen, "#run-0")
        app.screen.select_run(0)
        await pilot.press("e")
        await _wait_until(pilot, lambda: _api_export_dialog_ready(app))
        app.screen.query_one("#api-export-destination", Input).value = str(destination)
        await focus_and_press(pilot, app, "#api-export-submit")
        await _wait_until(pilot, lambda: _api_export_failure_ready(app))

        text = _screen_text(app.screen)
        assert "导出失败" in text
        assert "Traceback" not in text
        assert app.screen.query_one("#api-export-destination", Input).value == str(destination)
        assert destination.read_text(encoding="utf-8") == "user file"


@pytest.mark.asyncio
async def test_api_export_failure_returns_to_dialog_and_retry_keeps_destination(
    tmp_path: Path,
) -> None:
    repository = ApiRunRepository.from_cwd(tmp_path)
    run = _run("20260811T080012-111111111111")
    repository.save(run)
    calls: list[tuple[Path, str, bool]] = []
    exporter = _RetryingExporter(calls)
    destination = tmp_path / "自定义 API 输出"
    app = ApiApp(
        repository,
        ScenarioLoader(),
        lambda: FakeRunner(run),
        load_locale(),
        exporter_factory=lambda: exporter,
    )

    async with app.run_test(size=(100, 30)) as pilot:
        await wait_for_screen(pilot, app, ApiScreen)
        await wait_for_widget(pilot, app.screen, "#run-0")
        app.screen.select_run(0)
        await pilot.press("e")
        await _wait_until(pilot, lambda: _api_export_dialog_ready(app))
        app.screen.query_one("#api-export-destination", Input).value = str(destination)
        await focus_and_press(pilot, app, "#api-export-submit")
        await _wait_until(pilot, lambda: _api_export_failure_ready(app))

        assert "导出失败" in _screen_text(app.screen)
        assert app.screen.query_one("#api-export-destination", Input).value == str(destination)
        await focus_and_press(pilot, app, "#api-export-submit")
        await _wait_until(pilot, lambda: _api_export_result_rendered(app), timeout=5.0)
        assert calls == [(destination, "dark", False), (destination, "dark", False)]


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
        await wait_for_screen(pilot, app, ApiScreen)
        await wait_for_widget(pilot, app.screen, "#scenario-0")
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
    missing_app = ApiApp(
        repository,
        ScenarioLoader(),
        lambda: TransportFailingRunner(),
        load_locale(),
    )

    async with missing_app.run_test(size=(80, 24)) as pilot:
        await wait_for_screen(pilot, missing_app, ApiScreen)
        scenario_button = await wait_for_widget(pilot, missing_app.screen, "#scenario-0")
        scenario_button.focus()
        await wait_for_focus(pilot, missing_app, scenario_button)
        await pilot.press("enter")
        await _wait_until(
            pilot,
            lambda: (
                not missing_app.screen.is_working and "变量" in _screen_text(missing_app.screen)
            ),
        )
        assert "变量" in _screen_text(missing_app.screen)
        assert SECRET not in _screen_text(missing_app.screen)

    transport_dir = tmp_path / "transport" / ".csbox" / "api" / "scenarios"
    transport_dir.mkdir(parents=True)
    _scenario(transport_dir / "transport.toml", name="传输错误")
    transport_repository = ApiRunRepository.from_cwd(tmp_path / "transport")
    transport_runner = TransportFailingRunner()
    transport_app = ApiApp(
        transport_repository,
        ScenarioLoader(),
        lambda: transport_runner,
        load_locale(),
    )
    async with transport_app.run_test(size=(80, 24)) as pilot:
        await wait_for_screen(pilot, transport_app, ApiScreen)
        scenario_button = await wait_for_widget(pilot, transport_app.screen, "#scenario-0")
        scenario_button.focus()
        await wait_for_focus(pilot, transport_app, scenario_button)
        await pilot.press("enter")
        await wait_for_worker_start(pilot, transport_runner.started.is_set)
        await _wait_until(
            pilot,
            lambda: (
                not transport_app.screen.is_working
                and "网络传输" in _screen_text(transport_app.screen)
            ),
        )
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
        await wait_for_screen(pilot, embedded, "home")
        await focus_and_press(pilot, embedded, "#entry-api")
        await wait_for_screen(pilot, embedded, ApiScreen)
        await pilot.press("escape")
        await wait_for_screen(pilot, embedded, HomeScreen)

    standalone = ApiApp(
        ApiRunRepository.from_cwd(tmp_path),
        ScenarioLoader(),
        lambda: FakeRunner(_run("unused")),
        load_locale(),
    )
    async with standalone.run_test(size=(80, 24)) as pilot:
        await wait_for_screen(pilot, standalone, ApiScreen)
        await pilot.press("q")
        await _wait_until(pilot, lambda: standalone.is_running is False)


@pytest.mark.asyncio
async def test_home_keeps_api_and_check_workflows_out_of_lightweight_home(
    tmp_path: Path,
) -> None:
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
        project_text = str(app.screen.query_one("#home-project-path").renderable)
        workflow_text = str(app.screen.query_one("#home-workflow-status").renderable)
        assert project_text
        assert "准备就绪" in workflow_text
        assert "中文 API 场景" not in _screen_text(app.screen)
        assert "项目检查：通过" not in _screen_text(app.screen)
        assert SECRET not in _screen_text(app.screen)


@pytest.mark.asyncio
async def test_api_detail_uses_display_cells_for_cjk_and_ascii_mixed_content(
    tmp_path: Path,
) -> None:
    repository = ApiRunRepository.from_cwd(tmp_path)
    repository.save(_run("20260811T080005-ffffffffffff", scenario_name="计算机网络实验 abc"))
    app = ApiApp(repository, ScenarioLoader(), lambda: FakeRunner(_run("unused")), load_locale())

    async with app.run_test(size=(80, 24)) as pilot:
        await wait_for_screen(pilot, app, ApiScreen)
        await wait_for_widget(pilot, app.screen, "#run-0")
        app.screen.select_run(0)
        await _wait_until(pilot, lambda: app.screen.selected_run is not None)
        detail = await wait_for_widget(pilot, app.screen, "#api-detail")
        await _wait_until(pilot, lambda: detail.size.width > 0 and detail.size.height > 0)
        for line in str(detail.renderable).splitlines():
            assert display_width(line) <= max(2, detail.size.width - 2)
        assert "计算机网络实验" in _screen_text(app.screen)
        assert "😀" not in _screen_text(app.screen)
