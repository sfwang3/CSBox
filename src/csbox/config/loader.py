from __future__ import annotations

import json
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from csbox.config.models import CSBoxConfig
from csbox.config.paths import ConfigPaths
from csbox.core.safe_paths import atomic_write_text, ensure_private_directory, read_regular_text

_MAX_CONFIG_BYTES = 1024 * 1024


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
        details = []
        for item in error.errors(include_url=False, include_context=False):
            location = ".".join(str(part) for part in item["loc"])
            details.append(f"{location}: {item['type']}；请修改该字段后重试")
        raise ConfigurationError(path, "；".join(details)) from error


def _read_toml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        data = tomllib.loads(read_regular_text(path, max_bytes=_MAX_CONFIG_BYTES))
    except (
        OSError,
        UnicodeDecodeError,
        tomllib.TOMLDecodeError,
        ValueError,
        RecursionError,
    ) as error:
        raise ConfigurationError(path, "文件无法安全读取或解析；请检查 TOML 后重试") from error
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
        if path is not None:
            config_data = _merge_dicts(config_data, _read_toml(path))
    if overrides:
        override_path = Path("<命令行覆盖>")
        _validate(overrides, override_path)
        config_data = _merge_dicts(config_data, overrides)
    try:
        return CSBoxConfig.model_validate(config_data)
    except ValidationError as error:
        details = []
        for item in error.errors(include_url=False, include_context=False):
            location = ".".join(str(part) for part in item["loc"])
            details.append(f"{location}: {item['type']}；请修改该字段后重试")
        raise ConfigurationError(resolved_paths.project, "；".join(details)) from error


def _toml_value(value: str | int | float | bool) -> str:
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def _serialize_config(config: CSBoxConfig) -> str:
    data = config.model_dump(mode="python", exclude_none=True)
    lines = [f"locale = {_toml_value(data['locale'])}", ""]
    for section in ("student", "course", "lab", "render", "pack", "check", "api"):
        lines.append(f"[{section}]")
        for key, value in data[section].items():
            if section == "api" and key == "variables":
                continue
            lines.append(f"{key} = {_toml_value(value)}")
        lines.append("")
    if data["api"]["variables"]:
        lines.append("[api.variables]")
        for key, value in data["api"]["variables"].items():
            lines.append(f"{json.dumps(key, ensure_ascii=False)} = {_toml_value(value)}")
        lines.append("")
    return "\n".join(lines)


def save_project_config(config: CSBoxConfig, cwd: Path) -> Path:
    """Atomically persist the known configuration schema in the project directory."""
    destination = Path(cwd) / ".csbox" / "config.toml"
    try:
        ensure_private_directory(destination.parent)
        atomic_write_text(destination, _serialize_config(config))
    except (OSError, ValueError) as error:
        raise ConfigurationError(destination, "配置无法安全保存") from error
    return destination
