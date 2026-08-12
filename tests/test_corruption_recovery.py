from __future__ import annotations

import json
from pathlib import Path

import pytest

import csbox.lab.recorder as recorder_module
import csbox.lab.repository as repository_module
from csbox.core.events import TerminalSize
from csbox.lab.captures import CaptureStore
from csbox.lab.exporter import LabExporter, LabExportError
from csbox.lab.models import SessionPaths
from csbox.lab.recorder import AsciicastV3Reader, RecorderError
from csbox.lab.replay import CheckpointStore, ReplayService
from csbox.lab.repository import SessionRepository


def write_cast(
    path: Path, events: list[tuple[object, ...]], *, columns: int = 8, rows: int = 2
) -> None:
    lines = [json.dumps({"version": 3, "term": {"cols": columns, "rows": rows}})]
    lines.extend(json.dumps(list(event), ensure_ascii=False) for event in events)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_reader_rejects_invalid_header_and_missing_header_without_traceback_leak(
    tmp_path: Path,
) -> None:
    for name, content in (
        ("missing.cast", "# partial\n"),
        ("bad-header.cast", '{"version":3,"term":{"cols":0,"rows":2}}\n'),
    ):
        path = tmp_path / name
        path.write_text(content, encoding="utf-8")
        with pytest.raises(RecorderError, match="header"):
            AsciicastV3Reader(path).read()


def test_reader_maps_json_recursion_at_header_and_event_boundaries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cast_path = tmp_path / "recursive.cast"
    write_cast(cast_path, [(1.0, "o", "safe")])
    real_loads = recorder_module.json.loads
    monkeypatch.setattr(
        recorder_module.json,
        "loads",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RecursionError("too deep")),
    )

    with pytest.raises(RecorderError, match="invalid asciicast v3 header"):
        AsciicastV3Reader(cast_path).read_header()

    calls = 0

    def recurse_on_event(value: bytes) -> object:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RecursionError("too deep")
        return real_loads(value)

    monkeypatch.setattr(recorder_module.json, "loads", recurse_on_event)

    result = AsciicastV3Reader(cast_path).read()

    assert result.events == ()
    assert result.warnings == ("corrupt cast line 2 ignored",)


def test_reader_opens_cast_through_the_shared_regular_file_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from contextlib import contextmanager

    from csbox.core.safe_paths import open_regular_binary as real_open_regular_binary

    cast_path = tmp_path / "session.cast"
    cast_path.write_text(
        '{"version":3,"term":{"cols":80,"rows":24}}\n',
        encoding="utf-8",
    )
    opened: list[Path] = []

    @contextmanager
    def observed_open(path: Path):
        opened.append(Path(path))
        with real_open_regular_binary(path) as stream:
            yield stream

    monkeypatch.setattr(
        recorder_module,
        "open_regular_binary",
        observed_open,
        raising=False,
    )

    AsciicastV3Reader(cast_path).read_header()

    assert opened == [cast_path]


@pytest.mark.parametrize(
    ("columns", "rows", "valid"),
    [(2000, 400, True), (2001, 500, False)],
)
def test_reader_applies_a_bounded_screen_cell_budget(
    tmp_path: Path, columns: int, rows: int, valid: bool
) -> None:
    path = tmp_path / f"{columns}x{rows}.cast"
    path.write_text(
        json.dumps({"version": 3, "term": {"cols": columns, "rows": rows}}) + "\n",
        encoding="utf-8",
    )

    if valid:
        assert AsciicastV3Reader(path).read_header()["term"] == {
            "cols": columns,
            "rows": rows,
        }
    else:
        with pytest.raises(RecorderError, match="header"):
            AsciicastV3Reader(path).read_header()


@pytest.mark.parametrize(
    "event",
    [
        [0.1, "o", 123],
        [0.1, "o"],
        [-0.1, "o", "negative"],
        ["0.1", "o", "wrong timestamp"],
        [0.1, "r", "0x2"],
        [0.1, "r", "999999999x2"],
        [0.1, "r", "8x0"],
    ],
)
def test_reader_keeps_valid_prefix_and_warns_for_invalid_events(
    tmp_path: Path, event: list[object]
) -> None:
    path = tmp_path / "corrupt.cast"
    write_cast(path, [[0.1, "o", "before"], event, [0.1, "o", "after"]])

    result = AsciicastV3Reader(path).read()

    assert [(item.code, item.data) for item in result.events] == [
        ("o", "before"),
        ("o", "after"),
    ]
    assert result.warnings


def test_reader_rejects_overflowing_integer_timestamp_without_traceback(
    tmp_path: Path,
) -> None:
    path = tmp_path / "huge-timestamp.cast"
    write_cast(path, [[10**1000, "o", "bad"], [0.1, "o", "after"]])

    result = AsciicastV3Reader(path).read()

    assert [item.data for item in result.events] == ["after"]
    assert result.warnings


def test_reader_preserves_cr_only_cast_line_boundaries(tmp_path: Path) -> None:
    path = tmp_path / "cr.cast"
    path.write_bytes(
        b'{"version":3,"term":{"cols":8,"rows":2}}\r[0.1,"o","before"]\r[0.1,"o","after"]\r'
    )

    result = AsciicastV3Reader(path).read()

    assert [item.data for item in result.events] == ["before", "after"]


def test_replay_rejects_malformed_resize_instead_of_constructing_invalid_terminal_size(
    tmp_path: Path,
) -> None:
    path = tmp_path / "bad-resize.cast"
    write_cast(path, [[0.1, "o", "ok"], [0.1, "r", "0x0"], [0.1, "o", "still ok"]])

    service = ReplayService(path, checkpoint_store=CheckpointStore(tmp_path / "checkpoints.json"))

    snapshot = service.seek(1.0)
    assert snapshot.columns == 8
    assert "still ok" in "".join(
        cell.character for row in snapshot.cells for cell in row if cell.width != 0
    )
    assert any(
        "resize" in warning.lower() or "invalid" in warning.lower() for warning in service.warnings
    )


def test_corrupt_checkpoint_is_rebuilt_without_modifying_raw_cast(tmp_path: Path) -> None:
    cast_path = tmp_path / "session.cast"
    checkpoint_path = tmp_path / "checkpoints.json"
    write_cast(cast_path, [[5.0, "o", "中"], [1.0, "o", "done"]], columns=8)
    original = cast_path.read_bytes()

    first = ReplayService(cast_path, checkpoint_store=CheckpointStore(checkpoint_path))
    checkpoint_path.write_text("{truncated", encoding="utf-8")

    rebuilt = ReplayService(cast_path, checkpoint_store=CheckpointStore(checkpoint_path))

    assert rebuilt.seek(6.0) == first.seek(6.0)
    assert cast_path.read_bytes() == original
    assert json.loads(checkpoint_path.read_text(encoding="utf-8"))["version"] >= 1


def test_corrupt_capture_primary_recovers_backup_and_corrupt_metadata_is_warning_only(
    tmp_path: Path,
) -> None:
    paths = SessionPaths(tmp_path / "session")
    paths.root.mkdir()
    write_cast(paths.cast, [])
    store = CaptureStore(paths.captures)
    store.create_capture(
        ReplayService(paths.cast).seek(0),
        cwd=tmp_path / "中文项目",
        timestamp=0.0,
    )
    paths.captures.write_text("{broken", encoding="utf-8")
    paths.metadata.write_text("{broken", encoding="utf-8")

    loaded = CaptureStore(paths.captures).load()

    assert len(loaded.captures) == 1
    assert any("backup" in warning for warning in loaded.warnings)
    assert paths.metadata.read_text(encoding="utf-8") == "{broken"


def test_repository_lists_healthy_session_when_sibling_session_is_partial(tmp_path: Path) -> None:
    repository = SessionRepository(tmp_path / "sessions")
    healthy = repository.create_running(
        "健康 session",
        platform="linux",
        shell="bash",
        shell_version=None,
        size=TerminalSize(8, 2),
        cwd=tmp_path / "中文项目",
        session_id="healthy",
    )
    write_cast(healthy.cast, [])
    repository.finish(healthy, "completed", exit_code=0)
    partial = repository.root / "partial"
    partial.mkdir()
    (partial / "metadata.json").write_text("{partial", encoding="utf-8")
    (partial / "extra.txt").write_text("not a session", encoding="utf-8")
    corrupt_cast = repository.create_running(
        "损坏录制",
        platform="linux",
        shell="bash",
        shell_version=None,
        size=TerminalSize(8, 2),
        cwd=tmp_path,
        session_id="corrupt-cast",
    )
    corrupt_cast.cast.write_text("{bad header", encoding="utf-8")
    repository.finish(corrupt_cast, "failed", exit_code=None)

    sessions = repository.list_sessions()

    assert [item.paths.root.name for item in sessions] == ["healthy"]


def test_repository_reads_metadata_through_a_bounded_regular_file_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = SessionRepository(tmp_path / "sessions")
    paths = repository.create_running(
        "bounded",
        shell="bash",
        shell_version="5",
        size=TerminalSize(columns=8, rows=2),
        cwd=tmp_path,
        session_id="bounded-session",
    )
    write_cast(paths.cast, [])
    observed: list[tuple[Path, int]] = []
    from csbox.core.safe_paths import read_regular_text as real_read_regular_text

    def observed_read(path: Path, *, max_bytes: int, encoding: str = "utf-8") -> str:
        observed.append((Path(path), max_bytes))
        return real_read_regular_text(path, max_bytes=max_bytes, encoding=encoding)

    monkeypatch.setattr(repository_module, "read_regular_text", observed_read, raising=False)

    assert repository.list_sessions()[0].metadata.experiment_name == "bounded"
    assert observed == [(paths.metadata, observed[0][1])]
    assert observed[0][1] > 0


def test_repository_does_not_list_metadata_only_session_as_available(tmp_path: Path) -> None:
    repository = SessionRepository(tmp_path / "sessions")
    missing_cast = repository.create_running(
        "缺少录制",
        platform="linux",
        shell="bash",
        shell_version=None,
        size=TerminalSize(8, 2),
        cwd=tmp_path,
        session_id="missing-cast",
    )
    repository.finish(missing_cast, "completed", exit_code=0)

    assert repository.list_sessions() == ()


def test_export_rejects_missing_cast_before_creating_partial_output(tmp_path: Path) -> None:
    paths = SessionPaths(tmp_path / "session")
    paths.root.mkdir()
    destination = tmp_path / "export"

    with pytest.raises(LabExportError, match="录制|cast"):
        LabExporter(object()).export(paths, destination)  # type: ignore[arg-type]

    assert not destination.exists()


def test_export_renderer_failure_does_not_publish_partial_new_export(tmp_path: Path) -> None:
    paths = SessionPaths(tmp_path / "session")
    paths.root.mkdir()
    write_cast(paths.cast, [])
    CaptureStore(paths.captures).create_capture(
        ReplayService(paths.cast).seek(0),
        cwd=tmp_path / "中文项目",
        timestamp=0.0,
    )

    class FailingRenderer:
        def render(self, *args: object, **kwargs: object) -> Path:
            del args, kwargs
            raise RuntimeError("renderer failed after staging started")

    destination = tmp_path / "export"
    with pytest.raises(RuntimeError, match="renderer failed"):
        LabExporter(FailingRenderer()).export(paths, destination)  # type: ignore[arg-type]

    assert not destination.exists()
    assert not list(tmp_path.glob(".csbox-lab-export-*.partial"))


def test_force_export_failure_preserves_existing_generated_files(tmp_path: Path) -> None:
    paths = SessionPaths(tmp_path / "session")
    paths.root.mkdir()
    write_cast(paths.cast, [])
    snapshot = ReplayService(paths.cast).seek(0)
    store = CaptureStore(paths.captures)
    store.create_capture(snapshot, cwd=tmp_path, timestamp=0.0, title="第一张")
    store.create_capture(snapshot, cwd=tmp_path, timestamp=1.0, title="第二张")

    class Writer:
        def render(self, snapshot: object, destination: Path, theme: object) -> Path:
            del snapshot, theme
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(b"old-" + destination.name.encode())
            return destination

    destination = tmp_path / "export"
    LabExporter(Writer()).export(paths, destination)  # type: ignore[arg-type]
    before = {
        path.relative_to(destination): path.read_bytes()
        for path in destination.rglob("*")
        if path.is_file()
    }

    class FailingWriter(Writer):
        calls = 0

        def render(self, snapshot: object, destination: Path, theme: object) -> Path:
            self.calls += 1
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(b"new-partial")
            if self.calls == 2:
                raise RuntimeError("second image failed")
            return destination

    with pytest.raises(RuntimeError, match="second image failed"):
        LabExporter(FailingWriter()).export(paths, destination, force=True)  # type: ignore[arg-type]

    after = {
        path.relative_to(destination): path.read_bytes()
        for path in destination.rglob("*")
        if path.is_file()
    }
    assert after == before
    assert not list(tmp_path.glob(".csbox-lab-export-*.partial"))


def test_export_rejects_broken_output_symlink_before_staging(tmp_path: Path) -> None:
    paths = SessionPaths(tmp_path / "session")
    paths.root.mkdir()
    write_cast(paths.cast, [])
    CaptureStore(paths.captures).create_capture(
        ReplayService(paths.cast).seek(0), cwd=tmp_path, timestamp=0.0
    )
    destination = tmp_path / "broken-export"
    try:
        destination.symlink_to(tmp_path / "missing-target", target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlinks are unavailable: {exc}")

    class UnexpectedRenderer:
        def render(self, *args: object, **kwargs: object) -> Path:
            del args, kwargs
            raise AssertionError("renderer must not run")

    with pytest.raises(LabExportError, match="符号链接"):
        LabExporter(UnexpectedRenderer()).export(paths, destination)  # type: ignore[arg-type]

    assert destination.is_symlink()
