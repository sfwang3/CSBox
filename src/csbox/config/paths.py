from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class ConfigPaths:
    user: Path | None
    project: Path

    @property
    def api_runs(self) -> Path:
        """Return the project-local root for persisted API runs."""

        return self.project.parent / "api" / "runs"

    @classmethod
    def from_cwd(cls, cwd: Path, *, environ: Mapping[str, str] | None = None) -> ConfigPaths:
        environment = os.environ if environ is None else environ
        appdata = environment.get("APPDATA")
        if appdata:
            user = Path(appdata) / "CSBox" / "config.toml"
        else:
            config_home = environment.get("XDG_CONFIG_HOME")
            if config_home:
                user = Path(config_home) / "csbox" / "config.toml"
            else:
                home_value = environment.get("HOME")
                home = Path(home_value) if home_value else Path.home()
                user = home / ".config" / "csbox" / "config.toml"
        return cls(user=user, project=Path(cwd) / ".csbox" / "config.toml")

    @classmethod
    def project_only(cls, cwd: Path) -> ConfigPaths:
        """Return paths for commands that must not read user-level configuration."""

        return cls(user=None, project=Path(cwd) / ".csbox" / "config.toml")
