from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from csbox.core.events import TerminalSize
from csbox.lab.captures import CaptureStore
from csbox.lab.recorder import AsciicastV3Reader
from csbox.lab.replay import CheckpointStore, ReplayService
from csbox.lab.repository import SessionRepository
from csbox.lab.screen import TerminalCell, TerminalCursor, TerminalSnapshot


def _snapshot(relative_time: float = 0.0) -> TerminalSnapshot:
    return TerminalSnapshot(
        rows=1,
        columns=4,
        cells=(
            (
                TerminalCell("中", width=2),
                TerminalCell("", width=0),
                TerminalCell(" "),
                TerminalCell(" "),
            ),
        ),
        cursor=TerminalCursor(),
        relative_time=relative_time,
    )


def _create_session(
    repository: SessionRepository,
    *,
    session_id: str,
    started_at: datetime,
    status: str = "completed",
    shell: str = "bash",
    capture_count: int = 0,
) -> Path:
    paths = repository.create_starting(
        f"实验 {session_id}",
        shell=shell,
        shell_version=None,
        size=TerminalSize(columns=80, rows=24),
        cwd=repository.root.parent,
        session_id=session_id,
        started_at=started_at,
    )
    paths.cast.write_text('{"version":3,"term":{"cols":80,"rows":24}}\n', encoding="utf-8")
    for index in range(capture_count):
        CaptureStore(paths.captures).create_capture(
            _snapshot(float(index)),
            timestamp=float(index),
            cwd=repository.root.parent,
            title=f"Capture {index + 1}",
        )
    if status != "running":
        repository.finish(
            paths,
            status,
            ended_at=started_at.replace(minute=started_at.minute + 1),
        )
    return paths.root


def test_list_summaries_is_lightweight_and_never_opens_session_cast(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = SessionRepository(tmp_path / "sessions")
    _create_session(
        repository,
        session_id="lightweight",
        started_at=datetime(2026, 8, 19, 8, 0, tzinfo=UTC),
        capture_count=2,
    )

    def forbidden_boundary(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("Records crossed a replay or snapshot boundary")

    monkeypatch.setattr(AsciicastV3Reader, "read", forbidden_boundary)
    monkeypatch.setattr(AsciicastV3Reader, "read_header", forbidden_boundary)
    monkeypatch.setattr(ReplayService, "__init__", forbidden_boundary)
    monkeypatch.setattr(CheckpointStore, "load", forbidden_boundary)
    monkeypatch.setattr(CheckpointStore, "save", forbidden_boundary)
    monkeypatch.setattr(CaptureStore, "load", forbidden_boundary)

    summaries = repository.list_summaries()

    assert len(summaries) == 1
    assert summaries[0].metadata.session_id == "lightweight"
    assert summaries[0].capture_count == 2


def test_list_summaries_is_newest_first_with_a_deterministic_session_id_tiebreak(
    tmp_path: Path,
) -> None:
    repository = SessionRepository(tmp_path / "sessions")
    tied = datetime(2026, 8, 19, 8, 0, tzinfo=UTC)
    _create_session(repository, session_id="zeta", started_at=tied)
    _create_session(repository, session_id="alpha", started_at=tied)
    _create_session(
        repository,
        session_id="newest",
        started_at=datetime(2026, 8, 19, 9, 0, tzinfo=UTC),
    )

    summaries = repository.list_summaries()

    assert [summary.metadata.session_id for summary in summaries] == [
        "newest",
        "alpha",
        "zeta",
    ]


def test_read_summary_refreshes_only_the_requested_session(tmp_path: Path) -> None:
    repository = SessionRepository(tmp_path / "sessions")
    first_root = _create_session(
        repository,
        session_id="first",
        started_at=datetime(2026, 8, 19, 8, 0, tzinfo=UTC),
    )
    _create_session(
        repository,
        session_id="second",
        started_at=datetime(2026, 8, 19, 9, 0, tzinfo=UTC),
    )
    CaptureStore(first_root / "captures.json").create_capture(
        _snapshot(), timestamp=0.0, cwd=tmp_path, title="补充 Capture"
    )

    summary = repository.read_summary("first")

    assert summary is not None
    assert summary.metadata.session_id == "first"
    assert summary.capture_count == 1
    assert repository.read_summary("missing") is None
    assert repository.read_summary("../first") is None


def test_read_summary_preserves_the_exact_safe_session_id(tmp_path: Path) -> None:
    repository = SessionRepository(tmp_path / "sessions")
    _create_session(
        repository,
        session_id=" spaced",
        started_at=datetime(2026, 8, 19, 8, 0, tzinfo=UTC),
    )

    summary = repository.read_summary(" spaced")

    assert summary is not None
    assert summary.metadata.session_id == " spaced"
    assert repository.read_summary("spaced") is None


def test_list_summaries_skips_one_corrupt_metadata_record_without_leaking_it(
    tmp_path: Path,
) -> None:
    repository = SessionRepository(tmp_path / "sessions")
    _create_session(
        repository,
        session_id="valid-a",
        started_at=datetime(2026, 8, 19, 8, 0, tzinfo=UTC),
    )
    corrupt = repository.root / "corrupt-secret"
    corrupt.mkdir(parents=True)
    (corrupt / "metadata.json").write_text(
        '{"id":"token=CSBOX_SECRET_SENTINEL_records"', encoding="utf-8"
    )
    _create_session(
        repository,
        session_id="valid-c",
        started_at=datetime(2026, 8, 19, 9, 0, tzinfo=UTC),
    )

    summaries = repository.list_summaries()

    assert [summary.metadata.session_id for summary in summaries] == ["valid-c", "valid-a"]


def test_list_summaries_skips_metadata_ids_that_do_not_match_their_directories(
    tmp_path: Path,
) -> None:
    repository = SessionRepository(tmp_path / "sessions")
    roots = (
        _create_session(
            repository,
            session_id="directory-a",
            started_at=datetime(2026, 8, 19, 8, 0, tzinfo=UTC),
        ),
        _create_session(
            repository,
            session_id="directory-b",
            started_at=datetime(2026, 8, 19, 8, 0, tzinfo=UTC),
        ),
    )
    for root in roots:
        metadata_path = root / "metadata.json"
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        metadata["id"] = "duplicate"
        metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

    assert repository.list_summaries() == ()
    assert repository.read_summary("duplicate") is None
