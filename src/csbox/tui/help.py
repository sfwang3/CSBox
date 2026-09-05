"""Beginner-first global and contextual help for the Textual UI."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Container, Vertical, VerticalScroll
from textual.events import Resize
from textual.screen import ModalScreen
from textual.widgets import Button, Static

from csbox.core.display_width import truncate_cells
from csbox.core.text_layout import wrap_cells
from csbox.locales import Translator


class HelpContext(StrEnum):
    """The small, explicit set of user-facing help contexts."""

    GLOBAL = "global"
    HOME = "home"
    LAB_START = "lab_start"
    RECORDS = "records"
    REVIEW = "review"
    EVIDENCE_LIST = "evidence_list"
    EVIDENCE_EDITOR = "evidence_editor"
    EVIDENCE_BROWSER = "evidence_browser"
    REPORT_CONFIG = "report_config"
    REPORT_EXPORT = "report_export"
    CHECK = "check"
    PACK = "pack"
    API = "api"


# These bindings intentionally use the app namespace. The same tuple can be
# attached to an App, a regular Screen, or a ModalScreen without duplicating
# the action implementation. They are normal-priority bindings so printable
# ``?`` remains available to Input and TextArea widgets.
HELP_BINDINGS = (
    Binding("?", "app.show_help", "帮助"),
    Binding("f1", "app.show_help", "帮助", show=False),
)


_SCREEN_CONTEXTS = {
    "home": HelpContext.HOME,
    "records": HelpContext.RECORDS,
    "review": HelpContext.REVIEW,
    "evidence-sets": HelpContext.EVIDENCE_LIST,
    "evidence-editor": HelpContext.EVIDENCE_EDITOR,
    "evidence-capture-browser": HelpContext.EVIDENCE_BROWSER,
    "report-profile": HelpContext.REPORT_CONFIG,
    "report-export": HelpContext.REPORT_EXPORT,
    "report-export-result": HelpContext.REPORT_EXPORT,
    "project-check": HelpContext.CHECK,
    "pack-confirmation": HelpContext.PACK,
    "pack-result": HelpContext.PACK,
    "api": HelpContext.API,
    "api-export": HelpContext.API,
    "api-export-result": HelpContext.API,
}

_CLASS_CONTEXTS = {
    "LabStartDialog": HelpContext.LAB_START,
    "CaptureTitleDialog": HelpContext.REVIEW,
    "EvidenceSetTitleDialog": HelpContext.EVIDENCE_LIST,
    "EvidenceItemDialog": HelpContext.EVIDENCE_EDITOR,
    "ExportDialog": HelpContext.RECORDS,
    "ExportOverwriteDialog": HelpContext.RECORDS,
    "ExportResultScreen": HelpContext.RECORDS,
    "ApiOpenApiImportDialog": HelpContext.API,
    "ApiOpenApiOverwriteDialog": HelpContext.API,
    "ApiScenarioOverwriteDialog": HelpContext.API,
    "ApiExportDialog": HelpContext.API,
    "ApiExportOverwriteDialog": HelpContext.API,
    "ApiQuickCreateDialog": HelpContext.API,
    "PackOverwriteDialog": HelpContext.PACK,
    "ReportExportOverwriteDialog": HelpContext.REPORT_EXPORT,
    "ConfirmDialog": HelpContext.GLOBAL,
    "UnavailableDialog": HelpContext.GLOBAL,
    "HelpDialog": HelpContext.GLOBAL,
}


def context_for_screen(screen: object) -> HelpContext:
    """Return the explicit beginner context for a current screen instance."""

    name = getattr(screen, "name", None)
    if isinstance(name, str) and name in _SCREEN_CONTEXTS:
        return _SCREEN_CONTEXTS[name]
    return _CLASS_CONTEXTS.get(type(screen).__name__, HelpContext.GLOBAL)


def help_text(context: HelpContext, locale: Translator) -> str:
    """Load concise help content for *context* from the active locale."""

    if context is HelpContext.GLOBAL:
        return locale("help.global.body")
    return locale(f"help.context.{context.value}")


class HelpDialog(ModalScreen[None]):
    """A single scrollable help surface that leaves the covered screen intact."""

    BINDINGS = (
        Binding("escape", "close_help", "关闭", show=False, priority=True),
        Binding("q", "close_help", "关闭", show=False, priority=True),
        Binding("up", "scroll_up_help", "向上滚动", show=False, priority=True),
        Binding("down", "scroll_down_help", "向下滚动", show=False, priority=True),
        Binding("pageup", "page_up_help", "向上翻页", show=False, priority=True),
        Binding("pagedown", "page_down_help", "向下翻页", show=False, priority=True),
    )

    def __init__(
        self,
        *,
        locale: Translator,
        context: HelpContext = HelpContext.GLOBAL,
    ) -> None:
        super().__init__(name="help")
        self.locale = locale
        self.context = context

    def compose(self) -> ComposeResult:
        yield Container(
            Vertical(
                Static(self.locale("help.title"), id="help-title", markup=False),
                VerticalScroll(
                    Static(id="help-body", markup=False),
                    id="help-scroll",
                ),
                Button(
                    self.locale("help.close"),
                    id="help-close",
                    variant="primary",
                ),
                Static(self.locale("help.footer"), id="help-footer", markup=False),
                id="help-card",
            ),
            id="help-dialog",
        )

    def on_mount(self) -> None:
        self.call_after_refresh(self._refresh_content)
        self.call_after_refresh(self._focus_close)

    def on_resize(self, event: Resize) -> None:
        del event
        self.call_after_refresh(self._refresh_content)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "help-close":
            self.action_close_help()

    def action_close_help(self) -> None:
        self.dismiss(None)

    def action_scroll_up_help(self) -> None:
        self.query_one("#help-scroll", VerticalScroll).scroll_up(animate=False)

    def action_scroll_down_help(self) -> None:
        self.query_one("#help-scroll", VerticalScroll).scroll_down(animate=False)

    def action_page_up_help(self) -> None:
        self.query_one("#help-scroll", VerticalScroll).scroll_page_up(animate=False)

    def action_page_down_help(self) -> None:
        self.query_one("#help-scroll", VerticalScroll).scroll_page_down(animate=False)

    def _focus_close(self) -> None:
        self.query_one("#help-close", Button).focus()

    def _refresh_content(self) -> None:
        body = self.query_one("#help-body", Static)
        body_width = body.content_region.width or self.size.width - 10
        # A vertical scrollbar may be introduced after the first body
        # measurement. Reserve one display cell so the wrapped renderable
        # remains safe when that happens at every viewport size.
        body_width = max(2, body_width - 1)
        body.update("\n".join(wrap_cells(help_text(self.context, self.locale), body_width)))

        footer = self.query_one("#help-footer", Static)
        footer_width = footer.content_region.width or self.size.width - 10
        footer.update(
            truncate_cells(self.locale("help.footer"), max(1, footer_width), ellipsis="…")
        )


def open_help(
    app: Any,
    locale: Translator,
    context: HelpContext | None = None,
) -> None:
    """Push Help without rebuilding or mutating the covered workflow."""

    active_screen = getattr(app, "screen", None)
    if isinstance(active_screen, HelpDialog):
        return

    focused = getattr(active_screen, "focused", None)
    screen_context = context_for_screen(active_screen)
    # Home is the beginner entry point, so its discoverable Help key opens
    # the product map first. The explicit HOME context remains available for
    # callers and screen-level tests that need the five-question page guide.
    selected_context = context or (
        HelpContext.GLOBAL if screen_context is HelpContext.HOME else screen_context
    )

    def restore_focus(_: object) -> None:
        if active_screen is None or focused is None:
            return
        try:
            if (
                focused.is_attached
                and focused.screen is active_screen
                and focused.display
                and focused.visible
                and not focused.disabled
            ):
                focused.focus()
        except (AttributeError, RuntimeError):
            return

    app.push_screen(
        HelpDialog(locale=locale, context=selected_context),
        restore_focus,
    )


__all__ = [
    "HELP_BINDINGS",
    "HelpContext",
    "HelpDialog",
    "context_for_screen",
    "help_text",
    "open_help",
]
