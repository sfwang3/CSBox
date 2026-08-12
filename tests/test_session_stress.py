from __future__ import annotations

import json
from pathlib import Path

import pytest

from csbox.core.events import TerminalEvent, TerminalEventType, TerminalSize
from csbox.lab.recorder import AsciicastV3Reader, AsciicastV3Recorder
from csbox.lab.replay import CHECKPOINT_EVENTS, CheckpointStore, ReplayService


def event(
    sequence: int, relative_time: float, event_type: TerminalEventType, payload: object
) -> TerminalEvent:
    return TerminalEvent(
        sequence=sequence,
        monotonic_time=relative_time,
        relative_time=relative_time,
        type=event_type,
        payload=payload,  # type: ignore[arg-type]
    )


@pytest.mark.stress
def test_recorder_and_replay_handle_one_hundred_thousand_events_with_bounded_checkpoints(
    tmp_path: Path,
) -> None:
    cast_path = tmp_path / "100k.cast"
    checkpoint_path = tmp_path / "100k.checkpoints.json"
    recorder = AsciicastV3Recorder(cast_path, columns=24, rows=5, queue_size=2048)
    for sequence in range(1, 100_001):
        recorder.record(event(sequence, sequence * 0.001, TerminalEventType.OUTPUT, b"."))
    recorder.record(event(100_001, 100.001, TerminalEventType.EXIT, 0))
    recorder.close()

    read_result = AsciicastV3Reader(cast_path).read()
    service = ReplayService(cast_path, checkpoint_store=CheckpointStore(checkpoint_path))
    end = service.seek(service.duration)

    assert len(read_result.events) == 100_001
    assert end.rows == 5
    assert end.columns == 24
    assert len(service.checkpoints) <= 1 + (len(read_result.events) // CHECKPOINT_EVENTS) + 1
    assert service.seek(10.0) == service.seek(10.0)


@pytest.mark.stress
def test_long_output_resize_and_cjk_capture_state_are_stable(tmp_path: Path) -> None:
    cast_path = tmp_path / "long-output.cast"
    checkpoint_path = tmp_path / "long-output.checkpoints.json"
    output = ("ASCII 中文 mixed e\u0301 ").encode("utf-8") * 1_500
    recorder = AsciicastV3Recorder(cast_path, columns=40, rows=6)
    sequence = 1
    recorder.record(event(sequence, 1.0, TerminalEventType.OUTPUT, output))
    for index, (columns, rows) in enumerate(((80, 10), (20, 3), (100, 12), (40, 6))):
        sequence += 1
        recorder.record(
            event(sequence, 2.0 + index, TerminalEventType.RESIZE, TerminalSize(columns, rows))
        )
        sequence += 1
        recorder.record(event(sequence, 2.5 + index, TerminalEventType.OUTPUT, "中文".encode()))
    recorder.record(event(sequence + 1, 7.0, TerminalEventType.EXIT, 0))
    recorder.close()

    service = ReplayService(cast_path, checkpoint_store=CheckpointStore(checkpoint_path))
    at_end = service.seek(service.duration)
    before_end = service.seek(max(0.0, service.duration - 0.01))

    assert (at_end.rows, at_end.columns) == (6, 40)
    assert any(cell.character == "中" and cell.width == 2 for row in at_end.cells for cell in row)
    assert before_end == service.seek(max(0.0, service.duration - 0.01))
    assert (service.seek(1.0).rows, service.seek(1.0).columns) == (6, 40)


@pytest.mark.stress
def test_repeated_backward_seek_does_not_change_terminal_state(tmp_path: Path) -> None:
    cast_path = tmp_path / "seek.cast"
    write = [
        {"version": 3, "term": {"cols": 12, "rows": 3}},
        [1.0, "o", "hello中文"],
        [1.0, "r", "20x4"],
        [1.0, "o", "\x1b[2;1H世界"],
        [1.0, "r", "12x3"],
        [1.0, "o", "done"],
    ]
    cast_path.write_text(
        "\n".join(json.dumps(item, ensure_ascii=False) for item in write) + "\n",
        encoding="utf-8",
    )
    service = ReplayService(cast_path)
    expected = service.seek(2.0)

    for target in (5.0, 1.0, 4.0, 0.0, 2.0, 5.0, 2.0):
        service.seek(target)

    assert service.seek(2.0) == expected
