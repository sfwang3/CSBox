from __future__ import annotations

import json
import os
import tempfile
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from csbox.config.models import CSBoxConfig
from csbox.config.paths import ConfigPaths


class ConfigurationError(Exception):
    def __init__(self, path: Path, message: str) -> None:
        self.path = path
        self.message = message
        super().__init__(path, message)

    def __str__(self) -> str:
        return f"配置错误：{self.path}：{self.message}"


def _merge_dicts(base: dict[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    merged = base.copy()
    for key, value in override.items():
        if isinstance(value, Mapping) and isinstance(merged.get(key), Mapping):
            merged[key] = _merge_dicts(dict(merged[key]), value)
        else:
            merged[key] = value
    return merged


def _validate(data: Mapping[str, Any], path: Path) -> None:
    try:
        CSBoxConfig.model_validate(data)
    except ValidationError as error:
        raise ConfigurationError(path, str(error)) from error


def _read_toml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise ConfigurationError(path, str(error)) from error
    _validate(data, path)
    return data


def load_config(
    cwd: Path,
    *,
    environ: Mapping[str, str] | None = None,
    overrides: Mapping[str, Any] | None = None,
    paths: ConfigPaths | None = None,
) -> CSBoxConfig:
    """Load defaults, user and project configuration, then CLI overrides."""
    resolved_paths = paths or ConfigPaths.from_cwd(cwd, environ=environ)
    config_data = CSBoxConfig().model_dump(mode="python")
    for path in (resolved_paths.user, resolved_paths.project):
        config_data = _merge_dicts(config_data, _read_toml(path))
    if overrides:
        override_path = Path("<命令行覆盖>")
        _validate(overrides, override_path)
        config_data = _merge_dicts(config_data, overrides)
    try:
        return CSBoxConfig.model_validate(config_data)
    except ValidationError as error:
        raise ConfigurationError(resolved_paths.project, str(error)) from error


def _toml_value(value: str | int) -> str:
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def _serialize_config(config: CSBoxConfig) -> str:
    data = config.model_dump(mode="python", exclude_none=True)
    lines = [f"locale = {_toml_value(data['locale'])}", ""]
    for section in ("student", "course", "lab", "render", "pack", "check"):
        lines.append(f"[{section}]")
        for key, value in data[section].items():
            lines.append(f"{key} = {_toml_value(value)}")
        lines.append("")
    return "\n".join(lines)


def save_project_config(config: CSBoxConfig, cwd: Path) -> Path:
    """Atomically persist the known configuration schema in the project directory."""
    destination = Path(cwd) / ".csbox" / "config.toml"
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=destination.parent,
            prefix=".config-",
            suffix=".tmp",
            delete=False,
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
            temporary_file.write(_serialize_config(config))
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        os.replace(temporary_path, destination)
    except OSError as error:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise ConfigurationError(destination, str(error)) from error
    return destination
