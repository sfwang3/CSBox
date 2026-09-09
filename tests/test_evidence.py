from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from csbox.core.events import TerminalSize
from csbox.evidence.models import EvidenceItem, EvidenceSet, EvidenceSource
from csbox.evidence.repository import EvidencePersistenceError, EvidenceSetRepository
from csbox.evidence.resolver import LabCaptureResolver
from csbox.lab.captures import CaptureStore
from csbox.lab.repository import SessionRepository
from csbox.lab.screen import TerminalCell, TerminalCursor, TerminalSnapshot


def _snapshot(relative_time: float = 0.0) -> TerminalSnapshot:
    return TerminalSnapshot(
        rows=1,
        columns=4,
        cells=(
            (TerminalCell("中", width=2), TerminalCell(" "), TerminalCell(" "), TerminalCell(" ")),
        ),
        cursor=TerminalCursor(row=0, column=0),
        relative_time=relative_time,
    )


def make_completed_session_with_capture(
    tmp_path: Path, *, title: str
) -> tuple[SessionRepository, object, object]:
    repository = SessionRepository(tmp_path / "sessions")
    paths = repository.create_starting(
        "实验一",
        shell="bash",
        shell_version=None,
        size=TerminalSize(columns=80, rows=24),
        cwd=tmp_path,
        session_id="session-1",
        started_at=datetime(2026, 8, 31, 0, 0, tzinfo=UTC),
    )
    capture = CaptureStore(paths.captures).create_capture(
        _snapshot(), timestamp=0.0, cwd=tmp_path, title=title
    )
    repository.finish(
        paths,
        "completed",
        ended_at=datetime(2026, 8, 31, 0, 1, tzinfo=UTC),
    )
    return repository, paths, capture


class ReadOnlySessionRepository(SessionRepository):
    def read_summary(self, session_id: str) -> object:
        raise AssertionError(f"read_summary must not be used: {session_id}")

    def list_summaries(self) -> tuple[object, ...]:
        raise AssertionError("list_summaries must not be used for source resolution")

    def list_sessions(self) -> tuple[object, ...]:
        raise AssertionError("list_sessions must not be used for source resolution")

    def resolve(self, identifier: str) -> object:
        raise AssertionError(f"resolve must not be used for source resolution: {identifier}")

    def recover_stale_running(self) -> tuple[object, ...]:
        raise AssertionError("recover_stale_running must not be used for source resolution")


def test_create_uses_generated_id_for_filename_and_preserves_title(tmp_path: Path) -> None:
    repository = EvidenceSetRepository(
        tmp_path / ".csbox" / "evidence",
        id_factory=lambda: "set-123",
        clock=lambda: datetime(2026, 8, 31, 1, 2, 3, 456789, tzinfo=UTC),
    )

    created = repository.create("中文标题 / 原样保留")

    assert created.evidence_set_id == "set-123"
    assert created.title == "中文标题 / 原样保留"
    assert repository.path_for("set-123") == tmp_path / ".csbox" / "evidence" / "set-123.json"
    assert repository.path_for("set-123").exists()


def test_title_edit_does_not_rename_file_and_duplicate_titles_are_allowed(tmp_path: Path) -> None:
    repository = EvidenceSetRepository(tmp_path / "evidence", id_factory=iter(("a", "b")).__next__)
    first = repository.create("同名")
    second = repository.create("同名")

    saved = repository.save(first.model_copy(update={"title": "改名"}))

    assert saved.title == "改名"
    assert repository.path_for("a").exists()
    assert repository.path_for("b").exists()
    assert not (tmp_path / "evidence" / "改名.json").exists()
    assert [summary.title for summary in repository.list_summaries() if summary.readable] == [
        "改名",
        "同名",
    ]
    assert second.evidence_set_id == "b"


def test_save_preserves_created_at_even_if_candidate_has_different_value(tmp_path: Path) -> None:
    repository = EvidenceSetRepository(
        tmp_path / "evidence",
        id_factory=lambda: "set-1",
        clock=iter(
            (
                datetime(2026, 8, 31, 1, 0, tzinfo=UTC),
                datetime(2026, 8, 31, 1, 1, tzinfo=UTC),
            )
        ).__next__,
    )
    created = repository.create("集合")
    candidate = created.model_copy(
        update={
            "created_at": datetime(2030, 1, 1, tzinfo=UTC),
            "title": "修改后的集合",
        }
    )

    saved = repository.save(candidate)

    assert saved.created_at == created.created_at
    assert repository.load("set-1").created_at == created.created_at


def test_read_only_load_and_list_do_not_change_evidence_set_metadata(
    tmp_path: Path,
) -> None:
    repository = EvidenceSetRepository(tmp_path / "evidence", id_factory=lambda: "set-1")
    created = repository.create("只读集合")
    before = repository.path_for("set-1").read_bytes()

    assert repository.load("set-1").updated_at == created.updated_at
    assert repository.list_summaries()[0].updated_at == created.updated_at
    assert repository.path_for("set-1").read_bytes() == before


def test_evidence_item_has_no_item_id_or_order_and_source_key_is_complete() -> None:
    source = EvidenceSource(source_type="lab_capture", session_id="session", capture_id="capture")
    item = EvidenceItem(source=source, title="标题")

    assert source.equality_key == ("lab_capture", "session", "capture")
    assert item.caption == ""
    assert item.note == ""
    assert "item_id" not in EvidenceItem.model_fields
    assert "order" not in EvidenceItem.model_fields


def test_cjk_and_timestamp_round_trip_uses_canonical_utc_z(tmp_path: Path) -> None:
    now = datetime(2026, 8, 31, 4, 5, 6, 700000, tzinfo=UTC)
    repository = EvidenceSetRepository(
        tmp_path / "evidence",
        id_factory=lambda: "中文-safe",
        clock=lambda: now,
    )
    created = repository.create("实验一 🧪")
    repository.save(created.model_copy(update={"title": "实验一（修改）"}))

    loaded = repository.load("中文-safe")
    raw = repository.path_for("中文-safe").read_text(encoding="utf-8")

    assert loaded.title == "实验一（修改）"
    assert loaded.created_at == now
    assert loaded.updated_at == now
    assert "2026-08-31T04:05:06.700000Z" in raw
    assert "实验一（修改）" in raw


def test_failed_save_keeps_old_file_and_input_updated_at(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = EvidenceSetRepository(
        tmp_path / "evidence",
        id_factory=lambda: "set-1",
        clock=iter(
            (
                datetime(2026, 8, 31, 1, 0, tzinfo=UTC),
                datetime(2026, 8, 31, 1, 1, tzinfo=UTC),
            )
        ).__next__,
    )
    created = repository.create("原始标题")
    original_bytes = repository.path_for("set-1").read_bytes()

    def fail_write(*args: object, **kwargs: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr("csbox.evidence.repository.atomic_write_text", fail_write)

    with pytest.raises(EvidencePersistenceError):
        repository.save(created.model_copy(update={"title": "新标题"}))

    assert repository.path_for("set-1").read_bytes() == original_bytes
    assert repository.load("set-1").title == "原始标题"
    assert repository.load("set-1").updated_at == created.updated_at


def test_create_does_not_replace_file_that_appears_after_precheck(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import csbox.evidence.repository as repository_module

    repository = EvidenceSetRepository(tmp_path / "evidence", id_factory=lambda: "set-1")
    real_create = repository_module.atomic_create_text

    def create_race(destination: Path, payload: str) -> None:
        destination.write_text("attacker-owned", encoding="utf-8")
        real_create(destination, payload)

    monkeypatch.setattr(repository_module, "atomic_create_text", create_race)

    with pytest.raises(EvidencePersistenceError):
        repository.create("不能覆盖")

    assert repository.path_for("set-1").read_text(encoding="utf-8") == "attacker-owned"


def test_oversized_save_keeps_old_file_and_updated_at(tmp_path: Path) -> None:
    repository = EvidenceSetRepository(tmp_path / "evidence", id_factory=lambda: "set-1")
    created = repository.create("原始标题")
    original_bytes = repository.path_for("set-1").read_bytes()
    oversized_title = "x" * (4 * 1024 * 1024)

    with pytest.raises(EvidencePersistenceError):
        repository.save(created.model_copy(update={"title": oversized_title}))

    assert repository.path_for("set-1").read_bytes() == original_bytes
    assert repository.load("set-1").updated_at == created.updated_at


def test_corrupt_and_unknown_version_files_do_not_hide_healthy_sets(tmp_path: Path) -> None:
    repository = EvidenceSetRepository(
        tmp_path / "evidence", id_factory=iter(("healthy",)).__next__
    )
    repository.create("健康集合")
    (tmp_path / "evidence" / "broken.json").write_text("{bad", encoding="utf-8")
    (tmp_path / "evidence" / "future.json").write_text(
        json.dumps({"version": 99, "id": "future"}, ensure_ascii=False), encoding="utf-8"
    )

    summaries = {summary.evidence_set_id: summary for summary in repository.list_summaries()}

    assert summaries["healthy"].readable is True
    assert summaries["healthy"].title == "健康集合"
    assert summaries["broken"].readable is False
    assert summaries["broken"].title == "不可读取"
    assert summaries["broken"].item_count is None
    assert summaries["future"].readable is False


def test_recursive_document_failure_isolated_from_healthy_list(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = EvidenceSetRepository(tmp_path / "evidence", id_factory=lambda: "healthy")
    repository.create("健康集合")
    broken = tmp_path / "evidence" / "broken.json"
    broken.write_text("{}", encoding="utf-8")
    real_read = repository._read

    def read_with_recursive_failure(path: Path, *, expected_id: str) -> EvidenceSet:
        if expected_id == "broken":
            raise RecursionError("nested document")
        return real_read(path, expected_id=expected_id)

    monkeypatch.setattr(repository, "_read", read_with_recursive_failure)

    summaries = {summary.evidence_set_id: summary for summary in repository.list_summaries()}

    assert summaries["healthy"].readable is True
    assert summaries["broken"].readable is False


def test_resolver_returns_exact_capture_without_replay(tmp_path: Path) -> None:
    session_repository, paths, capture = make_completed_session_with_capture(
        tmp_path, title="源标题"
    )
    resolver = LabCaptureResolver(session_repository)

    resolved = resolver.resolve(
        EvidenceSource(
            source_type="lab_capture",
            session_id=paths.root.name,
            capture_id=capture.capture_id,
        )
    )

    assert resolved.available is True
    assert resolved.session_name == "实验一"
    assert resolved.capture is not None
    assert resolved.capture.capture_id == capture.capture_id
    assert resolved.capture.title == "源标题"


def test_resolver_retains_controlled_missing_source_state(tmp_path: Path) -> None:
    resolver = LabCaptureResolver(SessionRepository(tmp_path / "sessions"))

    resolved = resolver.resolve(
        EvidenceSource(
            source_type="lab_capture",
            session_id="missing-session",
            capture_id="missing-capture",
        )
    )

    assert resolved.available is False
    assert resolved.capture is None
    assert resolved.unavailable_reason == "session_missing"


@pytest.mark.skipif(
    os.name == "nt", reason="symlink creation may require elevated Windows privileges"
)
def test_resolver_does_not_follow_symlinked_session_root(tmp_path: Path) -> None:
    real_root = tmp_path / "real-sessions"
    real_root.mkdir()
    session_repository = SessionRepository(real_root)
    paths = session_repository.create_starting(
        "实验一",
        shell="bash",
        shell_version=None,
        size=TerminalSize(columns=80, rows=24),
        cwd=tmp_path,
        session_id="session-1",
        started_at=datetime(2026, 8, 31, 0, 0, tzinfo=UTC),
    )
    capture = CaptureStore(paths.captures).create_capture(_snapshot(), cwd=tmp_path, title="源")
    session_repository.finish(
        paths,
        "completed",
        ended_at=datetime(2026, 8, 31, 0, 1, tzinfo=UTC),
    )
    linked_root = tmp_path / "linked-sessions"
    linked_root.symlink_to(real_root, target_is_directory=True)

    resolved = LabCaptureResolver(SessionRepository(linked_root)).resolve(
        EvidenceSource(
            source_type="lab_capture",
            session_id=paths.root.name,
            capture_id=capture.capture_id,
        )
    )

    assert resolved.available is False
    assert resolved.unavailable_reason == "session_missing"


def test_resolver_does_not_treat_unsupported_source_type_as_lab_path(tmp_path: Path) -> None:
    resolver = LabCaptureResolver(SessionRepository(tmp_path / "sessions"))

    resolved = resolver.resolve(
        SimpleNamespace(
            source_type="future_source",
            session_id="session",
            capture_id="capture",
        )
    )

    assert resolved.available is False
    assert resolved.unavailable_reason == "unsupported_source_type"


def test_resolver_reads_source_without_summary_recovery_or_metadata_mutation(
    tmp_path: Path,
) -> None:
    session_repository, paths, capture = make_completed_session_with_capture(
        tmp_path, title="只读来源"
    )
    read_only_repository = ReadOnlySessionRepository(session_repository.root)
    metadata_before = paths.metadata.read_bytes()

    resolved = LabCaptureResolver(read_only_repository).resolve(
        EvidenceSource(
            source_type="lab_capture",
            session_id=paths.root.name,
            capture_id=capture.capture_id,
        )
    )

    assert resolved.available is True
    assert paths.metadata.read_bytes() == metadata_before


def test_read_only_session_summaries_do_not_recover_stale_running_metadata(
    tmp_path: Path,
) -> None:
    repository = SessionRepository(tmp_path / "sessions")
    paths = repository.create_starting(
        "仍在录制",
        shell="bash",
        shell_version=None,
        size=TerminalSize(columns=80, rows=24),
        cwd=tmp_path,
        session_id="running-session",
        started_at=datetime(2026, 8, 31, 0, 0, tzinfo=UTC),
    )
    repository.release_owner(paths)
    metadata_before = paths.metadata.read_bytes()
    owner_lock_exists_before = paths.owner_lock.exists()
    owner_lock_before = paths.owner_lock.read_bytes() if owner_lock_exists_before else None
    read_only_repository = ReadOnlySessionRepository(repository.root)

    summaries = LabCaptureResolver(read_only_repository).list_sessions()

    assert summaries[0].metadata.status == "running"
    assert paths.metadata.read_bytes() == metadata_before
    assert paths.owner_lock.exists() is owner_lock_exists_before
    assert (
        paths.owner_lock.read_bytes() if owner_lock_exists_before else None
    ) == owner_lock_before
