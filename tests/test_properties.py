from __future__ import annotations

import json
import random
from dataclasses import replace
from pathlib import Path

import pytest

from csbox.api.errors import ApiConfigError
from csbox.api.scenario import ScenarioLoader
from csbox.core.display_width import display_width, truncate_cells
from csbox.core.events import TerminalEvent, TerminalEventType, TerminalSize
from csbox.core.safe_paths import safe_relative_path
from csbox.lab.captures import CaptureStore
from csbox.lab.recorder import AsciicastV3Reader
from csbox.lab.replay import CheckpointStore, ReplayService
from csbox.lab.screen import TerminalEmulator


def event(sequence: int, time: float, payload: bytes) -> TerminalEvent:
    return TerminalEvent(
        sequence=sequence,
        monotonic_time=time,
        relative_time=time,
        type=TerminalEventType.OUTPUT,
        payload=payload,
    )


def write_cast(
    path: Path, events: list[tuple[float, str, str]], *, columns: int, rows: int
) -> None:
    lines = [json.dumps({"version": 3, "term": {"cols": columns, "rows": rows}})]
    lines.extend(json.dumps(list(event), ensure_ascii=False) for event in events)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def generated_chunks(seed: int, count: int = 36) -> list[str]:
    rng = random.Random(seed)
    atoms = ("A", "b", "中", "文", "界", "e\u0301", "🙂", "\x1b[31m", "\x1b[0m", "\x1b[2J")
    return ["".join(rng.choice(atoms) for _ in range(rng.randint(1, 5))) for _ in range(count)]


@pytest.mark.parametrize("seed", range(8))
def test_generated_output_replay_is_deterministic_and_cell_valid(seed: int) -> None:
    chunks = generated_chunks(seed)
    continuous = TerminalEmulator(columns=24, rows=5)
    split = TerminalEmulator(columns=24, rows=5)
    current_time = 0.0
    for sequence, chunk in enumerate(chunks, start=1):
        current_time += 0.25
        continuous.apply(event(sequence, current_time, chunk.encode("utf-8")))
        for byte in chunk.encode("utf-8"):
            split.apply(event(sequence, current_time, bytes((byte,))))

    assert split.snapshot() == continuous.snapshot()
    snapshot = split.snapshot()
    for row in snapshot.cells:
        for column, cell in enumerate(row):
            assert cell.width in {0, 1, 2}
            if cell.width == 2:
                assert column + 1 < snapshot.columns
                assert row[column + 1].width == 0
                assert row[column + 1].character == ""


@pytest.mark.parametrize("seed", range(6))
def test_generated_display_width_truncation_never_exceeds_cell_budget(seed: int) -> None:
    rng = random.Random(seed)
    atoms = ("a", "中", "文", "e\u0301", "🙂", " ")
    for width in range(0, 20):
        text = "".join(rng.choice(atoms) for _ in range(24))
        truncated = truncate_cells(text, width, ellipsis="…")
        assert display_width(truncated) <= width


@pytest.mark.parametrize("seed", range(6))
def test_generated_chinese_relative_paths_round_trip_and_reject_parent_variants(seed: int) -> None:
    rng = random.Random(seed)
    parts = [rng.choice(("src", "课程", "实验", "case-1")) for _ in range(1 + seed % 3)]
    value = "/".join(parts)

    assert safe_relative_path(value).as_posix() == value
    with pytest.raises(ValueError):
        safe_relative_path(f"../{value}")


@pytest.mark.parametrize("seed", range(4))
def test_finite_generated_invalid_scenarios_map_to_domain_errors(seed: int, tmp_path: Path) -> None:
    invalid_documents = (
        'name = "场景"\nsteps = "not-a-list"\n',
        'name = "场景"\n[[steps]]\nname = "步骤"\nmethod = "TRACE"\nurl = "https://example.test"\n',
        'name = "场景"\n[[steps]]\nname = "步骤"\nmethod = "GET"\nurl = "{{__import__(\'os\')}}"\n',
        'name = "场景"\n[[steps]]\nname = "步骤"\nmethod = 7\nurl = "https://example.test"\n',
    )
    path = tmp_path / f"invalid-{seed}.toml"
    path.write_text(invalid_documents[seed], encoding="utf-8")

    with pytest.raises(ApiConfigError):
        ScenarioLoader().load(path)


def test_replay_checkpoint_equivalence_and_seek_round_trip_for_generated_timeline(
    tmp_path: Path,
) -> None:
    events = [
        (1.0, "o", "A"),
        (1.0, "o", "中"),
        (1.0, "r", "12x3"),
        (1.0, "o", "\x1b[2;1H中文"),
        (1.0, "r", "8x2"),
        (1.0, "o", "\x1b[1;1Hdone"),
    ]
    cast_path = tmp_path / "generated.cast"
    checkpoint_path = tmp_path / "generated.checkpoints.json"
    write_cast(cast_path, events, columns=8, rows=2)
    service = ReplayService(cast_path, checkpoint_store=CheckpointStore(checkpoint_path))

    targets = (0.0, 1.0, 2.0, 3.0, 4.0, 5.0, 6.0)
    first_pass = {target: service.seek(target) for target in targets}
    for target in reversed(targets):
        assert service.seek(target) == first_pass[target]

    for target in targets:
        full = TerminalEmulator(columns=8, rows=2)
        current = 0.0
        for sequence, (interval, code, data) in enumerate(events, start=1):
            current += interval
            if current > target:
                break
            if code == "o":
                full.apply(event(sequence, current, data.encode()))
            elif code == "r":
                columns, rows = (int(value) for value in data.split("x"))
                full.apply(
                    TerminalEvent(
                        sequence=sequence,
                        monotonic_time=current,
                        relative_time=current,
                        type=TerminalEventType.RESIZE,
                        payload=TerminalSize(columns, rows),
                    )
                )
        expected = replace(full.snapshot(), relative_time=target)
        assert service.seek(target) == expected


def test_reader_parse_does_not_require_whole_cast_read_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "large.cast"
    path.write_text(
        '{"version":3,"term":{"cols":8,"rows":2}}\n' + '[0.001,"o","x"]\n' * 2_000,
        encoding="utf-8",
    )
    original = Path.read_bytes

    def fail_read_bytes(self: Path) -> bytes:
        raise AssertionError("reader must parse incrementally")

    monkeypatch.setattr(Path, "read_bytes", fail_read_bytes)
    try:
        result = AsciicastV3Reader(path).read()
    finally:
        monkeypatch.setattr(Path, "read_bytes", original)

    assert len(result.events) == 2_000


def test_reader_ignores_one_oversized_line_and_continues_to_later_events(tmp_path: Path) -> None:
    path = tmp_path / "oversized.cast"
    oversized = "x" * (1024 * 1024 + 1)
    path.write_text(
        '{"version":3,"term":{"cols":8,"rows":2}}\n'
        f'[0.1,"o","{oversized}"]\n'
        '[0.1,"o","after"]\n',
        encoding="utf-8",
    )

    result = AsciicastV3Reader(path).read()

    assert [(item.code, item.data) for item in result.events] == [("o", "after")]
    assert any("oversized" in warning for warning in result.warnings)


def test_capture_after_backward_seek_keeps_cjk_state_and_dimensions(tmp_path: Path) -> None:
    cast_path = tmp_path / "capture-after-seek.cast"
    write_cast(
        cast_path,
        [
            (1.0, "o", "A中"),
            (1.0, "r", "12x3"),
            (1.0, "o", "\x1b[2;1H中文"),
        ],
        columns=8,
        rows=2,
    )
    service = ReplayService(
        cast_path, checkpoint_store=CheckpointStore(tmp_path / "checkpoints.json")
    )
    expected = service.seek(3.0)
    service.seek(1.0)
    store = CaptureStore(tmp_path / "captures.json")

    capture = store.create_capture(
        service.seek(3.0), timestamp=3.0, cwd=tmp_path / "中文项目", title="回退后中文"
    )

    assert capture.snapshot == expected
    assert (capture.rows, capture.columns) == (3, 12)
    assert any(
        cell.character == "中" and cell.width == 2 for row in capture.snapshot.cells for cell in row
    )
