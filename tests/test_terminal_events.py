from csbox.core.events import TerminalEventClock, TerminalEventType, TerminalSize


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
