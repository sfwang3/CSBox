from __future__ import annotations

import os
from collections.abc import Mapping

_SAFE_ENVIRONMENT_NAMES = frozenset(
    {
        "APPDATA",
        "CI",
        "COMSPEC",
        "GRADLE_USER_HOME",
        "HOME",
        "JAVA_HOME",
        "JDK_HOME",
        "LANG",
        "LC_ALL",
        "LOCALAPPDATA",
        "LOGNAME",
        "NODE_ENV",
        "NPM_CONFIG_CACHE",
        "PATH",
        "PATHEXT",
        "PROGRAMDATA",
        "SHELL",
        "SYSTEMROOT",
        "TEMP",
        "TERM",
        "TMP",
        "TMPDIR",
        "USER",
        "USERPROFILE",
        "UV_CACHE_DIR",
        "VIRTUAL_ENV",
        "WINDIR",
        "XDG_CACHE_HOME",
    }
)


def minimal_subprocess_environment(
    source: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Return the small non-secret environment needed by local build tools."""

    environment = os.environ if source is None else source
    safe: dict[str, str] = {}
    for name, value in environment.items():
        normalized = name.upper()
        if normalized in _SAFE_ENVIRONMENT_NAMES or normalized.startswith("LC_"):
            safe[name] = value
    return safe


__all__ = ["minimal_subprocess_environment"]
