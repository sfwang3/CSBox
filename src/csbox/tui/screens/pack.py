"""Small, explicit confirmation screen for a real PackPlan adapter."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.css.query import NoMatches
from textual.events import Resize
from textual.screen import Screen
from textual.widgets import Button, Static

from csbox.core.display_width import truncate_cells
from csbox.core.text_layout import wrap_cells
from csbox.locales import Translator


class PackConfirmationScreen(Screen[None]):
    """Show a dry-run plan and require an explicit Enter-confirmed action."""

    BINDINGS = (
        ("escape", "cancel", "取消"),
        ("q", "cancel", "取消"),
    )

    def __init__(
        self,
        plan: object,
        locale: Translator,
        *,
        pack_action: Callable[[object], Any] | None = None,
    ) -> None:
        super().__init__(name="pack-confirmation")
        self.plan = plan
        self.locale = locale
        self.pack_action = pack_action
        self.confirmed = False
        self._status_message = ""

    def compose(self) -> ComposeResult:
        yield Vertical(
            Static(self.locale("pack.title"), id="pack-title", markup=False),
            Static(self._summary(), id="pack-summary", markup=False),
            Static(self._items(), id="pack-items", markup=False),
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
        self.query_one("#pack-cancel", Button).focus()
        if _rejected(self.plan):
            self.query_one("#pack-confirm", Button).disabled = True
        self._refresh_content()

    def on_resize(self, event: Resize) -> None:
        del event
        self._refresh_content()

    def _refresh_content(self) -> None:
        self.query_one("#pack-summary", Static).update(self._summary())
        self.query_one("#pack-items", Static).update(self._items())
        self.query_one("#pack-footer", Static).update(
            truncate_cells(
                self.locale("pack.shortcuts"),
                self._content_width("#pack-footer"),
                ellipsis="…",
            )
        )
        self._render_status()

    def _set_status(self, message: str) -> None:
        self._status_message = message
        self._render_status()

    def _render_status(self) -> None:
        status = self.query_one("#pack-status", Static)
        status.update(
            "\n".join(wrap_cells(self._status_message, self._content_width("#pack-status")))
        )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "pack-confirm":
            self.action_confirm()
        elif event.button.id == "pack-cancel":
            self.action_cancel()

    def action_cancel(self) -> None:
        self.app.pop_screen()

    def action_confirm(self) -> None:
        if _rejected(self.plan) or self.confirmed:
            return
        self.confirmed = True
        try:
            if self.pack_action is None:
                self._set_status(self.locale("pack.no_service"))
                self.confirmed = False
                return
            self.pack_action(self.plan)
        except Exception:
            self.confirmed = False
            self._set_status(self.locale("pack.error"))
            return
        self._set_status(self.locale("pack.success"))

    def _summary(self) -> str:
        project_type = _text(getattr(self.plan, "project_type", None), "unknown")
        source_bytes = getattr(self.plan, "source_bytes", 0)
        included = _items(getattr(self.plan, "included", getattr(self.plan, "entries", ())))
        excluded = _items(getattr(self.plan, "excluded", ()))
        rejected = _items(getattr(self.plan, "rejected", ()))
        line = self.locale(
            "pack.summary",
            project_type=project_type,
            included=len(included),
            excluded=len(excluded),
            rejected=len(rejected),
            source_bytes=source_bytes,
        )
        width = self._content_width("#pack-summary")
        return "\n".join(wrap_cells(line, width))

    def _items(self) -> str:
        width = self._content_width("#pack-items")
        included = _items(getattr(self.plan, "included", getattr(self.plan, "entries", ())))
        excluded = _items(getattr(self.plan, "excluded", ()))
        rejected = _items(getattr(self.plan, "rejected", ()))
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
            lines.append(self.locale("pack.rejected.hidden", count=len(rejected)))
        else:
            lines.append(self.locale("pack.rejected.none"))
        return "\n".join(lines)

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


def _wrapped_item_lines(prefix: str, item: str, width: int) -> tuple[str, ...]:
    item_width = max(1, width - 4)
    wrapped = wrap_cells(item, item_width)
    return tuple(
        (f"  {prefix} {line}" if index == 0 else f"    {line}")
        for index, line in enumerate(wrapped)
    )


def _items(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, (str, bytes)):
        return (truncate_cells(str(value), 80, ellipsis="…"),)
    try:
        return tuple(truncate_cells(str(item), 80, ellipsis="…") for item in value)  # type: ignore[union-attr]
    except TypeError:
        return ()


def _rejected(plan: object) -> tuple[str, ...]:
    return _items(getattr(plan, "rejected", ()))


def _text(value: object, fallback: str) -> str:
    if value is None:
        return fallback
    return Path(str(value)).name if isinstance(value, Path) else str(value)


__all__ = ["PackConfirmationScreen"]
