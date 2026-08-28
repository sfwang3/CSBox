from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from csbox.core.models import EnvironmentSnapshot, HomeSnapshot
from csbox.lab.ports import HomeDataSource
from csbox.lab.repository import SessionRepository

if TYPE_CHECKING:
    from csbox.api.repository import ApiRunRepository
    from csbox.check.service import CheckService


class RealHomeDataSource(HomeDataSource):
    """Build a lightweight Home snapshot without opening persisted histories."""

    source_id = "real"

    def __init__(
        self,
        repository: SessionRepository,
        project_dir: Path | str,
        *,
        api_repository: ApiRunRepository | None = None,
        check_service: CheckService | None = None,
    ) -> None:
        # These collaborators remain accepted for source compatibility. Home deliberately
        # does not enumerate them; their full workflows load only when the user opens one.
        self.repository = repository
        self.project_dir = Path(project_dir)
        self.api_repository = api_repository
        self.check_service = check_service

    def get_home_snapshot(self, environment: EnvironmentSnapshot) -> HomeSnapshot:
        return HomeSnapshot(environment=environment, project_dir=self.project_dir)
