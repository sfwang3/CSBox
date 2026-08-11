from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from csbox.core.models import EnvironmentSnapshot, HomeSnapshot, RecentExperiment
from csbox.lab.ports import HomeDataSource
from csbox.lab.repository import SessionRepository


class RealHomeDataSource(HomeDataSource):
    """Build Home state from the current project and persisted sessions."""

    source_id = "real"

    def __init__(self, repository: SessionRepository, project_dir: Path | str) -> None:
        self.repository = repository
        self.project_dir = Path(project_dir)

    def get_home_snapshot(self, environment: EnvironmentSnapshot) -> HomeSnapshot:
        experiments = [
            RecentExperiment(
                name=summary.metadata.experiment_name,
                status=summary.metadata.status,
                duration=_duration(summary.metadata.started_at, summary.metadata.ended_at),
                demo=False,
                capture_count=summary.capture_count,
                platform=summary.metadata.platform,
                cwd=summary.metadata.cwd,
            )
            for summary in self.repository.list_sessions()[:5]
        ]
        return HomeSnapshot(
            environment=environment,
            recent_experiments=experiments,
            project_dir=self.project_dir,
        )


def _duration(started_at: datetime, ended_at: datetime | None) -> str:
    end = ended_at or datetime.now(UTC)
    seconds = max(0, int((end - started_at).total_seconds()))
    if seconds < 60:
        return f"{seconds} s"
    return f"{seconds // 60} min"
