from __future__ import annotations

from dataclasses import dataclass

from csbox.evidence.models import EvidenceSource, validate_identifier
from csbox.lab.captures import CaptureStore
from csbox.lab.models import CaptureRecord, SessionMetadata, SessionPaths
from csbox.lab.repository import (
    SessionRepository,
    SessionRepositoryError,
    SessionSummary,
    load_session_metadata,
)


@dataclass(frozen=True, slots=True)
class ResolvedLabCapture:
    """Resolved Capture metadata or a stable reason why the source is unavailable."""

    source: EvidenceSource
    session_name: str | None
    capture: CaptureRecord | None
    unavailable_reason: str | None

    @property
    def available(self) -> bool:
        return self.capture is not None and self.unavailable_reason is None


class LabCaptureResolver:
    """Observe Lab metadata without entering the Lab lifecycle recovery boundary."""

    def __init__(self, repository: SessionRepository) -> None:
        self.repository = repository

    def list_sessions(self) -> tuple[SessionSummary, ...]:
        """List persisted sessions without stale recovery, lock probing, or cast reads."""

        root = self.repository.root
        if root.is_symlink() or not root.is_dir():
            return ()
        try:
            directories = sorted(root.iterdir(), key=lambda path: path.name)
        except OSError:
            return ()

        summaries: list[SessionSummary] = []
        for directory in directories:
            if directory.is_symlink() or not directory.is_dir():
                continue
            try:
                identifier = validate_identifier(directory.name, field_name="session_id")
                paths = SessionPaths(directory)
                metadata = load_session_metadata(paths)
                if metadata.session_id != identifier:
                    continue
                capture_count = CaptureStore(paths.captures).count()
            except (
                OSError,
                UnicodeError,
                ValueError,
                RecursionError,
                SessionRepositoryError,
            ):
                continue
            summaries.append(
                SessionSummary(
                    paths=paths,
                    metadata=metadata,
                    capture_count=capture_count,
                )
            )

        summaries.sort(key=lambda item: item.metadata.session_id)
        summaries.sort(key=lambda item: item.metadata.started_at, reverse=True)
        return tuple(summaries)

    def resolve(self, source: EvidenceSource) -> ResolvedLabCapture:
        """Resolve one exact Lab Capture without calling lifecycle-aware APIs."""

        if source.source_type != "lab_capture":
            return ResolvedLabCapture(
                source=source,
                session_name=None,
                capture=None,
                unavailable_reason="unsupported_source_type",
            )

        try:
            session_id = validate_identifier(source.session_id, field_name="session_id")
            capture_id = validate_identifier(source.capture_id, field_name="capture_id")
        except ValueError:
            return ResolvedLabCapture(
                source=source,
                session_name=None,
                capture=None,
                unavailable_reason="invalid_source_reference",
            )

        root = self.repository.root
        if root.is_symlink() or not root.is_dir():
            return ResolvedLabCapture(
                source=source,
                session_name=None,
                capture=None,
                unavailable_reason="session_missing",
            )
        paths = SessionPaths(root / session_id)
        if paths.root.is_symlink() or not paths.root.is_dir():
            return ResolvedLabCapture(
                source=source,
                session_name=None,
                capture=None,
                unavailable_reason="session_missing",
            )

        try:
            metadata: SessionMetadata = load_session_metadata(paths)
        except (OSError, UnicodeError, ValueError, RecursionError, SessionRepositoryError):
            return ResolvedLabCapture(
                source=source,
                session_name=None,
                capture=None,
                unavailable_reason="session_unavailable",
            )
        if metadata.session_id != session_id:
            return ResolvedLabCapture(
                source=source,
                session_name=None,
                capture=None,
                unavailable_reason="session_unavailable",
            )

        try:
            capture_result = CaptureStore(paths.captures).load()
        except (OSError, UnicodeError, ValueError, RecursionError):
            return ResolvedLabCapture(
                source=source,
                session_name=metadata.experiment_name,
                capture=None,
                unavailable_reason="capture_unavailable",
            )
        if capture_result.warnings:
            return ResolvedLabCapture(
                source=source,
                session_name=metadata.experiment_name,
                capture=None,
                unavailable_reason="capture_unavailable",
            )
        capture = next(
            (record for record in capture_result.captures if record.capture_id == capture_id),
            None,
        )
        if capture is None:
            reason = "capture_unavailable" if capture_result.warnings else "capture_missing"
            return ResolvedLabCapture(
                source=source,
                session_name=metadata.experiment_name,
                capture=None,
                unavailable_reason=reason,
            )
        return ResolvedLabCapture(
            source=source,
            session_name=metadata.experiment_name,
            capture=capture,
            unavailable_reason=None,
        )


__all__ = ["LabCaptureResolver", "ResolvedLabCapture"]
