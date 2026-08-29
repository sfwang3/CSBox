"""Small API-specific modal dialogs for scenario bootstrap actions."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical, VerticalScroll
from textual.reactive import reactive
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Select, Static

from csbox.api.models import ApiMethod, ApiRun
from csbox.locales import Translator


class ApiQuickCreateInput(Input):
    """Keep bootstrap typing local and avoid layout work per character."""

    value = reactive("", layout=False, init=False)


class ApiOpenApiPathInput(Input):
    """Keep OpenAPI path typing local to the import modal."""

    value = reactive("", layout=False, init=False)


@dataclass(frozen=True, slots=True)
class ApiQuickCreateRequest:
    name: str
    method: ApiMethod
    url: str


class ApiOpenApiImportDialog(ModalScreen[Path | None]):
    """Collect one OpenAPI source path without introducing a native picker."""

    BINDINGS = [
        Binding(
            "up",
            "focus_previous_control",
            "上一个控件",
            show=False,
            priority=True,
        ),
        Binding(
            "down",
            "focus_next_control",
            "下一个控件",
            show=False,
            priority=True,
        ),
        Binding("escape", "cancel", "取消"),
        Binding("q", "cancel", "取消", show=False),
    ]

    def __init__(
        self,
        *,
        locale: Translator,
        initial_path: Path | None = None,
        initial_error: str = "",
    ) -> None:
        super().__init__(name="api-openapi-import")
        self.locale = locale
        self.initial_path = initial_path
        self.initial_error = initial_error

    def compose(self) -> ComposeResult:
        yield Container(
            Vertical(
                Static(self.locale("api.openapi_import.title"), id="api-openapi-heading"),
                VerticalScroll(
                    Static(self.locale("api.openapi_import.path"), classes="field-label"),
                    ApiOpenApiPathInput(
                        value="" if self.initial_path is None else str(self.initial_path),
                        placeholder=self.locale("api.openapi_import.path.placeholder"),
                        id="api-openapi-path",
                        classes="api-openapi-control",
                    ),
                    Static(self.initial_error, id="api-openapi-validation", markup=False),
                    Static(self.locale("api.openapi_import.hint"), id="api-openapi-hint"),
                    id="api-openapi-fields",
                ),
                Horizontal(
                    Button(
                        self.locale("api.openapi_import.submit"),
                        id="api-openapi-submit",
                        classes="api-openapi-control",
                        variant="primary",
                    ),
                    Button(
                        self.locale("api.openapi_import.cancel"),
                        id="api-openapi-cancel",
                        classes="api-openapi-control",
                    ),
                    id="api-openapi-actions",
                ),
                id="api-openapi-card",
            ),
            id="api-openapi-import-dialog",
        )

    def on_mount(self) -> None:
        self.query_one("#api-openapi-path", Input).focus()
        self._refresh_validation()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "api-openapi-path":
            self._refresh_validation()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "api-openapi-path":
            self._submit()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "api-openapi-submit":
            self._submit()
        elif event.button.id == "api-openapi-cancel":
            self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_focus_next_control(self) -> None:
        self.focus_next(".api-openapi-control")

    def action_focus_previous_control(self) -> None:
        self.focus_previous(".api-openapi-control")

    def _submit(self) -> None:
        input_widget = self.query_one("#api-openapi-path", Input)
        value = input_widget.value.strip()
        error = _openapi_path_error(value, self.locale)
        if error:
            self.query_one("#api-openapi-validation", Static).update(error)
            input_widget.focus()
            return
        self.dismiss(Path(value))

    def _refresh_validation(self) -> None:
        value = self.query_one("#api-openapi-path", Input).value.strip()
        error = _openapi_path_error(value, self.locale) if value else ""
        self.query_one("#api-openapi-validation", Static).update(error or self.initial_error)
        self.query_one("#api-openapi-submit", Button).disabled = bool(
            _openapi_path_error(value, self.locale)
        )


def _openapi_path_error(value: str, locale: Translator) -> str:
    if not value:
        return locale("api.openapi_import.validation.path")
    if Path(value).suffix.casefold() not in {".json", ".yaml", ".yml"}:
        return locale("api.openapi_import.validation.format")
    return ""


class ApiScenarioOverwriteDialog(ModalScreen[bool]):
    """Confirm replacing an existing scenario source before force-writing it."""

    BINDINGS = [
        Binding("escape", "cancel", "取消"),
        Binding("q", "cancel", "取消", show=False),
    ]

    def __init__(self, *, locale: Translator, filename: str) -> None:
        super().__init__(name="api-scenario-overwrite")
        self.locale = locale
        self.filename = filename

    def compose(self) -> ComposeResult:
        yield Container(
            Vertical(
                Static(self.locale("api.quick_create.overwrite.title"), id="api-overwrite-heading"),
                Static(
                    self.locale("api.quick_create.overwrite.message", name=self.filename),
                    id="api-overwrite-message",
                    markup=False,
                ),
                Horizontal(
                    Button(
                        self.locale("api.quick_create.overwrite.confirm"),
                        id="api-scenario-overwrite-confirm",
                        variant="primary",
                    ),
                    Button(
                        self.locale("api.quick_create.cancel"),
                        id="api-scenario-overwrite-cancel",
                    ),
                    id="api-scenario-overwrite-actions",
                ),
                id="api-scenario-overwrite-card",
            ),
            id="api-scenario-overwrite-dialog",
        )

    def on_mount(self) -> None:
        self.query_one("#api-scenario-overwrite-confirm", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "api-scenario-overwrite-confirm":
            self.dismiss(True)
        elif event.button.id == "api-scenario-overwrite-cancel":
            self.dismiss(False)

    def action_cancel(self) -> None:
        self.dismiss(False)


class ApiOpenApiOverwriteDialog(ModalScreen[bool]):
    """Confirm replacing matching generated scenarios from one import."""

    BINDINGS = [
        Binding("escape", "cancel", "取消"),
        Binding("q", "cancel", "取消", show=False),
    ]

    def __init__(self, *, locale: Translator, destination: Path) -> None:
        super().__init__(name="api-openapi-overwrite")
        self.locale = locale
        self.destination = destination

    def compose(self) -> ComposeResult:
        yield Container(
            Vertical(
                Static(
                    self.locale("api.openapi_import.overwrite.title"),
                    id="api-openapi-overwrite-heading",
                ),
                Static(
                    self.locale(
                        "api.openapi_import.overwrite.message",
                        path=str(self.destination),
                    ),
                    id="api-openapi-overwrite-message",
                    markup=False,
                ),
                Horizontal(
                    Button(
                        self.locale("api.openapi_import.overwrite.confirm"),
                        id="api-openapi-overwrite-confirm",
                        variant="primary",
                    ),
                    Button(
                        self.locale("api.openapi_import.cancel"),
                        id="api-openapi-overwrite-cancel",
                    ),
                    id="api-openapi-overwrite-actions",
                ),
                id="api-openapi-overwrite-card",
            ),
            id="api-openapi-overwrite-dialog",
        )

    def on_mount(self) -> None:
        self.query_one("#api-openapi-overwrite-confirm", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "api-openapi-overwrite-confirm":
            self.dismiss(True)
        elif event.button.id == "api-openapi-overwrite-cancel":
            self.dismiss(False)

    def action_cancel(self) -> None:
        self.dismiss(False)


class ApiExportDestinationInput(Input):
    """Keep export destination typing local to the API export modal."""

    value = reactive("", layout=False, init=False)


@dataclass(frozen=True, slots=True)
class ApiExportRequest:
    destination: Path
    theme: Literal["dark", "light"] = "dark"


class ApiExportOverwriteDialog(ModalScreen[bool]):
    """Confirm the API exporter's existing-directory force contract."""

    BINDINGS = [
        Binding("escape", "cancel", "取消"),
        Binding("q", "cancel", "取消", show=False),
    ]

    def __init__(self, *, locale: Translator, destination: Path) -> None:
        super().__init__(name="api-export-overwrite")
        self.locale = locale
        self.destination = destination

    def compose(self) -> ComposeResult:
        yield Container(
            Vertical(
                Static(
                    self.locale("api.export.overwrite.title"), id="api-export-overwrite-heading"
                ),
                Static(
                    self.locale(
                        "api.export.overwrite.message",
                        path=str(self.destination),
                    ),
                    id="api-export-overwrite-message",
                    markup=False,
                ),
                Horizontal(
                    Button(
                        self.locale("api.export.overwrite.confirm"),
                        id="api-export-overwrite-confirm",
                        variant="primary",
                    ),
                    Button(
                        self.locale("api.export.cancel"),
                        id="api-export-overwrite-cancel",
                    ),
                    id="api-export-overwrite-actions",
                ),
                id="api-export-overwrite-card",
            ),
            id="api-export-overwrite-dialog",
        )

    def on_mount(self) -> None:
        self.call_after_refresh(self._focus_confirm)

    def _focus_confirm(self) -> None:
        self.query_one("#api-export-overwrite-confirm", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "api-export-overwrite-confirm":
            self.dismiss(True)
        elif event.button.id == "api-export-overwrite-cancel":
            self.dismiss(False)

    def action_cancel(self) -> None:
        self.dismiss(False)


class ApiExportDialog(ModalScreen[ApiExportRequest | None]):
    """Collect only API export destination and existing backend theme options."""

    BINDINGS = [
        Binding(
            "up",
            "focus_previous_control",
            "上一个控件",
            show=False,
            priority=True,
        ),
        Binding(
            "down",
            "focus_next_control",
            "下一个控件",
            show=False,
            priority=True,
        ),
        Binding("escape", "cancel", "取消"),
        Binding("q", "cancel", "取消", show=False),
    ]

    def __init__(
        self,
        *,
        locale: Translator,
        run: ApiRun,
        default_destination: Path,
        initial: ApiExportRequest | None = None,
        error: str = "",
    ) -> None:
        super().__init__(name="api-export")
        self.locale = locale
        self.run = run
        self.default_destination = Path(default_destination)
        self.destination = self.default_destination if initial is None else initial.destination
        self.theme: Literal["dark", "light"] = "dark" if initial is None else initial.theme
        self.error = error

    def compose(self) -> ComposeResult:
        yield Container(
            Vertical(
                Static(self.locale("api.export.title"), id="api-export-heading"),
                Static(
                    self.locale(
                        "api.export.run",
                        id=self.run.id,
                        name=self.run.scenario.name,
                    ),
                    id="api-export-run",
                    markup=False,
                ),
                Static(self.locale("api.export.hint"), id="api-export-hint", markup=False),
                VerticalScroll(
                    Static(self.locale("api.export.destination"), classes="field-label"),
                    ApiExportDestinationInput(
                        value=str(self.destination),
                        id="api-export-destination",
                        classes="api-export-control",
                    ),
                    Static(self.error, id="api-export-validation", markup=False),
                    Button(
                        self.locale("api.export.restore_default"),
                        id="api-export-restore-default",
                        classes="api-export-control",
                    ),
                    Static(self.locale("api.export.theme"), classes="field-label"),
                    Select(
                        [
                            (self.locale("api.export.theme.dark"), "dark"),
                            (self.locale("api.export.theme.light"), "light"),
                        ],
                        value=self.theme,
                        allow_blank=False,
                        id="api-export-theme",
                        classes="api-export-control",
                    ),
                    id="api-export-fields",
                ),
                Horizontal(
                    Button(
                        self.locale("api.export.submit"),
                        id="api-export-submit",
                        classes="api-export-control",
                        variant="primary",
                    ),
                    Button(
                        self.locale("api.export.cancel"),
                        id="api-export-cancel",
                        classes="api-export-control",
                    ),
                    id="api-export-actions",
                ),
                id="api-export-card",
            ),
            id="api-export-dialog",
        )

    def on_mount(self) -> None:
        self.call_after_refresh(self._initialize)

    def _initialize(self) -> None:
        self.query_one("#api-export-destination", Input).focus()
        self._refresh_validation()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "api-export-destination":
            self._refresh_validation()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "api-export-destination":
            self._submit()

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "api-export-theme" and event.value is not Select.BLANK:
            self.theme = str(event.value)  # type: ignore[assignment]

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "api-export-submit":
            self._submit()
        elif event.button.id == "api-export-cancel":
            self.dismiss(None)
        elif event.button.id == "api-export-restore-default":
            self.destination = self.default_destination
            self.query_one("#api-export-destination", Input).value = str(self.destination)

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_focus_next_control(self) -> None:
        self.focus_next(".api-export-control")

    def action_focus_previous_control(self) -> None:
        self.focus_previous(".api-export-control")

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if (
            action in {"focus_next_control", "focus_previous_control"}
            and self.query_one("#api-export-theme", Select).expanded
        ):
            return False
        return super().check_action(action, parameters)

    def _submit(self) -> None:
        input_widget = self.query_one("#api-export-destination", Input)
        value = input_widget.value.strip()
        if not value:
            self._refresh_validation()
            input_widget.focus()
            return
        self.destination = Path(value)
        self.dismiss(ApiExportRequest(destination=self.destination, theme=self.theme))

    def _refresh_validation(self) -> None:
        value = self.query_one("#api-export-destination", Input).value.strip()
        message = self.error or (
            self.locale("api.export.validation.destination") if not value else ""
        )
        self.query_one("#api-export-validation", Static).update(message)
        self.query_one("#api-export-submit", Button).disabled = not bool(value)


class ApiQuickCreateDialog(ModalScreen[ApiQuickCreateRequest | None]):
    """Collect the minimum real request needed for a runnable TOML scenario."""

    BINDINGS = [
        Binding(
            "up",
            "focus_previous_control",
            "上一个控件",
            show=False,
            priority=True,
        ),
        Binding(
            "down",
            "focus_next_control",
            "下一个控件",
            show=False,
            priority=True,
        ),
        Binding("escape", "cancel", "取消"),
        Binding("q", "cancel", "取消", show=False),
    ]

    def __init__(
        self,
        *,
        locale: Translator,
        initial: ApiQuickCreateRequest | None = None,
    ) -> None:
        super().__init__(name="api-quick-create")
        self.locale = locale
        self.initial = initial
        self.method: ApiMethod = initial.method if initial is not None else "GET"

    def compose(self) -> ComposeResult:
        initial_name = "" if self.initial is None else self.initial.name
        initial_url = "" if self.initial is None else self.initial.url
        yield Container(
            Vertical(
                Static(self.locale("api.quick_create.title"), id="api-quick-create-heading"),
                VerticalScroll(
                    Static(self.locale("api.quick_create.name"), classes="field-label"),
                    ApiQuickCreateInput(
                        value=initial_name,
                        placeholder=self.locale("api.quick_create.name.placeholder"),
                        id="api-quick-create-name",
                        classes="api-quick-create-control",
                    ),
                    Static(self.locale("api.quick_create.method"), classes="field-label"),
                    Select(
                        [(method, method) for method in ("GET", "POST", "PUT", "PATCH", "DELETE")],
                        value=self.method,
                        allow_blank=False,
                        id="api-quick-create-method",
                        classes="api-quick-create-control",
                    ),
                    Static(self.locale("api.quick_create.url"), classes="field-label"),
                    ApiQuickCreateInput(
                        value=initial_url,
                        placeholder=self.locale("api.quick_create.url.placeholder"),
                        id="api-quick-create-url",
                        classes="api-quick-create-control",
                    ),
                    Static("", id="api-quick-create-validation", markup=False),
                    Static(self.locale("api.quick_create.hint"), id="api-quick-create-hint"),
                    id="api-quick-create-fields",
                ),
                Horizontal(
                    Button(
                        self.locale("api.quick_create.submit"),
                        id="api-quick-create-submit",
                        classes="api-quick-create-control",
                        variant="primary",
                    ),
                    Button(
                        self.locale("api.quick_create.cancel"),
                        id="api-quick-create-cancel",
                        classes="api-quick-create-control",
                    ),
                    id="api-quick-create-actions",
                ),
                id="api-quick-create-card",
            ),
            id="api-quick-create-dialog",
        )

    def on_mount(self) -> None:
        self.query_one("#api-quick-create-name", Input).focus()
        self._refresh_validation()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id in {"api-quick-create-name", "api-quick-create-url"}:
            self._refresh_validation()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id in {"api-quick-create-name", "api-quick-create-url"}:
            self._submit()

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id != "api-quick-create-method" or event.value is Select.BLANK:
            return
        self.method = str(event.value)  # type: ignore[assignment]

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "api-quick-create-submit":
            self._submit()
        elif event.button.id == "api-quick-create-cancel":
            self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_focus_next_control(self) -> None:
        self.focus_next(".api-quick-create-control")

    def action_focus_previous_control(self) -> None:
        self.focus_previous(".api-quick-create-control")

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if (
            action in {"focus_next_control", "focus_previous_control"}
            and self.query_one("#api-quick-create-method", Select).expanded
        ):
            return False
        return super().check_action(action, parameters)

    def _submit(self) -> None:
        name_input = self.query_one("#api-quick-create-name", Input)
        url_input = self.query_one("#api-quick-create-url", Input)
        name = name_input.value.strip()
        url = url_input.value.strip()
        if not name:
            self._show_validation(self.locale("api.quick_create.validation.name"), name_input)
            return
        if not _valid_http_url(url):
            self._show_validation(self.locale("api.quick_create.validation.url"), url_input)
            return
        self.dismiss(ApiQuickCreateRequest(name=name, method=self.method, url=url))

    def _refresh_validation(self) -> None:
        name = self.query_one("#api-quick-create-name", Input).value.strip()
        url = self.query_one("#api-quick-create-url", Input).value.strip()
        message = ""
        if not name:
            message = self.locale("api.quick_create.validation.name")
        elif url and not _valid_http_url(url):
            message = self.locale("api.quick_create.validation.url")
        self.query_one("#api-quick-create-validation", Static).update(message)
        self.query_one("#api-quick-create-submit", Button).disabled = not (
            bool(name) and _valid_http_url(url)
        )

    def _show_validation(self, message: str, control: Input) -> None:
        self.query_one("#api-quick-create-validation", Static).update(message)
        control.focus()


def _valid_http_url(value: str) -> bool:
    if (
        not value
        or "{{" in value
        or "}}" in value
        or any(character.isspace() or ord(character) < 32 for character in value)
    ):
        return False
    try:
        parsed = urlsplit(value)
    except ValueError:
        return False
    return parsed.scheme.casefold() in {"http", "https"} and bool(parsed.netloc)


__all__ = [
    "ApiOpenApiImportDialog",
    "ApiOpenApiOverwriteDialog",
    "ApiOpenApiPathInput",
    "ApiExportDestinationInput",
    "ApiExportDialog",
    "ApiExportOverwriteDialog",
    "ApiExportRequest",
    "ApiQuickCreateDialog",
    "ApiQuickCreateInput",
    "ApiQuickCreateRequest",
    "ApiScenarioOverwriteDialog",
]
