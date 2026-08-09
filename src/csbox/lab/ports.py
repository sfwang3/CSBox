from __future__ import annotations

from typing import Protocol

from csbox.core.models import EnvironmentSnapshot, HomeSnapshot
from csbox.core.registry import Registry


class HomeDataSource(Protocol):
    source_id: str

    def get_home_snapshot(self, environment: EnvironmentSnapshot) -> HomeSnapshot: ...


HOME_DATA_SOURCES = Registry[HomeDataSource]("home-data-source")
