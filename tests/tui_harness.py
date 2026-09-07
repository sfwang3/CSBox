from __future__ import annotations

from collections.abc import Callable
from time import monotonic
from typing import Any


async def wait_until(
    pilot: Any,
    predicate: Callable[[], bool],
    *,
    timeout: float = 5.0,
) -> None:
    deadline = monotonic() + timeout
    while monotonic() < deadline:
        if predicate():
            return
        await pilot.pause()
    assert predicate()


async def wait_for_screen(
    pilot: Any,
    app: Any,
    expected: type[Any] | str,
    *,
    timeout: float = 5.0,
) -> Any:
    await wait_until(
        pilot,
        lambda: (
            app.screen.name == expected
            if isinstance(expected, str)
            else isinstance(app.screen, expected)
        ),
        timeout=timeout,
    )
    return app.screen


async def wait_for_widget(
    pilot: Any,
    root: Any,
    selector: str,
    *,
    timeout: float = 5.0,
) -> Any:
    await wait_until(pilot, lambda: bool(tuple(root.query(selector))), timeout=timeout)
    return root.query_one(selector)


async def wait_for_focus(
    pilot: Any,
    app: Any,
    target: Any,
    *,
    timeout: float = 5.0,
) -> None:
    await wait_until(
        pilot,
        lambda: getattr(app.screen, "focused", None) is target,
        timeout=timeout,
    )


async def focus_and_press(
    pilot: Any,
    app: Any,
    selector: str,
    key: str = "enter",
    *,
    timeout: float = 5.0,
) -> Any:
    target = await wait_for_widget(pilot, app.screen, selector, timeout=timeout)
    if hasattr(target, "has_class"):
        await wait_until(
            pilot,
            lambda: not target.has_class("-active"),
            timeout=timeout,
        )
    target.focus()
    await wait_for_focus(pilot, app, target, timeout=timeout)
    await pilot.press(key)
    return target


async def wait_for_disabled(
    pilot: Any,
    root: Any,
    selector: str,
    disabled: bool,
    *,
    timeout: float = 5.0,
) -> Any:
    target = await wait_for_widget(pilot, root, selector, timeout=timeout)
    await wait_until(
        pilot,
        lambda: getattr(target, "disabled", None) is disabled,
        timeout=timeout,
    )
    return target


async def wait_for_busy(
    pilot: Any,
    screen: Any,
    busy: bool,
    *,
    timeout: float = 5.0,
) -> None:
    await wait_until(
        pilot,
        lambda: getattr(screen, "is_working", None) is busy,
        timeout=timeout,
    )


async def wait_for_worker_start(
    pilot: Any,
    started: Callable[[], bool],
    *,
    timeout: float = 5.0,
) -> None:
    await wait_until(pilot, started, timeout=timeout)


async def wait_for_rendered(
    pilot: Any,
    root: Any,
    selector: str,
    *,
    timeout: float = 5.0,
) -> Any:
    widget = await wait_for_widget(pilot, root, selector, timeout=timeout)
    await wait_until(
        pilot,
        lambda: bool(str(widget.renderable).strip()),
        timeout=timeout,
    )
    return widget
