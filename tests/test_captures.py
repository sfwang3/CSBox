from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from csbox.core.events import TerminalEvent, TerminalEventType
from csbox.lab.captures import CaptureStore, CaptureStoreError
from csbox.lab.dispatcher import DispatchError, TerminalEventDispatcher
from csbox.lab.recorder import RecorderError
from csbox.lab.screen import TerminalCell, TerminalCursor, TerminalSnapshot


def snapshot(text: str, relative_time: float) -> TerminalSnapshot:
    cells = tuple(TerminalCell(character=character) for character in text.ljust(8))
    return TerminalSnapshot(
        rows=1,
        columns=8,
        cells=(cells,),
        cursor=TerminalCursor(row=0, column=min(len(text), 7)),
        relative_time=relative_time,
    )


def make_store(path: Path) -> CaptureStore:
    ids = iter(("capture-2", "capture-1", "capture-3", "capture-4"))
    times = iter(
        (
            datetime(2026, 8, 10, 6, 0, 2, tzinfo=UTC),
            datetime(2026, 8, 10, 6, 0, 1, tzinfo=UTC),
            datetime(2026, 8, 10, 6, 0, 3, tzinfo=UTC),
            datetime(2026, 8, 10, 6, 0, 4, tzinfo=UTC),
        )
    )
    return CaptureStore(path, id_factory=lambda: next(ids), clock=lambda: next(times))


def test_capture_store_persists_immutable_domain_snapshot_and_sorted_metadata(
    tmp_path: Path,
) -> None:
    captures_path = tmp_path / "captures.json"
    store = make_store(captures_path)
    late_snapshot = snapshot("late", 2.0)
    early_snapshot = snapshot("early", 1.0)

    late = store.create_capture(
        late_snapshot,
        timestamp=2.0,
        cwd=Path("/tmp/中文项目"),
        command="pytest -q",
        title="测试通过",
    )
    early = store.create_capture(early_snapshot, timestamp=1.0, cwd=Path("/tmp/中文项目"))

    result = store.load()
    payload = json.loads(captures_path.read_text(encoding="utf-8"))
    assert [item.capture_id for item in result.captures] == [early.capture_id, late.capture_id]
    assert late.snapshot is late_snapshot
    assert (late.timestamp, late.rows, late.columns) == (2.0, 1, 8)
    assert late.cwd == Path("/tmp/中文项目")
    assert late.command == "pytest -q"
    assert early.command is None
    assert payload["version"] == 1
    assert [item["timestamp"] for item in payload["captures"]] == [1.0, 2.0]
    assert "pyte.screens" not in captures_path.read_text(encoding="utf-8")


def test_capture_store_atomic_replace_failure_keeps_previous_primary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captures_path = tmp_path / "captures.json"
    store = make_store(captures_path)
    store.create_capture(snapshot("first", 1.0), timestamp=1.0, cwd=tmp_path)
    previous = captures_path.read_bytes()
    real_replace = __import__("os").replace
    temp_parents: list[Path] = []

    def fail_primary_replace(source: str | Path, destination: str | Path) -> None:
        temp_parents.append(Path(source).parent)
        if Path(destination) == captures_path:
            raise OSError("disk failure")
        real_replace(source, destination)

    monkeypatch.setattr("csbox.lab.captures.os.replace", fail_primary_replace)

    with pytest.raises(CaptureStoreError, match="persist"):
        store.create_capture(snapshot("second", 2.0), timestamp=2.0, cwd=tmp_path)

    assert captures_path.read_bytes() == previous
    assert temp_parents and all(parent == captures_path.parent for parent in temp_parents)


def test_capture_store_recovers_backup_and_isolates_one_invalid_capture(tmp_path: Path) -> None:
    captures_path = tmp_path / "captures.json"
    store = make_store(captures_path)
    first = store.create_capture(snapshot("first", 1.0), timestamp=1.0, cwd=tmp_path)
    store.create_capture(snapshot("second", 2.0), timestamp=2.0, cwd=tmp_path)
    captures_path.write_text("{truncated", encoding="utf-8")

    recovered = store.load()

    assert [capture.capture_id for capture in recovered.captures] == [first.capture_id]
    assert any("backup" in warning for warning in recovered.warnings)

    valid = first.model_dump(mode="json", by_alias=True)
    captures_path.write_text(
        json.dumps({"version": 1, "captures": [valid, {"id": "broken"}, valid]}),
        encoding="utf-8",
    )
    isolated = store.load()
    assert len(isolated.captures) == 2
    assert any("capture 2" in warning for warning in isolated.warnings)


def test_capture_store_isolates_snapshot_dimension_mismatch(tmp_path: Path) -> None:
    captures_path = tmp_path / "captures.json"
    store = make_store(captures_path)
    capture = store.create_capture(snapshot("valid", 1.0), timestamp=1.0, cwd=tmp_path)
    invalid = capture.model_dump(mode="json", by_alias=True)
    invalid["rows"] = 2
    captures_path.write_text(json.dumps({"version": 1, "captures": [invalid]}), encoding="utf-8")

    result = store.load()

    assert result.captures == ()
    assert any("invalid capture 1" in warning for warning in result.warnings)


def test_capture_store_edits_title_and_deletes_by_id(tmp_path: Path) -> None:
    store = make_store(tmp_path / "captures.json")
    capture = store.create_capture(snapshot("one", 1.0), timestamp=1.0, cwd=tmp_path)

    edited = store.edit_title(capture.capture_id, "新的标题")
    assert edited.title == "新的标题"
    assert store.load().captures[0].title == "新的标题"
    assert store.delete(capture.capture_id)
    assert store.load().captures == ()
    assert not store.delete("missing")


def test_dispatcher_preserves_sink_order_and_propagates_recorder_failures() -> None:
    calls: list[str] = []

    class Sink:
        def __init__(self, name: str) -> None:
            self.name = name

        def handle(self, event: TerminalEvent) -> None:
            calls.append(self.name)

    class FailedRecorder:
        def handle(self, event: TerminalEvent) -> None:
            calls.append("recorder")
            raise RecorderError("queue full")

    dispatcher = TerminalEventDispatcher([Sink("screen"), FailedRecorder(), Sink("stdout")])
    output = TerminalEvent(1, 10.0, 0.1, TerminalEventType.OUTPUT, b"hello")

    with pytest.raises(DispatchError, match="sink 2") as error:
        dispatcher.dispatch(output)

    assert isinstance(error.value.__cause__, RecorderError)
    assert calls == ["screen", "recorder"]
