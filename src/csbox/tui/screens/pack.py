"""Pack delivery preview, destination selection, recovery, and result screens."""

from __future__ import annotations

import asyncio
import inspect
import stat
from collections.abc import Callable
from dataclasses import fields, is_dataclass, replace
from pathlib import Path
from typing import Any, Literal

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical, VerticalScroll
from textual.css.query import NoMatches
from textual.events import Resize
from textual.reactive import reactive
from textual.screen import ModalScreen, Screen
from textual.widgets import Button, Input, Static

from csbox.core.display_width import truncate_cells
from csbox.core.text_layout import wrap_cells
from csbox.locales import Translator
from csbox.tui.help import HELP_BINDINGS

PackPlanFactory = Callable[..., object]
_WorkflowState = Literal["idle", "awaiting_overwrite", "planning", "publishing"]

_REJECTION_LABELS = {
    "env": "真实 .env",
    "private-key": "私钥",
    "hard-coded-secret": "硬编码 secret",
    "deep-secret-scan": "深度 secret 扫描",
    "check-failed": "检查项目",
    "path-unsafe": "不安全路径",
    "path-conflict": "路径冲突",
}


class PackDestinationInput(Input):
    """Keep destination typing local and avoid layout work per keystroke."""

    value = reactive("", layout=False, init=False)


class PackConfirmationScreen(Screen[None]):
    """Show a truthful plan and complete the delivery flow from one screen."""

    BINDINGS = (
        Binding("tab", "focus_next_control", "下一个控件", show=False, priority=True),
        Binding(
            "shift+tab",
            "focus_previous_control",
            "上一个控件",
            show=False,
            priority=True,
        ),
        Binding("escape", "cancel", "返回"),
        Binding("q", "cancel", "返回", show=False),
    )

    def __init__(
        self,
        plan: object,
        locale: Translator,
        *,
        pack_action: Callable[[object], Any] | None = None,
        plan_factory: PackPlanFactory | None = None,
    ) -> None:
        super().__init__(name="pack-confirmation")
        self.default_plan = plan
        self.plan = plan
        self.locale = locale
        self.pack_action = pack_action
        self.plan_factory = plan_factory
        self.default_destination = _absolute_path(_plan_destination(plan))
        self.destination = self.default_destination
        self.confirmed = False
        self._working = False
        self._workflow_state: _WorkflowState = "idle"
        self._overwrite_token = 0
        self._pending_overwrite_plan: object | None = None
        self._status_message = ""

    def compose(self) -> ComposeResult:
        yield Vertical(
            Static(self.locale("pack.title"), id="pack-title", markup=False),
            VerticalScroll(
                Static(self._summary(), id="pack-summary", markup=False),
                Static(self.locale("pack.destination.label"), id="pack-destination-label"),
                PackDestinationInput(
                    value=str(self.default_destination),
                    id="pack-destination-input",
                ),
                Static("", id="pack-destination-validation", markup=False),
                Static(self.locale("pack.destination.hint"), id="pack-destination-hint"),
                Horizontal(
                    Button(
                        self.locale("pack.edit_destination"),
                        id="pack-edit-destination",
                    ),
                    Button(
                        self.locale("pack.restore_default"),
                        id="pack-restore-default",
                    ),
                    Button(
                        self.locale("pack.update_preview"),
                        id="pack-update-preview",
                    ),
                    id="pack-destination-actions",
                ),
                Static(self._items(), id="pack-items", markup=False),
                id="pack-body-scroll",
            ),
            Static("", id="pack-status", markup=False),
            Horizontal(
                Button(self.locale("pack.confirm"), id="pack-confirm", variant="primary"),
                Button(self.locale("pack.cancel"), id="pack-cancel"),
                id="pack-actions",
            ),
            Static(self.locale("pack.shortcuts"), id="pack-footer", markup=False),
            id="pack-layout",
        )

    def on_mount(self) -> None:
        self.call_after_refresh(self._refresh_content)
        self.call_after_refresh(self._focus_destination)

    def _focus_destination(self) -> None:
        self.query_one("#pack-destination-input", Input).focus()

    @property
    def is_working(self) -> bool:
        return self._working

    def on_resize(self, event: Resize) -> None:
        del event
        self.call_after_refresh(self._refresh_content)

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id != "pack-destination-input":
            return
        self._refresh_destination_validation(event.value)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "pack-destination-input":
            return
        if self._apply_destination():
            self.query_one("#pack-confirm", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id
        if button_id == "pack-confirm":
            self.action_confirm()
        elif button_id == "pack-cancel":
            self.action_cancel()
        elif button_id == "pack-edit-destination":
            self.query_one("#pack-destination-input", Input).focus()
        elif button_id == "pack-restore-default":
            self._restore_default()
        elif button_id == "pack-update-preview" and self._apply_destination():
            self.query_one("#pack-confirm", Button).focus()

    def action_focus_next_control(self) -> None:
        self.focus_next(self._control_selector())

    def action_focus_previous_control(self) -> None:
        self.focus_previous(self._control_selector())

    def action_cancel(self) -> None:
        if self._workflow_state != "idle":
            return
        self.app.pop_screen()

    def action_confirm(self) -> None:
        if self.confirmed or self._workflow_state != "idle":
            return
        if not self._apply_destination():
            return
        if _rejected(self.plan):
            return
        if bool(getattr(self.plan, "output_exists", False)):
            self._start_overwrite_confirmation(self.plan)
            return
        self._execute(self.plan)

    def _execute(self, plan: object) -> None:
        if self._workflow_state != "idle":
            return
        self._workflow_state = "publishing"
        self.confirmed = True
        self._set_working(True, self.locale("pack.status.packing"))
        self.run_worker(
            self._execute_in_background(plan),
            name="pack-publish",
            group="pack-publish",
            exclusive=True,
            exit_on_error=False,
        )

    async def _execute_in_background(self, plan: object) -> None:
        try:
            if self.pack_action is None:
                self.confirmed = False
                self._set_working(False)
                self._set_status(self.locale("pack.no_service"))
                return
            result = await asyncio.to_thread(self.pack_action, plan)
            if inspect.isawaitable(result):
                result = await result
        except Exception as error:
            self.confirmed = False
            self._set_working(False)
            if _is_conflict_error(error) or (
                _is_plan_changed_error(error)
                and not bool(getattr(error, "details", ()))
                and _is_regular_target(_plan_destination(plan))
            ):
                self._workflow_state = "idle"
                self._start_overwrite_confirmation(plan)
                return
            self._set_status(self._failure_message(error))
            self.query_one("#pack-destination-input", Input).focus()
            return

        self.confirmed = False
        self._set_working(False)
        if _looks_like_report(result):
            self._set_status("")
            self.app.push_screen(PackResultScreen(report=result, locale=self.locale))
            return
        self._set_status(
            self.locale(
                "pack.success.incomplete",
                path=_display_path(_plan_destination(plan)),
            )
        )

    def _start_overwrite_confirmation(self, plan: object) -> None:
        if self._workflow_state != "idle":
            return
        self._workflow_state = "awaiting_overwrite"
        self._pending_overwrite_plan = plan
        self._overwrite_token += 1
        token = self._overwrite_token
        self.app.push_screen(
            PackOverwriteDialog(
                destination=_plan_destination(plan),
                locale=self.locale,
            ),
            lambda confirmed: self._handle_overwrite(token, confirmed),
        )

    def _handle_overwrite(self, token: int, confirmed: bool) -> None:
        if token != self._overwrite_token or self._workflow_state != "awaiting_overwrite":
            return
        plan = self._pending_overwrite_plan
        self._pending_overwrite_plan = None
        self._overwrite_token += 1
        self._workflow_state = "idle"
        if not confirmed:
            self._set_status(self.locale("pack.overwrite.cancelled"))
            return
        if plan is None:
            return
        try:
            force_plan = _copy_plan(plan, force=True)
        except Exception as error:
            self._set_status(self._failure_message(error))
            return
        self._execute(force_plan)

    def _apply_destination(self) -> bool:
        if self._workflow_state != "idle":
            return False
        destination_input = self.query_one("#pack-destination-input", Input)
        value = destination_input.value
        validation = _local_destination_error(value, self.locale)
        if validation is not None:
            self._show_destination_error(validation)
            return False

        requested = _absolute_path(Path(value))
        target_error = _existing_target_error(requested, self.locale)
        if target_error is not None:
            self._show_destination_error(target_error)
            return False

        current = _absolute_path(_plan_destination(self.plan))
        if requested == current:
            self.destination = requested
            self._set_status("")
            self._refresh_destination_validation(value)
            return True

        self._start_plan_update(requested)
        return False

    def _start_plan_update(self, destination: Path) -> None:
        if self._workflow_state != "idle":
            return
        self._workflow_state = "planning"
        self._set_working(True, self.locale("pack.status.planning"))
        self.run_worker(
            self._update_plan_in_background(destination),
            name="pack-plan",
            group="pack-plan",
            exclusive=True,
            exit_on_error=False,
        )

    async def _update_plan_in_background(self, destination: Path) -> None:
        try:
            candidate = await asyncio.to_thread(self._build_plan, destination)
            candidate_destination = _absolute_path(_plan_destination(candidate))
        except Exception as error:
            self._set_working(False)
            self._show_destination_error(self._plan_failure_message(error))
            return

        if candidate_destination != destination:
            self._set_working(False)
            self._show_destination_error(
                self.locale("pack.destination.directory", path=str(destination))
            )
            return

        self.plan = candidate
        self.destination = destination
        self._set_working(False)
        self._set_status("")
        self._refresh_content()
        self.query_one("#pack-confirm", Button).focus()

    def _build_plan(self, destination: Path) -> object:
        factory = self.plan_factory
        if factory is None:
            return _copy_plan(self.plan, destination=destination)
        return _call_plan_factory(factory, destination)

    def _restore_default(self) -> None:
        if self._workflow_state != "idle":
            return
        self.plan = self.default_plan
        self.destination = self.default_destination
        destination_input = self.query_one("#pack-destination-input", Input)
        destination_input.value = str(self.default_destination)
        self._set_status("")
        self._refresh_content()
        destination_input.focus()

    def _refresh_content(self) -> None:
        self.query_one("#pack-summary", Static).update(self._summary())
        self.query_one("#pack-items", Static).update(self._items())
        self.query_one("#pack-destination-hint", Static).update(
            self._fit(
                self.locale("pack.destination.hint"),
                self._content_width("#pack-destination-hint"),
            )
        )
        self.query_one("#pack-footer", Static).update(
            truncate_line(
                self.locale("pack.shortcuts"),
                self._content_width("#pack-footer"),
            )
        )
        self._refresh_destination_validation(self.query_one("#pack-destination-input", Input).value)
        self._render_status()

    def _refresh_destination_validation(self, value: str) -> None:
        validation = _local_destination_error(value, self.locale)
        self.query_one("#pack-destination-validation", Static).update(
            ""
            if validation is None
            else self._fit(
                validation,
                self._content_width("#pack-destination-validation"),
            )
        )
        self.query_one("#pack-confirm", Button).disabled = (
            self._working or validation is not None or bool(_rejected(self.plan))
        )

    def _show_destination_error(self, message: str) -> None:
        self._set_status(message)
        self.query_one("#pack-destination-input", Input).focus()

    def _set_status(self, message: str) -> None:
        self._status_message = message
        if self.is_mounted:
            self._render_status()

    def _set_working(self, working: bool, message: str | None = None) -> None:
        self._working = working
        if not working and self._workflow_state in {"planning", "publishing"}:
            self._workflow_state = "idle"
        if message is not None:
            self._set_status(message)
        for widget_id in (
            "pack-destination-input",
            "pack-edit-destination",
            "pack-restore-default",
            "pack-update-preview",
            "pack-confirm",
            "pack-cancel",
        ):
            self.query_one(f"#{widget_id}").disabled = working
        self._refresh_destination_validation(self.query_one("#pack-destination-input", Input).value)

    def _render_status(self) -> None:
        try:
            status = self.query_one("#pack-status", Static)
        except NoMatches:
            return
        status.update(self._fit(self._status_message, self._content_width("#pack-status")))

    def _failure_message(self, error: Exception) -> str:
        kind = getattr(error, "kind", None)
        if kind == "verify_failed":
            return self.locale("pack.error.verify")
        if kind == "destination_unsafe":
            return self.locale("pack.destination.unsafe")
        if kind in {
            "destination_exists",
            "destination_permission",
            "destination_unavailable",
            "publish_failed",
        }:
            return self.locale("pack.error.destination")
        if kind in {"plan_changed", "source_changed"}:
            return self.locale("pack.error.plan")
        if kind == "content_rejected":
            return self.locale("pack.error.rejected")
        detail = _safe_error_text(error)
        if "ZIP 校验" in detail or "ZIP 条目" in detail or "manifest" in detail:
            return self.locale("pack.error.verify")
        if "输出路径不可安全使用" in detail:
            return self.locale("pack.destination.unsafe")
        if "输出路径" in detail or "输出文件" in detail or "目标路径" in detail:
            return self.locale("pack.error.destination")
        if "打包计划已变化" in detail:
            return self.locale("pack.error.plan")
        if "敏感或不安全" in detail:
            return self.locale("pack.error.rejected")
        return self.locale("pack.error")

    def _plan_failure_message(self, error: Exception) -> str:
        kind = getattr(error, "kind", None)
        if kind == "destination_unsafe":
            return self.locale("pack.destination.unsafe")
        if kind == "destination_unavailable":
            return self.locale("pack.destination.unavailable")
        if kind == "content_rejected":
            return self.locale("pack.error.rejected")
        detail = _safe_error_text(error)
        if "输出路径不可安全使用" in detail:
            return self.locale("pack.destination.unsafe")
        if "输出路径不可用" in detail:
            return self.locale("pack.destination.unavailable")
        if "敏感或不安全" in detail:
            return self.locale("pack.error.rejected")
        return self.locale("pack.destination.plan")

    def _summary(self) -> str:
        project_type = _text(getattr(self.plan, "project_type", None), "unknown")
        included = _as_items(getattr(self.plan, "included", getattr(self.plan, "entries", ())))
        excluded = _as_items(getattr(self.plan, "excluded", ()))
        rejected = _as_items(getattr(self.plan, "rejected", ()))
        warnings = _as_items(getattr(self.plan, "warnings", ()))
        source_bytes = getattr(self.plan, "source_bytes", 0)
        source_root = _display_path(getattr(self.plan, "source_root", Path.cwd()))
        destination = _display_path(self.destination)
        line = self.locale(
            "pack.summary",
            project_type=project_type,
            included=len(included),
            excluded=len(excluded),
            rejected=len(rejected),
            source_bytes=source_bytes,
        )
        if warnings:
            line += f"\n警告：{len(warnings)} 项（不阻塞打包）"
        return self._fit(
            f"项目：{source_root}\n{line}\n输出：{destination}",
            self._content_width("#pack-summary"),
        )

    def _items(self) -> str:
        width = self._content_width("#pack-items")
        included = _display_items(getattr(self.plan, "included", getattr(self.plan, "entries", ())))
        excluded = _display_items(getattr(self.plan, "excluded", ()))
        rejected = _rejected(self.plan)
        warnings = _display_items(getattr(self.plan, "warnings", ()))
        lines = ["included:"]
        for item in included[:8]:
            lines.extend(_wrapped_item_lines("+", item, width))
        if len(included) > 8:
            lines.append(f"  + … ({len(included) - 8})")
        lines.append("excluded:")
        for item in excluded[:8]:
            lines.extend(_wrapped_item_lines("-", item, width))
        if len(excluded) > 8:
            lines.append(f"  - … ({len(excluded) - 8})")
        lines.append("rejected:")
        if rejected:
            kinds = _rejection_kinds(rejected)
            lines.extend(
                self._fit_lines(
                    self.locale(
                        "pack.rejected.summary",
                        count=len(rejected),
                        kinds=kinds,
                    ),
                    width,
                )
            )
            lines.extend(self._fit_lines(self.locale("pack.rejected.next"), width))
        else:
            lines.append(self.locale("pack.rejected.none"))
        lines.append("warnings:")
        for item in warnings[:8]:
            lines.extend(_wrapped_item_lines("?", item, width))
        if len(warnings) > 8:
            lines.append(f"  ? … ({len(warnings) - 8})")
        return "\n".join(lines)

    def _fit_lines(self, value: str, width: int) -> list[str]:
        return list(wrap_cells(value, width))

    def _fit(self, value: str, width: int) -> str:
        return "\n".join(
            line
            for logical_line in value.splitlines()
            for line in wrap_cells(logical_line, max(2, width))
        )

    def _content_width(self, selector: str) -> int:
        try:
            panel = self.query_one(selector, Static)
            content_width = panel.content_region.width
            panel_width = panel.size.width
        except NoMatches:
            content_width = 0
            panel_width = 0
        available_width = content_width or (panel_width or self.size.width or 80) - 2
        if panel_width:
            available_width = min(available_width, max(2, panel_width - 2))
        return max(2, available_width)

    @staticmethod
    def _control_selector() -> str:
        return (
            "#pack-destination-input, #pack-edit-destination, #pack-restore-default, "
            "#pack-update-preview, #pack-confirm, #pack-cancel"
        )


class PackOverwriteDialog(ModalScreen[bool]):
    """Require an explicit confirmation before replacing one exact ZIP file."""

    BINDINGS = (
        *HELP_BINDINGS,
        Binding("escape", "cancel", "取消"),
        Binding("q", "cancel", "取消", show=False),
    )

    def __init__(self, *, destination: Path, locale: Translator) -> None:
        super().__init__(name="pack-overwrite")
        self.destination = _absolute_path(destination)
        self.locale = locale

    def compose(self) -> ComposeResult:
        yield Container(
            Vertical(
                Static(self.locale("pack.overwrite.title"), id="pack-overwrite-heading"),
                VerticalScroll(
                    Static(id="pack-overwrite-message", markup=False),
                    id="pack-overwrite-scroll",
                ),
                Horizontal(
                    Button(
                        self.locale("pack.overwrite.confirm"),
                        id="pack-overwrite-confirm",
                        variant="primary",
                    ),
                    Button(self.locale("pack.overwrite.cancel"), id="pack-overwrite-cancel"),
                    id="pack-overwrite-actions",
                ),
                id="pack-overwrite-card",
            ),
            id="pack-overwrite-dialog",
        )

    def on_mount(self) -> None:
        self.call_after_refresh(self._refresh)
        self.call_after_refresh(self._focus_confirm)

    def on_resize(self, event: Resize) -> None:
        del event
        self.call_after_refresh(self._refresh)

    def _focus_confirm(self) -> None:
        self.query_one("#pack-overwrite-confirm", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "pack-overwrite-confirm":
            self.dismiss(True)
        elif event.button.id == "pack-overwrite-cancel":
            self.dismiss(False)

    def action_cancel(self) -> None:
        self.dismiss(False)

    def _refresh(self) -> None:
        message = self.locale("pack.overwrite.message", path=str(self.destination))
        width = max(2, self.query_one("#pack-overwrite-message", Static).content_region.width)
        self.query_one("#pack-overwrite-message", Static).update(
            "\n".join(line for part in message.splitlines() for line in wrap_cells(part, width))
        )


class PackResultScreen(Screen[None]):
    """Show the exact published artifact and the backend's verification result."""

    BINDINGS = (
        Binding("enter", "go_back", "返回", show=False, priority=True),
        Binding("escape", "go_back", "返回"),
        Binding("q", "go_back", "返回", show=False),
    )

    def __init__(self, *, report: object, locale: Translator) -> None:
        super().__init__(name="pack-result")
        self.report = report
        self.locale = locale

    def compose(self) -> ComposeResult:
        yield Container(
            Vertical(
                Static(self.locale("pack.result.title"), id="pack-result-heading"),
                VerticalScroll(
                    Static(id="pack-result-body", markup=False),
                    id="pack-result-scroll",
                ),
                Horizontal(
                    Button(
                        self.locale("pack.result.return"),
                        id="pack-result-return",
                        variant="primary",
                    ),
                    id="pack-result-actions",
                ),
                Static(self.locale("pack.result.shortcuts"), id="pack-result-footer"),
                id="pack-result-card",
            ),
            id="pack-result-dialog",
        )

    def on_mount(self) -> None:
        self.call_after_refresh(self._refresh)
        self.call_after_refresh(self._focus_return)

    def on_resize(self, event: Resize) -> None:
        del event
        self.call_after_refresh(self._refresh)

    def _focus_return(self) -> None:
        self.query_one("#pack-result-return", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "pack-result-return":
            self.action_go_back()

    def action_go_back(self) -> None:
        self.app.pop_screen()

    def _refresh(self) -> None:
        body = self.query_one("#pack-result-body", Static)
        width = max(2, body.content_region.width or self.size.width - 8)
        destination = _plan_destination(self.report)
        entries = _as_items(getattr(self.report, "entries", getattr(self.report, "included", ())))
        excluded = _as_items(getattr(self.report, "excluded", ()))
        rejected = _as_items(getattr(self.report, "rejected", ()))
        archive_bytes = getattr(self.report, "archive_bytes", None)
        verified = bool(getattr(self.report, "verified", False))
        verification_status = getattr(self.report, "verification_status", "not_requested")
        if verified:
            verification = self.locale("pack.result.verify.pass")
        elif verification_status == "not_requested":
            verification = self.locale("pack.result.verify.not_requested")
        else:
            verification = str(verification_status)
        size = (
            f"{archive_bytes} bytes"
            if isinstance(archive_bytes, int) and archive_bytes >= 0
            else self.locale("pack.result.size.unknown")
        )
        message = "\n".join(
            (
                self.locale("pack.result.file", path=_display_path(destination)),
                self.locale("pack.result.name", name=Path(destination).name),
                self.locale("pack.result.size", size=size),
                self.locale("pack.result.verify", status=verification),
                self.locale(
                    "pack.result.summary",
                    included=len(entries),
                    excluded=len(excluded),
                    rejected=len(rejected),
                ),
            )
        )
        body.update(
            "\n".join(line for part in message.splitlines() for line in wrap_cells(part, width))
        )


def _call_plan_factory(factory: PackPlanFactory, destination: Path) -> object:
    try:
        parameters = tuple(inspect.signature(factory).parameters.values())
    except (TypeError, ValueError):
        return factory(destination)

    destination_parameter = next(
        (parameter for parameter in parameters if parameter.name == "destination"),
        None,
    )
    if destination_parameter is not None:
        if destination_parameter.kind is inspect.Parameter.POSITIONAL_ONLY:
            return factory(destination)
        return factory(destination=destination)
    if any(parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters):
        return factory(destination=destination)
    if any(parameter.kind is inspect.Parameter.VAR_POSITIONAL for parameter in parameters):
        return factory(destination)
    positional = tuple(
        parameter
        for parameter in parameters
        if parameter.kind
        in {inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD}
    )
    if positional:
        return factory(destination)
    return _copy_plan(factory(), destination=destination)


def _copy_plan(
    plan: object,
    *,
    destination: Path | None = None,
    force: bool | None = None,
) -> object:
    updates: dict[str, object] = {}
    if destination is not None:
        updates.update(
            {
                "destination": destination,
                "output_filename": destination.name,
                "output_exists": _path_exists(destination),
            }
        )
    if force is not None:
        updates["force"] = force
        if destination is None:
            updates["output_exists"] = _path_exists(_plan_destination(plan))
    if not updates:
        return plan

    model_copy = getattr(plan, "model_copy", None)
    if callable(model_copy):
        model_fields = getattr(type(plan), "model_fields", {})
        return model_copy(
            update={key: value for key, value in updates.items() if key in model_fields}
        )
    if is_dataclass(plan):
        names = {field.name for field in fields(plan)}
        return replace(plan, **{key: value for key, value in updates.items() if key in names})
    return _PlanOverride(plan, updates)


class _PlanOverride:
    __slots__ = ("_base", "_updates")

    def __init__(self, base: object, updates: dict[str, object]) -> None:
        self._base = base
        self._updates = updates

    def __getattr__(self, name: str) -> object:
        try:
            return self._updates[name]
        except KeyError:
            return getattr(self._base, name)


def _plan_destination(plan: object) -> Path:
    value = getattr(plan, "destination", None)
    if value is not None:
        return Path(value)
    source_root = Path(getattr(plan, "source_root", Path.cwd()))
    filename = getattr(plan, "output_filename", "student-project-course.zip")
    return source_root / str(filename)


def _absolute_path(value: Path) -> Path:
    return Path(value).absolute()


def _display_path(value: Path | str) -> str:
    return str(_absolute_path(Path(value)))


def _local_destination_error(value: str, locale: Translator) -> str | None:
    if not value.strip():
        return locale("pack.destination.empty")
    if any(character in value for character in "\x00\r\n"):
        return locale("pack.destination.invalid")
    return None


def _existing_target_error(path: Path, locale: Translator) -> str | None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return None
    except OSError:
        return None
    if stat.S_ISLNK(metadata.st_mode):
        return locale("pack.destination.unsafe")
    if stat.S_ISDIR(metadata.st_mode):
        return locale("pack.destination.directory", path=str(path))
    if not stat.S_ISREG(metadata.st_mode):
        return locale("pack.destination.unsafe")
    return None


def _path_exists(path: Path) -> bool:
    try:
        path.lstat()
    except (FileNotFoundError, OSError):
        return False
    return True


def _as_items(value: object) -> tuple[object, ...]:
    if value is None or isinstance(value, (str, bytes)):
        return () if value is None else (value,)
    try:
        return tuple(value)  # type: ignore[arg-type]
    except TypeError:
        return ()


def _display_items(value: object) -> tuple[str, ...]:
    return tuple(truncate_line(str(item), 80) for item in _as_items(value))


def _rejected(plan: object) -> tuple[str, ...]:
    return _display_items(getattr(plan, "rejected", ()))


def _rejection_kinds(rejected: tuple[str, ...]) -> str:
    counts: dict[str, int] = {}
    for item in rejected:
        category = item.rsplit(":", 1)[-1] if ":" in item else "check-failed"
        label = _REJECTION_LABELS.get(category, "安全检查")
        counts[label] = counts.get(label, 0) + 1
    return (
        "、".join(
            f"{label}（{count} 项）" if count > 1 else label for label, count in counts.items()
        )
        or "安全检查"
    )


def _wrapped_item_lines(prefix: str, item: str, width: int) -> tuple[str, ...]:
    item_width = max(1, width - 4)
    wrapped = wrap_cells(item, item_width)
    return tuple(
        (f"  {prefix} {line}" if index == 0 else f"    {line}")
        for index, line in enumerate(wrapped)
    )


def truncate_line(value: str, width: int) -> str:
    return truncate_cells(value, max(1, width), ellipsis="…")


def _text(value: object, fallback: str) -> str:
    if value is None:
        return fallback
    return str(value)


def _safe_error_text(error: Exception) -> str:
    try:
        return str(error)
    except Exception:
        return ""


def _is_conflict_error(error: Exception) -> bool:
    kind = getattr(error, "kind", None)
    if kind is not None:
        return kind == "destination_exists"
    return isinstance(error, FileExistsError) or "目标文件已存在" in _safe_error_text(error)


def _is_plan_changed_error(error: Exception) -> bool:
    kind = getattr(error, "kind", None)
    if kind is not None:
        return kind == "plan_changed"
    return "打包计划已变化" in _safe_error_text(error)


def _is_regular_target(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except (FileNotFoundError, OSError):
        return False
    return stat.S_ISREG(metadata.st_mode)


def _looks_like_report(value: object) -> bool:
    return value is not None and hasattr(value, "destination") and hasattr(value, "archive_bytes")


__all__ = [
    "PackConfirmationScreen",
    "PackDestinationInput",
    "PackOverwriteDialog",
    "PackPlanFactory",
    "PackResultScreen",
]
