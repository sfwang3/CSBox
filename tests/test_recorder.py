from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from csbox.core.events import TerminalEvent, TerminalEventType, TerminalSize
from csbox.lab.recorder import AsciicastV3Reader, AsciicastV3Recorder, RecorderError


def event(
    sequence: int,
    relative_time: float,
    event_type: TerminalEventType,
    payload: bytes | str | TerminalSize | int | None,
) -> TerminalEvent:
    return TerminalEvent(
        sequence=sequence,
        monotonic_time=100.0 + relative_time,
        relative_time=relative_time,
        type=event_type,
        payload=payload,
    )


def test_recorder_writes_v3_header_filtered_env_and_all_event_codes(tmp_path: Path) -> None:
    cast_path = tmp_path / "session.cast"
    recorder = AsciicastV3Recorder(
        cast_path,
        columns=80,
        rows=24,
        env={"SHELL": "/bin/bash", "TERM": "xterm-256color", "TOKEN": "secret"},
    )

    recorder.record(event(1, 0.1, TerminalEventType.OUTPUT, b"hello"))
    recorder.record(event(2, 0.2, TerminalEventType.INPUT, b"ls\r"))
    recorder.record(event(3, 0.3, TerminalEventType.RESIZE, TerminalSize(120, 40)))
    recorder.record(event(4, 0.4, TerminalEventType.MARK, "checkpoint"))
    recorder.record(event(5, 0.5, TerminalEventType.CAPTURE, "capture-1"))
    recorder.record(event(6, 0.6, TerminalEventType.EXIT, 0))
    recorder.close()

    lines = cast_path.read_text(encoding="utf-8").splitlines()
    header = json.loads(lines[0])
    assert header["version"] == 3
    assert header["term"] == {"cols": 80, "rows": 24, "type": "xterm-256color"}
    assert header["env"] == {"SHELL": "/bin/bash", "TERM": "xterm-256color"}
    assert [json.loads(line)[1] for line in lines[1:]] == ["o", "i", "r", "m", "m", "x"]
    assert json.loads(lines[3])[2] == "120x40"
    assert json.loads(lines[-1])[1:] == ["x", "0"]


def test_recorder_diffuses_millisecond_rounding_error_between_adjacent_events(
    tmp_path: Path,
) -> None:
    cast_path = tmp_path / "session.cast"
    recorder = AsciicastV3Recorder(cast_path, columns=80, rows=24)

    for sequence, relative_time in enumerate((0.0004, 0.0008, 0.0012, 0.0016), start=1):
        recorder.record(event(sequence, relative_time, TerminalEventType.OUTPUT, b"."))
    recorder.close()

    intervals = [json.loads(line)[0] for line in cast_path.read_text().splitlines()[1:]]
    assert intervals == [0.0, 0.001, 0.0, 0.001]
    assert sum(intervals) == pytest.approx(0.002)


def test_recorder_decodes_utf8_split_across_output_events(tmp_path: Path) -> None:
    cast_path = tmp_path / "session.cast"
    encoded = "项目".encode()
    recorder = AsciicastV3Recorder(cast_path, columns=80, rows=24)

    recorder.record(event(1, 0.01, TerminalEventType.OUTPUT, encoded[:2]))
    recorder.record(event(2, 0.02, TerminalEventType.OUTPUT, encoded[2:4]))
    recorder.record(event(3, 0.03, TerminalEventType.OUTPUT, encoded[4:]))
    recorder.close()

    payload = "".join(json.loads(line)[2] for line in cast_path.read_text().splitlines()[1:])
    assert payload == "项目"


def test_incomplete_output_and_input_tails_are_flushed_before_exit(tmp_path: Path) -> None:
    cast_path = tmp_path / "session.cast"
    recorder = AsciicastV3Recorder(cast_path, columns=80, rows=24)
    recorder.record(event(1, 0.1, TerminalEventType.OUTPUT, b"\xe4"))
    recorder.record(event(2, 0.2, TerminalEventType.INPUT, b"\xe6"))
    recorder.record(event(3, 0.3, TerminalEventType.EXIT, 7))

    with pytest.raises(RecorderError, match="closed"):
        recorder.record(event(4, 0.4, TerminalEventType.OUTPUT, b"late"))
    recorder.close()

    events = [json.loads(line) for line in cast_path.read_text().splitlines()[1:]]
    assert events[-3:] == [[0.0, "o", "�"], [0.0, "i", "�"], [0.1, "x", "7"]]
    assert events[-1][1] == "x"


def test_stop_without_exit_still_flushes_an_incomplete_utf8_tail(tmp_path: Path) -> None:
    cast_path = tmp_path / "session.cast"
    recorder = AsciicastV3Recorder(cast_path, columns=80, rows=24)
    recorder.record(event(1, 0.1, TerminalEventType.OUTPUT, b"\xe4"))

    recorder.close()

    events = [json.loads(line) for line in cast_path.read_text().splitlines()[1:]]
    assert events[-1] == [0.0, "o", "�"]


def test_recorder_rejects_events_after_bounded_shutdown(tmp_path: Path) -> None:
    recorder = AsciicastV3Recorder(tmp_path / "session.cast", columns=80, rows=24)
    recorder.close()

    assert not recorder.is_alive
    with pytest.raises(RecorderError, match="closed"):
        recorder.record(event(1, 0.0, TerminalEventType.OUTPUT, b"late"))


def test_recorder_close_is_bounded_and_propagates_writer_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_write = AsciicastV3Recorder._write_json_line
    event_write_started = threading.Event()
    release_write = threading.Event()
    calls = 0

    def blocked_event_write(stream: object, value: object) -> None:
        nonlocal calls
        calls += 1
        if calls > 1:
            event_write_started.set()
            release_write.wait(timeout=1.0)
            raise OSError("disk failed")
        original_write(stream, value)

    monkeypatch.setattr(AsciicastV3Recorder, "_write_json_line", staticmethod(blocked_event_write))
    recorder = AsciicastV3Recorder(
        tmp_path / "session.cast", columns=80, rows=24, close_timeout=0.05
    )
    recorder.record(event(1, 0.1, TerminalEventType.OUTPUT, b"hello"))
    assert event_write_started.wait(timeout=0.5)

    started = time.monotonic()
    with pytest.raises(RecorderError, match="did not stop"):
        recorder.close()
    assert time.monotonic() - started < 0.5

    release_write.set()
    recorder._thread.join(timeout=0.5)
    with pytest.raises(RecorderError, match="writer failed") as error:
        recorder.close()
    assert isinstance(error.value.__cause__, OSError)


def test_close_retry_keeps_waiting_after_stop_was_enqueued(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_write = AsciicastV3Recorder._write_json_line
    event_write_started = threading.Event()
    release_write = threading.Event()
    calls = 0

    def gated_event_write(stream: object, value: object) -> None:
        nonlocal calls
        calls += 1
        if calls > 1:
            event_write_started.set()
            release_write.wait(timeout=1.0)
        original_write(stream, value)

    monkeypatch.setattr(AsciicastV3Recorder, "_write_json_line", staticmethod(gated_event_write))
    recorder = AsciicastV3Recorder(
        tmp_path / "session.cast", columns=80, rows=24, close_timeout=0.03
    )
    recorder.record(event(1, 0.1, TerminalEventType.OUTPUT, b"blocked"))
    assert event_write_started.wait(timeout=0.5)

    with pytest.raises(RecorderError, match="did not stop"):
        recorder.close()
    assert recorder.is_alive
    with pytest.raises(RecorderError, match="did not stop"):
        recorder.close()

    release_write.set()
    recorder.close()
    recorder.close()
    assert not recorder.is_alive


def test_close_retry_enqueues_stop_after_a_full_queue_drains(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_write_events = AsciicastV3Recorder._write_events
    writer_waiting = threading.Event()
    allow_drain = threading.Event()

    def gated_write_events(recorder: AsciicastV3Recorder, stream: object) -> None:
        writer_waiting.set()
        allow_drain.wait(timeout=1.0)
        original_write_events(recorder, stream)

    monkeypatch.setattr(AsciicastV3Recorder, "_write_events", gated_write_events)
    recorder = AsciicastV3Recorder(
        tmp_path / "session.cast",
        columns=80,
        rows=24,
        queue_size=1,
        enqueue_timeout=0.01,
        close_timeout=0.03,
    )
    assert writer_waiting.wait(timeout=0.5)
    recorder.record(event(1, 0.1, TerminalEventType.OUTPUT, b"queued"))

    with pytest.raises(RecorderError, match="queueing stop"):
        recorder.close()
    assert recorder.is_alive

    allow_drain.set()
    deadline = time.monotonic() + 0.5
    while not recorder._queue.empty() and time.monotonic() < deadline:
        time.sleep(0.001)
    recorder.close()
    assert not recorder.is_alive


def test_concurrent_and_repeated_close_never_return_before_writer_stops(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_write = AsciicastV3Recorder._write_json_line
    event_write_started = threading.Event()
    release_write = threading.Event()
    calls = 0

    def gated_event_write(stream: object, value: object) -> None:
        nonlocal calls
        calls += 1
        if calls > 1:
            event_write_started.set()
            release_write.wait(timeout=1.0)
        original_write(stream, value)

    monkeypatch.setattr(AsciicastV3Recorder, "_write_json_line", staticmethod(gated_event_write))
    recorder = AsciicastV3Recorder(
        tmp_path / "session.cast", columns=80, rows=24, close_timeout=0.5
    )
    recorder.record(event(1, 0.1, TerminalEventType.OUTPUT, b"blocked"))
    assert event_write_started.wait(timeout=0.5)
    completed: list[str] = []
    errors: list[BaseException] = []

    def close_in_thread(name: str) -> None:
        try:
            recorder.close()
            completed.append(name)
        except BaseException as exc:
            errors.append(exc)

    first = threading.Thread(target=close_in_thread, args=("first",))
    first.start()
    deadline = time.monotonic() + 0.5
    while recorder._queue.qsize() != 1 and time.monotonic() < deadline:
        time.sleep(0.001)
    second = threading.Thread(target=close_in_thread, args=("second",))
    second.start()

    time.sleep(0.03)
    assert completed == []
    release_write.set()
    first.join(timeout=0.5)
    second.join(timeout=0.5)

    assert sorted(completed) == ["first", "second"]
    assert errors == []
    recorder.close()
    assert not recorder.is_alive


def test_reader_keeps_valid_events_around_corruption_and_truncated_tail(tmp_path: Path) -> None:
    cast_path = tmp_path / "session.cast"
    cast_path.write_text(
        '{"version":3,"term":{"cols":80,"rows":24,"type":"xterm-256color"}}\n'
        "# generated by a future recorder\n"
        '[0.1,"o","first"]\n'
        "not-json\n"
        '[0.1,"z","future"]\n'
        '[0.1,"o","second"]\n'
        '[0.1,"o","unfinished"',
        encoding="utf-8",
    )

    result = AsciicastV3Reader(cast_path).read()

    assert result.header["version"] == 3
    assert [(item.interval, item.code, item.data) for item in result.events] == [
        (0.1, "o", "first"),
        (0.2, "o", "second"),
    ]
    assert any("line 4" in warning for warning in result.warnings)
    assert any("unknown event" in warning for warning in result.warnings)
    assert any("truncated" in warning for warning in result.warnings)


def test_reader_warns_and_skips_non_finite_intervals(tmp_path: Path) -> None:
    cast_path = tmp_path / "session.cast"
    cast_path.write_text(
        '{"version":3,"term":{"cols":80,"rows":24}}\n'
        '[NaN,"o","bad"]\n'
        '[Infinity,"o","also bad"]\n'
        '[0.1,"o","valid"]\n',
        encoding="utf-8",
    )

    result = AsciicastV3Reader(cast_path).read()

    assert [(item.interval, item.data) for item in result.events] == [(0.1, "valid")]
    assert sum("invalid cast event" in warning for warning in result.warnings) == 2


def test_reader_carries_multiple_unknown_intervals_to_the_next_known_event(
    tmp_path: Path,
) -> None:
    cast_path = tmp_path / "session.cast"
    cast_path.write_text(
        '{"version":3,"term":{"cols":80,"rows":24}}\n'
        '[0.1,"o","first"]\n'
        '[2.0,"future-a","ignored"]\n'
        '[3.0,"future-b","ignored"]\n'
        '[0.2,"o","second"]\n'
        '[4.0,"future-tail","ignored"]\n',
        encoding="utf-8",
    )

    result = AsciicastV3Reader(cast_path).read()

    assert [(item.interval, item.data) for item in result.events] == [
        (0.1, "first"),
        (5.2, "second"),
    ]
    assert sum(item.interval for item in result.events) == pytest.approx(5.3)
    assert len([warning for warning in result.warnings if "unknown event" in warning]) == 3


@pytest.mark.parametrize(
    "header",
    [
        {"version": 3.0, "term": {"cols": 80, "rows": 24}},
        {"version": 3},
        {"version": 3, "term": [80, 24]},
        {"version": 3, "term": {"rows": 24}},
        {"version": 3, "term": {"cols": 80}},
        {"version": 3, "term": {"cols": True, "rows": 24}},
        {"version": 3, "term": {"cols": 80, "rows": False}},
        {"version": 3, "term": {"cols": 0, "rows": 24}},
        {"version": 3, "term": {"cols": 80, "rows": -1}},
        {"version": 3, "term": {"cols": 80, "rows": 24, "type": 256}},
    ],
)
def test_reader_rejects_non_strict_v3_headers(tmp_path: Path, header: object) -> None:
    cast_path = tmp_path / "session.cast"
    cast_path.write_text(json.dumps(header) + "\n", encoding="utf-8")

    with pytest.raises(RecorderError, match="invalid asciicast v3 header"):
        AsciicastV3Reader(cast_path).read()


def test_reader_rejects_a_malformed_first_data_line_as_the_header(tmp_path: Path) -> None:
    cast_path = tmp_path / "session.cast"
    cast_path.write_text("# comment\n{truncated\n", encoding="utf-8")

    with pytest.raises(RecorderError, match="header on line 2"):
        AsciicastV3Reader(cast_path).read()
