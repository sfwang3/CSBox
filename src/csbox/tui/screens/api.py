"""Textual adapter for the persisted API scenario and evidence workflow."""

from __future__ import annotations

import asyncio
import inspect
import json
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.events import Resize
from textual.screen import Screen
from textual.widgets import Button, Static

from csbox.api.errors import (
    ApiConfigError,
    ApiDomainError,
    ApiPersistenceError,
    ApiTransportError,
)
from csbox.api.exporter import ApiEvidenceExporter, ApiExportResult
from csbox.api.models import (
    ApiAssertion,
    ApiRequest,
    ApiResponse,
    ApiRun,
    ApiScenario,
    ApiStep,
)
from csbox.api.openapi import OpenApiImporter, write_scenario_templates
from csbox.api.redaction import Redactor
from csbox.api.repository import ApiRunRepository, ApiRunSummary
from csbox.api.scenario import ScenarioLoader
from csbox.api.scenario_writer import ScenarioFile, scenario_filename, write_scenario_files
from csbox.api.variables import resolve_variables
from csbox.config import ConfigPaths, ConfigurationError, load_config
from csbox.core.display_width import display_width, truncate_cells
from csbox.core.fonts import FontResolutionError
from csbox.core.text_layout import wrap_cells
from csbox.locales import Translator
from csbox.tui.dialogs.api import (
    ApiExportDialog,
    ApiExportOverwriteDialog,
    ApiExportRequest,
    ApiOpenApiImportDialog,
    ApiOpenApiOverwriteDialog,
    ApiQuickCreateDialog,
    ApiQuickCreateRequest,
    ApiScenarioOverwriteDialog,
)
from csbox.tui.screens.api_export_result import ApiExportResultScreen
from csbox.tui.widgets.api import ApiDetail, ApiFooter, ApiRunList, ApiScenarioList

RunnerFactory = Callable[..., object]
ExporterFactory = Callable[[], object]


@dataclass(frozen=True, slots=True)
class _ScenarioItem:
    path: Path
    scenario: ApiScenario | None
    error: str | None = None


class ApiScreen(Screen[None]):
    """Render safe scenario/run views and dispatch domain services."""

    BINDINGS = (
        ("tab", "cycle_pane", "切换视图"),
        ("up", "focus_previous_row", "上移"),
        ("down", "focus_next_row", "下移"),
        ("left", "previous_pane", "左侧视图"),
        ("right", "next_pane", "右侧视图"),
        ("r", "run_selected", "运行场景"),
        ("e", "export_selected", "导出证据"),
        ("escape", "go_back", "返回"),
        ("q", "go_back", "返回"),
    )

    def __init__(
        self,
        repository: ApiRunRepository,
        scenario_loader: ScenarioLoader,
        runner_factory: RunnerFactory,
        locale: Translator,
        *,
        scenario_dir: Path | str | None = None,
        exporter_factory: ExporterFactory | None = None,
    ) -> None:
        super().__init__(name="api")
        self.repository = repository
        self.scenario_loader = scenario_loader
        self.runner_factory = runner_factory
        self.locale = locale
        self.scenario_dir = (
            Path(scenario_dir)
            if scenario_dir is not None
            else (Path(repository.root).parent / "scenarios")
        )
        self.exporter_factory = exporter_factory
        self.scenarios: tuple[_ScenarioItem, ...] = ()
        self.run_summaries: tuple[ApiRunSummary, ...] = ()
        self.selected_scenario_index: int | None = None
        self.selected_run_index: int | None = None
        self.selected_run: ApiRun | None = None
        self.active_pane = "scenarios"
        self.is_wide = False
        self._busy = False

    def compose(self) -> ComposeResult:
        yield Vertical(
            Static(self.locale("api.title"), id="api-title", markup=False),
            Static("", id="api-status", markup=False),
            Horizontal(
                ApiScenarioList(empty_text=self.locale("api.empty.scenarios"), id="api-scenarios"),
                ApiRunList(empty_text=self.locale("api.empty.runs"), id="api-runs"),
                ApiDetail(self.locale("api.empty.detail"), id="api-detail", markup=False),
                id="api-body",
            ),
            ApiFooter("", id="api-footer", markup=False),
            id="api-layout",
        )

    async def on_mount(self) -> None:
        self._set_layout(self.size.width >= 120)
        await self._reload()

    def on_resize(self, event: Resize) -> None:
        self._set_layout(event.size.width >= 120)
        self._focus_active_pane()
        self._refresh_detail()
        self._refresh_footer()

    def _set_layout(self, wide: bool) -> None:
        self.is_wide = wide
        self.set_class(wide, "wide")
        self.set_class(not wide, "narrow")
        self._set_active_pane_class()

    def _set_active_pane_class(self) -> None:
        for pane in ("scenarios", "runs", "detail"):
            self.set_class(self.active_pane == pane, f"show-{pane}")

    async def _reload(self) -> None:
        self.scenarios = self._load_scenarios()
        try:
            self.run_summaries = tuple(self.repository.list())
        except (ApiPersistenceError, OSError, UnicodeError, ValueError):
            self.run_summaries = ()
        self.selected_scenario_index = 0 if self.scenarios else None
        self.selected_run_index = 0 if self.run_summaries else None
        self.selected_run = None
        await self._refresh_lists()
        self._refresh_detail()
        self._refresh_status("")
        self._refresh_footer()
        self._focus_active_pane()

    def _load_scenarios(self) -> tuple[_ScenarioItem, ...]:
        try:
            paths = tuple(
                sorted(
                    (
                        path
                        for path in self.scenario_dir.glob("*.toml")
                        if path.is_file() and not path.is_symlink()
                    ),
                    key=lambda path: (path.name.casefold(), path.name),
                )
            )
        except (OSError, ValueError):
            return ()
        items: list[_ScenarioItem] = []
        for path in paths:
            try:
                loaded = self.scenario_loader.load(path)
                items.append(_ScenarioItem(path, _redact_scenario(loaded, path)))
            except ApiConfigError:
                items.append(_ScenarioItem(path, None, self.locale("api.error.scenario")))
            except (OSError, UnicodeError, ValueError):
                items.append(_ScenarioItem(path, None, self.locale("api.error.scenario")))
        return tuple(items)

    async def _refresh_lists(self) -> None:
        scenario_panel = self.query_one("#api-scenarios", ApiScenarioList)
        scenario_lines = [self.locale("api.scenarios.title")]
        scenario_buttons: list[Button] = []
        if not self._has_loadable_scenarios():
            scenario_lines.append(self.locale("api.empty.scenarios"))
            scenario_lines.append(self.locale("api.empty.scenarios.hint"))
            scenario_buttons.extend(
                (
                    Button(
                        self.locale("api.action.quick_create"),
                        id="api-quick-create",
                        classes="api-empty-action",
                        variant="primary",
                    ),
                    Button(
                        self.locale("api.action.openapi_import"),
                        id="api-openapi-import",
                        classes="api-empty-action",
                    ),
                )
            )
        for index, item in enumerate(self.scenarios):
            if item.scenario is None:
                scenario_lines.append(self.locale("api.scenario.invalid", name=item.path.name))
                label = self.locale("api.action.invalid", name=item.path.name)
            else:
                scenario_lines.append(
                    truncate_cells(
                        self.locale("api.scenario.item", name=item.scenario.name),
                        max(1, (scenario_panel.size.width or 34) - 2),
                        ellipsis="…",
                    )
                )
                label = self.locale("api.action.run", name=item.scenario.name)
            scenario_buttons.append(
                Button(
                    truncate_cells(
                        label,
                        max(8, (scenario_panel.size.width or 34) - 4),
                        ellipsis="…",
                    ),
                    id=f"scenario-{index}",
                    classes="api-row",
                )
            )
        await scenario_panel.set_rows("\n".join(scenario_lines), scenario_buttons)

        run_panel = self.query_one("#api-runs", ApiRunList)
        run_lines = [self.locale("api.runs.title")]
        run_buttons: list[Button] = []
        if not self.run_summaries:
            run_lines.append(self._run_empty_text())
        else:
            for index, summary in enumerate(self.run_summaries):
                status = _status_label(summary.status)
                available_width = max(1, (run_panel.size.width or 34) - 2)
                elapsed = f"{summary.elapsed_ms:.1f} ms"
                suffix_width = display_width(elapsed) + 2
                name_width = max(0, available_width - display_width(status) - suffix_width - 2)
                name = truncate_cells(summary.scenario_name, name_width, ellipsis="…")
                run_line = status
                if name_width > 0:
                    run_line = f"{status}  {name}  {elapsed}"
                run_lines.append(
                    truncate_cells(
                        run_line,
                        available_width,
                        ellipsis="…",
                    )
                )
                run_buttons.append(
                    Button(
                        truncate_cells(
                            self.locale("api.action.view", name=summary.scenario_name),
                            max(8, (run_panel.size.width or 34) - 4),
                            ellipsis="…",
                        ),
                        id=f"run-{index}",
                        classes="api-row",
                    )
                )
        await run_panel.set_rows("\n".join(run_lines), run_buttons)

    def _has_loadable_scenarios(self) -> bool:
        return any(item.scenario is not None for item in self.scenarios)

    def _run_empty_text(self) -> str:
        if self._has_unavailable_run_directories():
            return self.locale("api.empty.runs.corrupt")
        return self.locale("api.empty.runs")

    def _has_unavailable_run_directories(self) -> bool:
        try:
            return self.repository.root.is_dir() and any(
                path.is_dir() and not path.is_symlink() for path in self.repository.root.iterdir()
            )
        except OSError:
            return False

    def _refresh_detail(self) -> None:
        detail = self.query_one("#api-detail", ApiDetail)
        if self.selected_run is not None:
            detail.update(self._wrap(self._run_lines(self.selected_run)))
            return
        if self.selected_scenario_index is None or not self.scenarios:
            detail.update(self._wrap(self.locale("api.empty.detail").splitlines()))
            return
        item = self.scenarios[self.selected_scenario_index]
        if item.scenario is None:
            detail.update(self._wrap(self._scenario_error_lines(item)))
            return
        detail.update(self._wrap(self._scenario_lines(item.scenario)))

    def _scenario_lines(self, scenario: ApiScenario) -> list[str]:
        lines = [
            self.locale("api.detail.scenario", name=scenario.name),
            self.locale("api.detail.source", source=scenario.source or "—"),
            "",
        ]
        for index, step in enumerate(scenario.steps, 1):
            lines.extend(
                (
                    self.locale("api.detail.step", index=index, name=step.name),
                    f"{step.request.method} {step.request.url}",
                    *self._request_lines(step.request),
                    self.locale("api.detail.assertions"),
                )
            )
            if not step.assertions:
                lines.append(self.locale("api.detail.no_assertions"))
            else:
                lines.extend(self._assertion_lines(step.assertions))
        lines.extend(("", self.locale("api.detail.hint_run")))
        return lines

    def _scenario_error_lines(self, item: _ScenarioItem) -> list[str]:
        return [
            self.locale("api.detail.error_title"),
            self.locale("api.detail.error_what"),
            self.locale("api.detail.error_where", where=item.path.name),
            self.locale("api.detail.error_how"),
        ]

    def _run_lines(self, run: ApiRun) -> list[str]:
        lines = [
            self.locale("api.detail.run", id=run.id),
            self.locale("api.detail.scenario", name=run.scenario.name),
            self.locale("api.detail.status", status=_status_label(run.status)),
            "",
        ]
        for index, result in enumerate(run.results):
            lines.extend(
                (
                    self.locale("api.detail.step", index=index + 1, name=result.step_name),
                    self.locale("api.detail.status", status=_status_label(result.status)),
                )
            )
            request = run.scenario.steps[index].request if index < len(run.scenario.steps) else None
            if request is not None:
                lines.append(f"{request.method} {request.url}")
                lines.extend(self._request_lines(request))
            if result.response is None:
                lines.append(self.locale("api.detail.no_response"))
            else:
                lines.extend(self._response_lines(result.response))
            lines.append(self.locale("api.detail.assertions"))
            if result.assertions:
                lines.extend(self._assertion_lines(result.assertions))
            else:
                lines.append(self.locale("api.detail.no_assertions"))
            if result.error:
                lines.append(self.locale("api.detail.error_message", message=result.error))
            lines.append("")
        return lines

    def _request_lines(self, request: ApiRequest) -> list[str]:
        lines = [self.locale("api.detail.request")]
        for key, value in request.headers.items():
            lines.append(f"{key}: {value}")
        if request.query:
            lines.append(f"query={_inline_json(request.query)}")
        if request.json_body is not None:
            lines.append(f"json={_inline_json(request.json_body)}")
        if request.form:
            lines.append(f"form={_inline_json(request.form)}")
        if request.multipart:
            lines.append(
                f"multipart={_inline_json([part.model_dump() for part in request.multipart])}"
            )
        return lines

    def _response_lines(self, response: ApiResponse) -> list[str]:
        return [
            self.locale("api.detail.response"),
            f"HTTP {response.status_code}  {response.elapsed_ms:.1f} ms",
            *(() if response.content_type is None else (response.content_type,)),
            response.body or "—",
        ]

    def _assertion_lines(self, assertions: tuple[Any, ...]) -> list[str]:
        lines: list[str] = []
        for assertion in assertions:
            if isinstance(assertion, ApiAssertion):
                location = assertion.location or assertion.kind
                lines.append(
                    self.locale(
                        "api.detail.assertion_expected",
                        location=location,
                        expected=_inline_json(assertion.expected),
                    )
                )
            else:
                location = assertion.assertion.location or assertion.assertion.kind
                lines.append(
                    self.locale(
                        "api.detail.assertion_result",
                        status=_status_label(assertion.status),
                        location=location,
                        expected=_inline_json(assertion.assertion.expected),
                        actual=_inline_json(assertion.actual),
                        message=assertion.message,
                    )
                )
        return lines

    def _wrap(self, lines: list[str] | tuple[str, ...]) -> str:
        detail_width = self.query_one("#api-detail", ApiDetail).size.width
        available_width = detail_width or max(8, (self.size.width or 60) - 8)
        width = max(2, available_width - 2)
        return "\n".join(part for line in lines for part in wrap_cells(line, width))

    def _refresh_status(self, message: str) -> None:
        self.query_one("#api-status", Static).update(message)

    def _refresh_footer(self) -> None:
        footer = self.locale("api.shortcuts")
        footer_widget = self.query_one("#api-footer", ApiFooter)
        width = footer_widget.content_region.width or max(1, self.size.width - 6)
        footer_widget.update(truncate_cells(footer, width, ellipsis="…"))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        identifier = event.button.id or ""
        if identifier == "api-quick-create":
            self._open_quick_create()
        elif identifier == "api-openapi-import":
            self._open_openapi_import()
        elif identifier.startswith("scenario-"):
            self.select_scenario(int(identifier.removeprefix("scenario-")), execute=True)
        elif identifier.startswith("run-"):
            self.select_run(int(identifier.removeprefix("run-")))

    def _open_quick_create(self, initial: ApiQuickCreateRequest | None = None) -> None:
        self.app.push_screen(
            ApiQuickCreateDialog(locale=self.locale, initial=initial),
            self._handle_quick_create,
        )

    def _open_openapi_import(
        self,
        initial_path: Path | None = None,
        initial_error: str = "",
    ) -> None:
        self.app.push_screen(
            ApiOpenApiImportDialog(
                locale=self.locale,
                initial_path=initial_path,
                initial_error=initial_error,
            ),
            self._handle_openapi_import,
        )

    def _handle_openapi_import(self, source: Path | None) -> None:
        if source is None:
            return
        self.run_worker(
            self._import_openapi(source),
            name="api-openapi-import",
            group="api-openapi-import",
            exclusive=True,
            exit_on_error=False,
        )

    def _handle_openapi_overwrite(self, source: Path, confirmed: bool) -> None:
        if confirmed:
            self.run_worker(
                self._import_openapi(source, force=True),
                name="api-openapi-import",
                group="api-openapi-import",
                exclusive=True,
                exit_on_error=False,
            )
        else:
            self._open_openapi_import(source)

    async def _import_openapi(self, source: Path, *, force: bool = False) -> None:
        self._busy = True
        self._refresh_status(self.locale("api.status.importing"))
        previous_path = (
            self.scenarios[self.selected_scenario_index].path
            if self.selected_scenario_index is not None
            and self.selected_scenario_index < len(self.scenarios)
            else None
        )

        def import_files() -> tuple[tuple[Path, ...], bool]:
            importer = OpenApiImporter()
            document = importer.load(source)
            scenario = importer.to_scenario(document, source.stem)
            written = write_scenario_templates(scenario, self.scenario_dir, force=force)
            serialized = json.dumps(scenario.model_dump(mode="json"), ensure_ascii=False)
            return written, "{{TODO_" in serialized

        try:
            written, has_todo = await asyncio.to_thread(import_files)
            await self._reload()
            generated = {Path(path) for path in written}
            if not generated:
                self._restore_scenario_selection(previous_path)
            else:
                candidates = [
                    (index, item.path)
                    for index, item in enumerate(self.scenarios)
                    if item.path in generated
                ]
                if candidates:
                    self.select_scenario(candidates[0][0], execute=False)
            self._refresh_status(
                self.locale(
                    "api.status.imported",
                    count=len(written),
                )
            )
            self._show_import_result(written, has_todo)
        except ApiConfigError:
            self._open_openapi_import(source, self.locale("api.error.openapi_import"))
        except FileExistsError:
            if force:
                self._open_openapi_import(source, self.locale("api.error.openapi_conflict"))
            else:
                self.app.push_screen(
                    ApiOpenApiOverwriteDialog(
                        locale=self.locale,
                        destination=self.scenario_dir,
                    ),
                    lambda confirmed: self._handle_openapi_overwrite(source, confirmed),
                )
        except (OSError, UnicodeError, TypeError, ValueError):
            self._open_openapi_import(source, self.locale("api.error.openapi_import"))
        finally:
            self._busy = False

    def _restore_scenario_selection(self, previous_path: Path | None) -> None:
        self.selected_scenario_index = next(
            (
                index
                for index, item in enumerate(self.scenarios)
                if previous_path is not None and item.path == previous_path
            ),
            None,
        )
        self.selected_run_index = None
        self.selected_run = None
        self._refresh_detail()

    def _show_import_result(self, written: tuple[Path, ...], has_todo: bool) -> None:
        lines = [
            self.locale("api.import.result.title"),
            self.locale("api.import.result.count", count=len(written)),
            self.locale("api.import.result.location", path=str(self.scenario_dir.resolve())),
        ]
        if not written:
            lines.extend(("", self.locale("api.import.result.none")))
        elif has_todo:
            lines.extend(("", self.locale("api.import.result.todo")))
        else:
            lines.extend(("", self.locale("api.import.result.ready")))
        self.query_one("#api-detail", ApiDetail).update(self._wrap(lines))

    def _handle_quick_create(self, request: ApiQuickCreateRequest | None) -> None:
        if request is None:
            return
        self.run_worker(self._persist_quick_create(request), exclusive=True)

    def _handle_quick_create_overwrite(
        self,
        request: ApiQuickCreateRequest,
        confirmed: bool,
    ) -> None:
        if confirmed:
            self.run_worker(self._persist_quick_create(request, force=True), exclusive=True)
        else:
            self._open_quick_create(request)

    async def _persist_quick_create(
        self,
        request: ApiQuickCreateRequest,
        *,
        force: bool = False,
    ) -> None:
        self._busy = True
        self._refresh_status(self.locale("api.status.creating"))
        scenario = ApiScenario(
            name=request.name,
            steps=(
                ApiStep(
                    name="请求",
                    request=ApiRequest(method=request.method, url=request.url),
                ),
            ),
        )
        try:
            written = write_scenario_files(
                (ScenarioFile(filename_stem=request.name, scenario=scenario),),
                self.scenario_dir,
                force=force,
            )
            await self._reload()
            selected_path = written[0] if written else None
            self.selected_scenario_index = next(
                (
                    index
                    for index, item in enumerate(self.scenarios)
                    if selected_path is not None and item.path == selected_path
                ),
                None,
            )
            self.selected_run_index = None
            self.selected_run = None
            self.active_pane = "detail"
            self._set_active_pane_class()
            self._refresh_detail()
            self._refresh_status(self.locale("api.status.created", name=request.name))
        except FileExistsError:
            if force:
                self._show_inline_error(self.locale("api.error.quick_create"))
            else:
                self.app.push_screen(
                    ApiScenarioOverwriteDialog(
                        locale=self.locale,
                        filename=scenario_filename(request.name),
                    ),
                    lambda confirmed: self._handle_quick_create_overwrite(request, confirmed),
                )
        except (OSError, UnicodeError, TypeError, ValueError):
            self._show_inline_error(self.locale("api.error.quick_create"))
        finally:
            self._busy = False

    def select_scenario(self, index: int, *, execute: bool = False) -> None:
        if not 0 <= index < len(self.scenarios):
            return
        self.selected_scenario_index = index
        self.selected_run_index = None
        self.selected_run = None
        self.active_pane = "detail"
        self._set_active_pane_class()
        self._focus_active_pane()
        self._refresh_detail()
        if execute and self.scenarios[index].scenario is not None and not self._busy:
            self.run_worker(self._run_scenario(index), exclusive=True)

    def select_run(self, index: int) -> None:
        if not 0 <= index < len(self.run_summaries):
            return
        self.selected_run_index = index
        self.selected_scenario_index = None
        self.active_pane = "detail"
        self._set_active_pane_class()
        self._focus_active_pane()
        try:
            self.selected_run = self.repository.load(self.run_summaries[index].id)
        except ApiPersistenceError:
            self.selected_run = None
            self._show_inline_error(self.locale("api.error.corrupt_run"))
            return
        self._refresh_detail()

    def action_cycle_pane(self) -> None:
        self.action_next_pane()

    def action_previous_pane(self) -> None:
        self._move_pane(-1)

    def action_next_pane(self) -> None:
        self._move_pane(1)

    def _move_pane(self, step: int) -> None:
        panes = ("scenarios", "runs", "detail")
        self.active_pane = panes[(panes.index(self.active_pane) + step) % len(panes)]
        self._set_active_pane_class()
        self._focus_active_pane()
        self._refresh_detail()

    def _focus_active_pane(self) -> None:
        if self.active_pane in {"scenarios", "runs"}:
            panel = self.query_one(
                "#api-scenarios" if self.active_pane == "scenarios" else "#api-runs"
            )
            if self.active_pane == "scenarios" and not self._has_loadable_scenarios():
                empty_action = next(iter(panel.query("#api-quick-create")), None)
                if empty_action is not None:
                    empty_action.focus()
                    return
            if self._row_buttons(panel):
                self._focus_row(0, keep_selection=True)
            else:
                self.set_focus(None)
            return
        self.query_one("#api-detail", ApiDetail).focus()

    def action_focus_previous_row(self) -> None:
        self._focus_row(-1)

    def action_focus_next_row(self) -> None:
        self._focus_row(1)

    def _focus_row(self, step: int, *, keep_selection: bool = False) -> None:
        if self.active_pane not in {"scenarios", "runs"}:
            return
        panel = self.query_one("#api-scenarios" if self.active_pane == "scenarios" else "#api-runs")
        buttons = self._row_buttons(panel)
        if not buttons:
            return
        current = next((index for index, button in enumerate(buttons) if button.has_focus), None)
        if current is None:
            selected = (
                self.selected_scenario_index
                if self.active_pane == "scenarios"
                else self.selected_run_index
            )
            current = selected if selected is not None and selected < len(buttons) else 0
        target = max(0, min(len(buttons) - 1, current + step))
        buttons[target].focus()
        if self.active_pane == "scenarios":
            self.selected_scenario_index = target
            self.selected_run_index = None
            self.selected_run = None
        else:
            self.selected_run_index = target
            self.selected_scenario_index = None
            try:
                self.selected_run = self.repository.load(self.run_summaries[target].id)
            except ApiPersistenceError:
                self.selected_run = None
                self._show_inline_error(self.locale("api.error.corrupt_run"))
                return
        if not keep_selection:
            self._refresh_detail()

    def _row_buttons(self, panel: object) -> list[Button]:
        prefix = "scenario-" if self.active_pane == "scenarios" else "run-"
        return [
            button
            for button in panel.query(Button)  # type: ignore[attr-defined]
            if (button.id or "").startswith(prefix)
        ]

    def action_run_selected(self) -> None:
        if self.selected_scenario_index is not None and not self._busy:
            item = self.scenarios[self.selected_scenario_index]
            if item.scenario is not None:
                self.run_worker(self._run_scenario(self.selected_scenario_index), exclusive=True)

    def action_export_selected(self) -> None:
        if self.selected_run is not None and not self._busy:
            self._open_export_dialog()

    def _open_export_dialog(
        self,
        initial: ApiExportRequest | None = None,
        error: str = "",
    ) -> None:
        if self.selected_run is None:
            return
        self.app.push_screen(
            ApiExportDialog(
                locale=self.locale,
                run=self.selected_run,
                default_destination=self._project_dir() / "evidence",
                initial=initial,
                error=error,
            ),
            self._handle_export_request,
        )

    def _handle_export_request(self, request: ApiExportRequest | None) -> None:
        if request is None or self.selected_run is None or self._busy:
            return
        self.run_worker(
            self._export_run(request),
            name="api-export",
            group="api-export",
            exclusive=True,
            exit_on_error=False,
        )

    def _handle_export_overwrite(self, request: ApiExportRequest, confirmed: bool) -> None:
        if confirmed:
            self.run_worker(
                self._export_run(request, force=True),
                name="api-export",
                group="api-export",
                exclusive=True,
                exit_on_error=False,
            )
        else:
            self._open_export_dialog(request)

    def action_go_back(self) -> None:
        if getattr(self.app, "owns_api_screen", False):
            self.app.exit()
        else:
            self.app.pop_screen()

    async def _run_scenario(self, index: int) -> None:
        self._busy = True
        self._refresh_status(self.locale("api.status.running"))
        item = self.scenarios[index]
        try:
            raw_scenario = self.scenario_loader.load(item.path)
            variables = self._resolve_variables(raw_scenario)
            runner = _make_runner(self.runner_factory, raw_scenario, variables)
            result = runner.run(raw_scenario, variables)
            if inspect.isawaitable(result):
                result = await result
            if not isinstance(result, ApiRun):
                raise TypeError("runner returned an invalid result")
            self.repository.save(result)
            self.selected_run = self.repository.load(result.id)
            self.selected_run_index = next(
                (
                    index
                    for index, summary in enumerate(self.repository.list())
                    if summary.id == result.id
                ),
                None,
            )
            self.selected_scenario_index = None
            self.active_pane = "detail"
            self.run_summaries = tuple(self.repository.list())
            await self._refresh_lists()
            self._refresh_detail()
            self._refresh_status(
                self.locale("api.status.saved", status=_status_label(result.status))
            )
        except ApiConfigError as error:
            variable_message = self.locale("api.error.variable")
            message = (
                error.user_message
                if error.user_message.startswith(variable_message)
                else self.locale("api.error.config")
            )
            self._show_inline_error(message)
        except ApiTransportError:
            self._show_inline_error(self.locale("api.error.transport"))
        except ApiPersistenceError:
            self._show_inline_error(self.locale("api.error.repository"))
        except ApiDomainError:
            self._show_inline_error(self.locale("api.error.run"))
        except ConfigurationError:
            self._show_inline_error(self.locale("api.error.config"))
        except (OSError, UnicodeError, TypeError, ValueError):
            self._show_inline_error(self.locale("api.error.run"))
        finally:
            self._busy = False

    def _resolve_variables(self, scenario: ApiScenario) -> dict[str, str]:
        try:
            config = load_config(
                self._project_dir(), paths=ConfigPaths.project_only(self._project_dir())
            ).api
            names = _referenced_variable_names(scenario)
            resolution = resolve_variables(
                names,
                config.variables,
                scenario.variables,
                os.environ,
                {},
            )
        except ConfigurationError as error:
            raise ApiConfigError(self.locale("api.error.config")) from error
        if resolution.missing_names:
            missing = ", ".join(sorted(resolution.missing_names))
            raise ApiConfigError(f"{self.locale('api.error.variable')} 缺少：{missing}。")
        return dict(resolution.values)

    async def _export_run(self, request: ApiExportRequest, *, force: bool = False) -> None:
        if self.selected_run is None:
            return
        self._busy = True
        self._refresh_status(self.locale("api.status.exporting"))
        try:
            exporter = (
                self.exporter_factory()
                if self.exporter_factory is not None
                else ApiEvidenceExporter()
            )
            result = await asyncio.to_thread(
                exporter.export,
                self.selected_run,
                request.destination,
                theme=request.theme,
                force=force,
            )
            if not isinstance(result, ApiExportResult):
                raise TypeError("exporter returned an invalid result")
            self._refresh_status(self.locale("api.status.exported"))
            self.app.push_screen(
                ApiExportResultScreen(locale=self.locale, result=result, run=self.selected_run)
            )
        except ApiPersistenceError as error:
            if not force and "导出目录已存在" in error.user_message:
                self.app.push_screen(
                    ApiExportOverwriteDialog(
                        locale=self.locale,
                        destination=request.destination,
                    ),
                    lambda confirmed: self._handle_export_overwrite(request, confirmed),
                )
            else:
                self._open_export_dialog(request, self.locale("api.error.export_retry"))
        except (
            ApiConfigError,
            ApiDomainError,
            FontResolutionError,
            OSError,
            UnicodeError,
            TypeError,
            ValueError,
        ):
            self._open_export_dialog(request, self.locale("api.error.export_retry"))
        finally:
            self._busy = False

    def _show_inline_error(self, message: str) -> None:
        safe = message if "发生了什么" in message else self.locale("api.error.generic")
        self._refresh_status(self.locale("api.status.error"))
        self.query_one("#api-detail", ApiDetail).update(
            self._wrap(
                (
                    self.locale("api.detail.error_title"),
                    safe,
                    self.locale("api.detail.error_hint"),
                )
            )
        )

    def _project_dir(self) -> Path:
        try:
            return Path(self.repository.root).parents[2]
        except IndexError:
            return Path.cwd()


def _redact_scenario(scenario: ApiScenario, path: Path) -> ApiScenario:
    redactor = Redactor.with_configured_values(
        value for value in scenario.variables.values() if isinstance(value, str) and value
    )
    steps = tuple(_redact_step(step, redactor) for step in scenario.steps)
    return ApiScenario(
        name=redactor.text(scenario.name),
        variables={},
        source=path.name,
        steps=steps,
    )


def _redact_step(step: ApiStep, redactor: Redactor) -> ApiStep:
    request = step.request
    query = redactor.json_value(request.query)
    form = redactor.json_value(request.form)
    return ApiStep(
        name=redactor.text(step.name),
        request=ApiRequest(
            method=request.method,
            url=redactor.url(request.url),
            headers=redactor.headers(request.headers),
            query=query if isinstance(query, dict) else {},
            json_body=redactor.json_value(request.json_body),
            body=None if request.body is None else redactor.text(request.body),
            form=form if isinstance(form, dict) else {},
            multipart=tuple(
                {"name": redactor.text(part.name), "value": redactor.text(part.value)}
                for part in request.multipart
            ),
            timeout_seconds=request.timeout_seconds,
            follow_redirects=request.follow_redirects,
            verify_tls=request.verify_tls,
        ),
        assertions=tuple(
            ApiAssertion(
                kind=assertion.kind,
                expected=redactor.json_value(assertion.expected),
                location=None if assertion.location is None else redactor.text(assertion.location),
                operator=None if assertion.operator is None else redactor.text(assertion.operator),
            )
            for assertion in step.assertions
        ),
    )


def _make_runner(
    factory: RunnerFactory, scenario: ApiScenario, variables: Mapping[str, str]
) -> Any:
    try:
        parameters = tuple(inspect.signature(factory).parameters.values())
    except (TypeError, ValueError):
        return factory(scenario, variables)
    positional = tuple(
        parameter
        for parameter in parameters
        if parameter.kind
        in {inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD}
    )
    has_varargs = any(
        parameter.kind is inspect.Parameter.VAR_POSITIONAL for parameter in parameters
    )
    if has_varargs or len(positional) >= 2:
        return factory(scenario, variables)
    if positional:
        return factory(scenario)
    return factory()


def _referenced_variable_names(scenario: ApiScenario) -> set[str]:
    names: set[str] = set()
    for step in scenario.steps:
        _collect_placeholder_names(step.request.model_dump(mode="python"), names)
    return names


def _collect_placeholder_names(value: Any, names: set[str]) -> None:
    if isinstance(value, str):
        start = 0
        while True:
            opening = value.find("{{", start)
            if opening < 0:
                return
            closing = value.find("}}", opening + 2)
            if closing < 0:
                return
            candidate = value[opening + 2 : closing]
            if candidate.replace("_", "a").isalnum() and candidate[0:1].isalpha():
                names.add(candidate)
            start = closing + 2
    elif isinstance(value, Mapping):
        for item in value.values():
            _collect_placeholder_names(item, names)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _collect_placeholder_names(item, names)


def _status_label(status: str) -> str:
    labels = {
        "PASS": "通过 (PASS)",
        "FAIL": "失败 (FAIL)",
        "CONFIG_ERROR": "配置错误 (CONFIG_ERROR)",
        "RUNTIME_ERROR": "运行错误 (RUNTIME_ERROR)",
        "SKIP": "跳过 (SKIP)",
    }
    return labels.get(status, f"未知状态 ({status})")


def _inline_json(value: object) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    except (TypeError, ValueError):
        return "null"


__all__ = ["ApiScreen"]
