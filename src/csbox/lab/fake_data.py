from __future__ import annotations

from csbox.core.models import EnvironmentSnapshot, HomeSnapshot, RecentExperiment
from csbox.lab.ports import HOME_DATA_SOURCES, HomeDataSource


class FakeHomeDataSource:
    source_id = "fake"

    def get_home_snapshot(self, environment: EnvironmentSnapshot) -> HomeSnapshot:
        return HomeSnapshot(
            environment=environment,
            recent_experiments=[
                RecentExperiment(
                    name="链路验证演示",
                    status="complete",
                    duration="8 min",
                    demo=True,
                ),
                RecentExperiment(
                    name="Shell 交互演示",
                    status="in_progress",
                    duration="5 min",
                    demo=True,
                ),
                RecentExperiment(
                    name="项目结构演示",
                    status="not_started",
                    duration="—",
                    demo=True,
                ),
            ],
        )


HOME_DATA_SOURCES.register("fake", FakeHomeDataSource())

__all__ = ["FakeHomeDataSource", "HOME_DATA_SOURCES", "HomeDataSource"]
