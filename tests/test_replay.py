from __future__ import annotations

import json
from pathlib import Path

import pytest

from csbox.core.events import TerminalEvent, TerminalEventType, TerminalSize
from csbox.lab.recorder import AsciicastV3Reader
from csbox.lab.replay import (
    CHECKPOINT_VERSION,
    CheckpointStore,
    CheckpointStoreError,
    ReplayService,
)
from csbox.lab.screen import TerminalEmulator, TerminalSnapshot

FIXTURES = Path(__file__).parent / "fixtures" / "sessions"


def row_text(snapshot: TerminalSnapshot, row: int) -> str:
    return "".join(cell.character for cell in snapshot.cells[row] if cell.width != 0)


def write_cast(
    path: Path,
    events: list[tuple[float, str, str]],
    *,
    columns: int = 8,
    rows: int = 4,
) -> None:
    lines = [json.dumps({"version": 3, "term": {"cols": columns, "rows": rows}})]
    lines.extend(json.dumps(list(item), ensure_ascii=False) for item in events)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def output_event(sequence: int, relative_time: float, data: str) -> TerminalEvent:
    return TerminalEvent(
        sequence=sequence,
        monotonic_time=relative_time,
        relative_time=relative_time,
        type=TerminalEventType.OUTPUT,
        payload=data.encode(),
    )


def test_seek_is_deterministic_across_boundaries_resize_exit_and_backward_seek(
    tmp_path: Path,
) -> None:
    cast_path = tmp_path / "mixed.cast"
    cast_path.write_bytes((FIXTURES / "mixed.cast").read_bytes())
    service = ReplayService(
        cast_path,
        checkpoint_store=CheckpointStore(tmp_path / "checkpoints.json"),
    )

    at_zero = service.seek(0)
    between_events = service.seek(2.5)
    after_resize = service.seek(4.0)
    after_exit = service.seek(99.0)
    backward = service.seek(1.0)

    assert (at_zero.rows, at_zero.columns, row_text(at_zero, 0).strip()) == (2, 6, "")
    assert row_text(between_events, 0).startswith("A")
    assert "B" not in row_text(between_events, 0)
    assert (after_resize.rows, after_resize.columns) == (3, 8)
    assert row_text(after_resize, 0).startswith("AB")
    assert row_text(after_exit, 1).startswith("中文")
    assert backward == service.seek(1.0)
    assert row_text(backward, 0).startswith("A")
    assert "B" not in row_text(backward, 0)
    assert (after_exit.relative_time, service.duration) == pytest.approx((6.0, 6.0))
    assert any("unknown event" in warning for warning in service.warnings)


def test_empty_cast_returns_the_initial_terminal_snapshot(tmp_path: Path) -> None:
    cast_path = tmp_path / "empty.cast"
    cast_path.write_text(
        '{"version":3,"term":{"cols":7,"rows":3,"type":"xterm-256color"}}\n',
        encoding="utf-8",
    )

    snapshot = ReplayService(cast_path).seek(500)

    assert (snapshot.rows, snapshot.columns, snapshot.relative_time) == (3, 7, 0.0)
    assert all(not row_text(snapshot, row).strip() for row in range(3))


@pytest.mark.parametrize(
    ("prefix", "tail", "columns", "rows"),
    [
        ("\x1b[31mA", "B", 8, 2),
        ("ABC\x1b[1;2H\x1b[4h", "X", 8, 2),
        ("\x1b[?7labc", "de", 4, 2),
        ("\x1b[2;2H\x1b7\x1b[1;1H", "\x1b8X", 8, 3),
        ("\x1b[2;3r\x1b[3;1H", "one\ntwo\nthree", 8, 4),
        ("\x1b[3g\x1b[1;3H\x1bH\r", "\tX", 8, 2),
        ("\x1b%@\x1b(0", "q", 8, 2),
    ],
    ids=("sgr", "insert-mode", "autowrap", "saved-cursor", "margins", "tabstop", "charset"),
)
def test_checkpoint_seek_matches_continuous_terminal_state(
    tmp_path: Path,
    prefix: str,
    tail: str,
    columns: int,
    rows: int,
) -> None:
    cast_path = tmp_path / "state.cast"
    write_cast(cast_path, [(5.0, "o", prefix), (1.0, "o", tail)], columns=columns, rows=rows)
    continuous = TerminalEmulator(columns=columns, rows=rows)
    continuous.apply(output_event(1, 5.0, prefix))
    continuous.apply(output_event(2, 6.0, tail))

    store = CheckpointStore(tmp_path / "state.json")
    service = ReplayService(cast_path, checkpoint_store=store)

    assert service.seek(6.0) == continuous.snapshot()
    assert service.checkpoints[1].event_index == 1
    assert service.checkpoints[1].snapshot.state is not None
    assert ReplayService(cast_path, checkpoint_store=store).seek(6.0) == continuous.snapshot()


def test_checkpoint_is_delayed_until_split_csi_parser_reaches_a_safe_boundary(
    tmp_path: Path,
) -> None:
    cast_path = tmp_path / "split-csi.cast"
    write_cast(
        cast_path,
        [(5.0, "o", "\x1b[31"), (1.0, "o", "mR"), (1.0, "o", "X")],
        rows=2,
    )
    continuous = TerminalEmulator(columns=8, rows=2)
    continuous.apply(output_event(1, 5.0, "\x1b[31"))
    assert not continuous.checkpoint_safe
    continuous.apply(output_event(2, 6.0, "mR"))
    assert continuous.checkpoint_safe
    continuous.apply(output_event(3, 7.0, "X"))

    service = ReplayService(cast_path, checkpoint_store=CheckpointStore(tmp_path / "split.json"))

    assert [checkpoint.event_index for checkpoint in service.checkpoints] == [0, 2]
    assert service.seek(7.0) == continuous.snapshot()


def test_seek_uses_target_timeline_and_keeps_trailing_unknown_duration(tmp_path: Path) -> None:
    cast_path = tmp_path / "timeline.cast"
    write_cast(
        cast_path,
        [
            (1.0, "o", "A"),
            (1.0, "i", "input"),
            (1.0, "m", "mark"),
            (1.0, "x", "0"),
            (3.0, "future", "ignored"),
        ],
        rows=2,
    )
    reader_result = AsciicastV3Reader(cast_path).read()
    service = ReplayService(cast_path, checkpoint_store=CheckpointStore(tmp_path / "timeline.json"))

    assert reader_result.trailing_interval == 3.0
    assert service.duration == 7.0
    assert service.seek(-1).relative_time == 0.0
    assert service.seek(1.5).relative_time == 1.5
    assert service.seek(2.0).relative_time == 2.0
    assert service.seek(3.0).relative_time == 3.0
    assert service.seek(4.0).relative_time == 4.0
    assert service.seek(99).relative_time == 7.0
    assert row_text(service.seek(99), 0).startswith("A")


def test_reader_exposes_resume_offsets_and_time_checkpoint_is_persisted(tmp_path: Path) -> None:
    cast_path = tmp_path / "session.cast"
    cast_path.write_bytes((FIXTURES / "mixed.cast").read_bytes())
    checkpoints_path = tmp_path / "checkpoints.json"

    read_result = AsciicastV3Reader(cast_path).read()
    ReplayService(cast_path, checkpoint_store=CheckpointStore(checkpoints_path)).seek(6.0)
    payload = json.loads(checkpoints_path.read_text(encoding="utf-8"))

    assert read_result.data_offset == len(cast_path.read_bytes().splitlines(keepends=True)[0])
    assert all(event.cast_offset > read_result.data_offset for event in read_result.events)
    assert payload["version"] == CHECKPOINT_VERSION
    checkpoint = next(item for item in payload["checkpoints"] if item["relativeTime"] == 5.0)
    assert checkpoint["eventIndex"] == 4
    assert cast_path.read_bytes()[checkpoint["castOffset"] :].startswith(b'[1.0,"x"')


def test_event_count_checkpoint_bounds_tail_replay(tmp_path: Path) -> None:
    cast_path = tmp_path / "long.cast"
    lines = ['{"version":3,"term":{"cols":8,"rows":2}}']
    lines.extend('[0.001,"o","."]' for _ in range(501))
    cast_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    checkpoints_path = tmp_path / "checkpoints.json"

    ReplayService(cast_path, checkpoint_store=CheckpointStore(checkpoints_path)).seek(1.0)
    payload = json.loads(checkpoints_path.read_text(encoding="utf-8"))

    checkpoint = next(item for item in payload["checkpoints"] if item["eventIndex"] == 500)
    assert checkpoint["relativeTime"] == pytest.approx(0.5)
    assert cast_path.read_bytes()[checkpoint["castOffset"] :].startswith(b'[0.001,"o","."]')


def test_late_seek_restores_nearest_checkpoint_instead_of_replaying_from_zero(
    tmp_path: Path,
) -> None:
    cast_path = tmp_path / "session.cast"
    cast_path.write_bytes((FIXTURES / "mixed.cast").read_bytes())
    store = CheckpointStore(tmp_path / "checkpoints.json")
    ReplayService(cast_path, checkpoint_store=store).seek(6.0)
    created: list[CountingEmulator] = []

    class CountingEmulator(TerminalEmulator):
        def __init__(self, *, columns: int, rows: int) -> None:
            super().__init__(columns=columns, rows=rows)
            self.applied: list[TerminalEvent] = []
            self.restored = 0
            created.append(self)

        def apply(self, event: TerminalEvent) -> None:
            self.applied.append(event)
            super().apply(event)

        def restore(self, snapshot: TerminalSnapshot) -> None:
            self.restored += 1
            super().restore(snapshot)

    snapshot = ReplayService(
        cast_path,
        checkpoint_store=store,
        emulator_factory=CountingEmulator,
    ).seek(6.0)

    assert row_text(snapshot, 1).startswith("中文")
    assert len(created) == 1
    assert created[0].restored == 1
    assert len(created[0].applied) == 1
    assert created[0].applied[0].type is TerminalEventType.EXIT


def test_invalid_or_stale_checkpoint_index_is_rebuilt_without_touching_cast(
    tmp_path: Path,
) -> None:
    cast_path = tmp_path / "session.cast"
    cast_path.write_bytes((FIXTURES / "mixed.cast").read_bytes())
    original_cast = cast_path.read_bytes()
    checkpoints_path = tmp_path / "checkpoints.json"
    checkpoints_path.write_text('{"version":999,"checkpoints":[]}', encoding="utf-8")

    first_service = ReplayService(cast_path, checkpoint_store=CheckpointStore(checkpoints_path))
    first_service.seek(6.0)
    rebuilt = json.loads(checkpoints_path.read_text(encoding="utf-8"))
    assert rebuilt["version"] == CHECKPOINT_VERSION

    rebuilt["checkpoints"][-1]["snapshot"]["cells"][1][0]["character"] = "坏"
    checkpoints_path.write_text(json.dumps(rebuilt, ensure_ascii=False), encoding="utf-8")
    repaired = ReplayService(cast_path, checkpoint_store=CheckpointStore(checkpoints_path))
    assert row_text(repaired.seek(6.0), 1).startswith("中文")

    cast_path.write_text(
        cast_path.read_text(encoding="utf-8") + '[1.0,"o","!"]\n', encoding="utf-8"
    )
    changed_cast = cast_path.read_bytes()
    second_service = ReplayService(cast_path, checkpoint_store=CheckpointStore(checkpoints_path))

    assert row_text(second_service.seek(7.0), 1).startswith("中文!")
    assert original_cast != changed_cast
    assert cast_path.read_bytes() == changed_cast
    refreshed = json.loads(checkpoints_path.read_text(encoding="utf-8"))
    assert refreshed["cast"]["size"] == len(changed_cast)


def test_checkpoint_store_uses_same_directory_atomic_replace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cast_path = tmp_path / "session.cast"
    cast_path.write_bytes((FIXTURES / "mixed.cast").read_bytes())
    checkpoint_path = tmp_path / "state" / "checkpoints.json"
    replaced: list[tuple[Path, Path]] = []
    real_replace = __import__("os").replace

    def observe_replace(source: str | Path, destination: str | Path) -> None:
        replaced.append((Path(source), Path(destination)))
        real_replace(source, destination)

    monkeypatch.setattr("csbox.lab.replay.os.replace", observe_replace)

    ReplayService(cast_path, checkpoint_store=CheckpointStore(checkpoint_path)).seek(6.0)

    assert replaced
    assert replaced[-1][0].parent == checkpoint_path.parent
    assert replaced[-1][1] == checkpoint_path
    assert not list(checkpoint_path.parent.glob("*.tmp"))


def test_terminal_emulator_restores_a_domain_snapshot_and_continues_streaming() -> None:
    source = TerminalEmulator(columns=6, rows=2)
    source.apply(
        TerminalEvent(
            sequence=1,
            monotonic_time=10.0,
            relative_time=2.0,
            type=TerminalEventType.OUTPUT,
            payload=(b"\x1b[31;4;7mA" + "中".encode() + b"\x1b[0m\x1b[?25l"),
        )
    )
    expected = source.snapshot()
    restored = TerminalEmulator(columns=1, rows=1)

    restored.restore(expected)

    assert restored.snapshot() == expected
    restored.apply(
        TerminalEvent(
            sequence=2,
            monotonic_time=11.0,
            relative_time=3.0,
            type=TerminalEventType.RESIZE,
            payload=TerminalSize(columns=8, rows=3),
        )
    )
    assert (restored.rows, restored.columns, restored.snapshot().relative_time) == (3, 8, 3.0)


def test_default_checkpoint_path_cannot_replace_a_cast_named_checkpoints_json(
    tmp_path: Path,
) -> None:
    cast_path = tmp_path / "checkpoints.json"
    write_cast(cast_path, [(1.0, "o", "safe")], columns=8, rows=2)
    original = cast_path.read_bytes()

    service = ReplayService(cast_path)

    assert cast_path.read_bytes() == original
    assert service.seek(1.0).cells[0][0].character == "s"
    assert cast_path.with_name("checkpoints.checkpoints.json").is_file()


def test_custom_checkpoint_path_cannot_replace_the_original_cast(tmp_path: Path) -> None:
    cast_path = tmp_path / "session.cast"
    write_cast(cast_path, [(1.0, "o", "safe")], columns=8, rows=2)
    original = cast_path.read_bytes()

    with pytest.raises(CheckpointStoreError, match="checkpoint.*cast|cast.*checkpoint"):
        ReplayService(cast_path, checkpoint_store=CheckpointStore(cast_path))

    assert cast_path.read_bytes() == original
