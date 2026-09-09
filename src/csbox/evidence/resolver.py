from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError

from csbox.api.errors import ApiPersistenceError
from csbox.api.evidence import ApiEvidenceBuilder, redact_evidence
from csbox.api.models import ApiEvidence
from csbox.api.repository import ApiRunRepository, ApiRunSummary
from csbox.evidence.models import ApiStepSource, LabCaptureSource, validate_identifier
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

    source: object
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

    def resolve(self, source: object) -> ResolvedLabCapture:
        """Resolve one exact Lab Capture without calling lifecycle-aware APIs."""

        if getattr(source, "source_type", None) != "lab_capture":
            return ResolvedLabCapture(
                source=source,
                session_name=None,
                capture=None,
                unavailable_reason="unsupported_source_type",
            )

        try:
            source = LabCaptureSource.model_validate(
                source.model_dump(mode="python") if hasattr(source, "model_dump") else source
            )
        except (TypeError, ValueError, ValidationError):
            return ResolvedLabCapture(
                source=source,
                session_name=None,
                capture=None,
                unavailable_reason="invalid_source_reference",
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


@dataclass(frozen=True, slots=True)
class ApiStepSummary:
    """Safe metadata for one executed API step shown by the Evidence browser."""

    source: ApiStepSource
    scenario_name: str | None
    step_name: str
    step_status: str | None
    run_status: str | None


@dataclass(frozen=True, slots=True)
class ResolvedApiStep:
    """A redacted API Evidence view or a stable unavailable reason."""

    source: object
    scenario_name: str | None
    step_name: str | None
    run_status: str | None
    evidence: ApiEvidence | None
    unavailable_reason: str | None

    @property
    def available(self) -> bool:
        return self.evidence is not None and self.unavailable_reason is None


class ApiStepResolver:
    """Resolve exact steps from persisted API runs without executing scenarios."""

    def __init__(self, repository: ApiRunRepository) -> None:
        self.repository = repository

    def list_runs(self) -> tuple[ApiRunSummary, ...]:
        try:
            return self.repository.list()
        except (ApiPersistenceError, OSError, ValueError):
            return ()

    def list_steps(self, run_id: str) -> tuple[ApiStepSummary, ...]:
        validated = self._validated_run_id(run_id)
        if validated is None:
            return ()
        run, _ = self._load_run(validated)
        if run is None:
            return ()
        try:
            evidence_items = ApiEvidenceBuilder.from_run(run)
        except (ApiPersistenceError, TypeError, ValueError, RecursionError):
            return ()
        return tuple(
            ApiStepSummary(
                source=ApiStepSource(run_id=validated, step_index=item.step_index),
                scenario_name=item.scenario_name,
                step_name=item.title,
                step_status=None if item.result is None else item.result.status,
                run_status=item.run_status,
            )
            for item in evidence_items
            if item.step_index is not None
        )

    def resolve(self, source: object) -> ResolvedApiStep:
        if not isinstance(source, ApiStepSource):
            return ResolvedApiStep(
                source=source,
                scenario_name=None,
                step_name=None,
                run_status=None,
                evidence=None,
                unavailable_reason="unsupported_source_type",
            )

        validated = self._validated_source(source)
        if validated is None:
            return ResolvedApiStep(
                source=source,
                scenario_name=None,
                step_name=None,
                run_status=None,
                evidence=None,
                unavailable_reason="invalid_source_reference",
            )

        run, reason = self._load_run(validated.run_id)
        if run is None:
            return ResolvedApiStep(
                source=validated,
                scenario_name=None,
                step_name=None,
                run_status=None,
                evidence=None,
                unavailable_reason=reason or "run_unavailable",
            )
        try:
            evidence_items = ApiEvidenceBuilder.from_run(run)
        except (ApiPersistenceError, TypeError, ValueError, RecursionError):
            return ResolvedApiStep(
                source=validated,
                scenario_name=None,
                step_name=None,
                run_status=None,
                evidence=None,
                unavailable_reason="run_unavailable",
            )

        selected = next(
            (item for item in evidence_items if item.step_index == validated.step_index),
            None,
        )
        if selected is None:
            return ResolvedApiStep(
                source=validated,
                scenario_name=None,
                step_name=None,
                run_status=run.status,
                evidence=None,
                unavailable_reason="step_missing",
            )
        try:
            safe_selected = redact_evidence(selected)
        except (ApiPersistenceError, TypeError, ValueError, RecursionError):
            return ResolvedApiStep(
                source=validated,
                scenario_name=None,
                step_name=None,
                run_status=run.status,
                evidence=None,
                unavailable_reason="run_unavailable",
            )
        return ResolvedApiStep(
            source=validated,
            scenario_name=safe_selected.scenario_name,
            step_name=safe_selected.title,
            run_status=safe_selected.run_status,
            evidence=safe_selected,
            unavailable_reason=None,
        )

    def _load_run(self, run_id: str):
        try:
            paths = self.repository.paths_for(run_id)
        except (ApiPersistenceError, OSError, ValueError):
            return None, "run_unavailable"
        if paths.root.is_symlink():
            return None, "run_unavailable"
        if not paths.root.exists():
            return None, "run_missing"
        if not paths.root.is_dir():
            return None, "run_unavailable"
        try:
            return self.repository.load(run_id), None
        except FileNotFoundError:
            return None, "run_missing"
        except (ApiPersistenceError, OSError, ValueError, RecursionError):
            return None, "run_unavailable"

    @staticmethod
    def _validated_run_id(run_id: object) -> str | None:
        try:
            return validate_identifier(run_id, field_name="run_id")
        except ValueError:
            return None

    @classmethod
    def _validated_source(cls, source: ApiStepSource) -> ApiStepSource | None:
        try:
            return ApiStepSource.model_validate(source.model_dump(mode="python"))
        except (TypeError, ValueError, ValidationError):
            return None


@dataclass(frozen=True, slots=True)
class ResolvedReportableEvidence:
    """Source-independent report payload with source-specific metadata."""

    source: object
    capture: CaptureRecord | None = None
    api_evidence: ApiEvidence | None = None
    session_name: str | None = None
    scenario_name: str | None = None
    step_name: str | None = None
    run_status: str | None = None
    unavailable_reason: str | None = None

    @property
    def available(self) -> bool:
        return self.unavailable_reason is None and (
            self.capture is not None or self.api_evidence is not None
        )


class EvidenceSourceResolver:
    """Route the two supported source families to their canonical resolvers."""

    def __init__(self, lab: LabCaptureResolver, api: ApiStepResolver) -> None:
        self.lab = lab
        self.api = api

    @property
    def repository(self) -> SessionRepository:
        return self.lab.repository

    @property
    def source_roots(self) -> tuple[Path, ...]:
        """Return canonical roots that must not become report destinations."""

        return (self.lab.repository.root, self.api.repository.root)

    def resolve(self, source: object) -> ResolvedReportableEvidence:
        if isinstance(source, LabCaptureSource):
            resolved = self.lab.resolve(source)
            return ResolvedReportableEvidence(
                source=resolved.source,
                capture=resolved.capture,
                session_name=resolved.session_name,
                unavailable_reason=resolved.unavailable_reason,
            )
        if isinstance(source, ApiStepSource):
            resolved = self.api.resolve(source)
            return ResolvedReportableEvidence(
                source=resolved.source,
                api_evidence=resolved.evidence,
                scenario_name=resolved.scenario_name,
                step_name=resolved.step_name,
                run_status=resolved.run_status,
                unavailable_reason=resolved.unavailable_reason,
            )
        return ResolvedReportableEvidence(
            source=source,
            unavailable_reason="unsupported_source_type",
        )


__all__ = [
    "ApiStepResolver",
    "ApiStepSummary",
    "EvidenceSourceResolver",
    "LabCaptureResolver",
    "ResolvedApiStep",
    "ResolvedLabCapture",
    "ResolvedReportableEvidence",
]
