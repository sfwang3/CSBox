from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from csbox.api.errors import ApiPersistenceError
from csbox.api.repository import ApiRunRepository
from csbox.check.service import CheckService, CheckServiceError
from csbox.core.models import EnvironmentSnapshot, HomeSnapshot, RecentApiRun, RecentExperiment
from csbox.lab.ports import HomeDataSource
from csbox.lab.repository import SessionRepository


class RealHomeDataSource(HomeDataSource):
    """Build Home state from the current project and persisted sessions."""

    source_id = "real"

    def __init__(
        self,
        repository: SessionRepository,
        project_dir: Path | str,
        *,
        api_repository: ApiRunRepository | None = None,
        check_service: CheckService | None = None,
    ) -> None:
        self.repository = repository
        self.project_dir = Path(project_dir)
        self.api_repository = api_repository
        self.check_service = check_service

    def get_home_snapshot(self, environment: EnvironmentSnapshot) -> HomeSnapshot:
        experiments = [
            RecentExperiment(
                name=summary.metadata.experiment_name,
                session_id=summary.metadata.session_id,
                status=summary.metadata.status,
                duration=_duration(summary.metadata.started_at, summary.metadata.ended_at),
                demo=False,
                capture_count=summary.capture_count,
                platform=summary.metadata.platform,
                cwd=summary.metadata.cwd,
            )
            for summary in self.repository.list_sessions()[:5]
        ]
        api_runs = self._recent_api_runs()
        check_status = self._check_status()
        return HomeSnapshot(
            environment=environment,
            recent_experiments=experiments,
            recent_api_runs=api_runs,
            check_status=check_status,
            project_dir=self.project_dir,
        )

    def _recent_api_runs(self) -> list[RecentApiRun]:
        repository = self.api_repository or ApiRunRepository.from_cwd(self.project_dir)
        try:
            return [
                RecentApiRun(
                    id=summary.id,
                    scenario_name=summary.scenario_name,
                    status=summary.status,
                    started_at=summary.started_at,
                    elapsed_ms=summary.elapsed_ms,
                )
                for summary in repository.list()[:5]
            ]
        except (ApiPersistenceError, OSError, UnicodeError, ValueError):
            return []

    def _check_status(self) -> str | None:
        service = self.check_service or CheckService()
        try:
            return service.run(self.project_dir).status.value
        except (OSError, UnicodeError, ValueError, CheckServiceError):
            return None


def _duration(started_at: datetime, ended_at: datetime | None) -> str:
    end = ended_at or datetime.now(UTC)
    seconds = max(0, int((end - started_at).total_seconds()))
    if seconds < 60:
        return f"{seconds} s"
    return f"{seconds // 60} min"
