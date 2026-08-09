import pytest

from csbox.core.events import TerminalEvent, TerminalEventClock, TerminalEventType, TerminalSize


def test_event_clock_records_monotonic_relative_sequence() -> None:
    times = iter((100.0, 100.25))
    clock = TerminalEventClock(monotonic=lambda: next(times))

    event = clock.next(TerminalEventType.OUTPUT, b"ok")

    assert (event.sequence, event.monotonic_time, event.relative_time) == (1, 100.25, 0.25)
    assert event.type is TerminalEventType.OUTPUT
    assert event.payload == b"ok"


def test_event_clock_accepts_typed_resize_payload() -> None:
    times = iter((1.0, 1.5))
    event = TerminalEventClock(monotonic=lambda: next(times)).next(
        TerminalEventType.RESIZE, TerminalSize(columns=120, rows=40)
    )

    assert event.payload == TerminalSize(columns=120, rows=40)
    assert TerminalEventType.CAPTURE.value == "capture"


@pytest.mark.parametrize(
    ("columns", "rows"),
    [("120", 40), (120, "40"), (True, 40), (120, False)],
)
def test_terminal_size_rejects_non_integer_or_boolean_dimensions(
    columns: object, rows: object
) -> None:
    with pytest.raises(TypeError):
        TerminalSize(columns=columns, rows=rows)  # type: ignore[arg-type]


@pytest.mark.parametrize(("columns", "rows"), [(0, 40), (-1, 40), (120, 0), (120, -1)])
def test_terminal_size_rejects_non_positive_dimensions(columns: int, rows: int) -> None:
    with pytest.raises(ValueError):
        TerminalSize(columns=columns, rows=rows)


def test_event_clock_rejects_an_invalid_event_type() -> None:
    clock = TerminalEventClock(monotonic=lambda: 1.0)

    with pytest.raises(TypeError):
        clock.next("output", b"ok")  # type: ignore[arg-type]


def test_event_clock_rejects_an_unsupported_payload() -> None:
    clock = TerminalEventClock(monotonic=lambda: 1.0)

    with pytest.raises(TypeError):
        clock.next(TerminalEventType.OUTPUT, object())  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("event_type", "payload"),
    [
        (TerminalEventType.OUTPUT, b"output"),
        (TerminalEventType.INPUT, b"input"),
        (TerminalEventType.RESIZE, TerminalSize(columns=120, rows=40)),
        (TerminalEventType.MARK, "checkpoint"),
        (TerminalEventType.CAPTURE, "capture.png"),
        (TerminalEventType.EXIT, 0),
        (TerminalEventType.EXIT, None),
    ],
)
def test_terminal_event_accepts_only_its_allowed_payload(
    event_type: TerminalEventType, payload: bytes | str | TerminalSize | int | None
) -> None:
    event = TerminalEvent(
        sequence=1,
        monotonic_time=1.0,
        relative_time=0.0,
        type=event_type,
        payload=payload,
    )

    assert event.payload == payload


@pytest.mark.parametrize(
    ("event_type", "payload"),
    [
        (TerminalEventType.OUTPUT, "output"),
        (TerminalEventType.INPUT, "input"),
        (TerminalEventType.RESIZE, "120x40"),
        (TerminalEventType.MARK, b"checkpoint"),
        (TerminalEventType.CAPTURE, b"capture.png"),
        (TerminalEventType.EXIT, "0"),
        (TerminalEventType.EXIT, True),
    ],
)
def test_terminal_event_rejects_payloads_for_other_event_types(
    event_type: TerminalEventType, payload: bytes | str | TerminalSize | int | None
) -> None:
    with pytest.raises(TypeError):
        TerminalEvent(
            sequence=1,
            monotonic_time=1.0,
            relative_time=0.0,
            type=event_type,
            payload=payload,
        )
