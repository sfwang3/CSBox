from __future__ import annotations

import re
from collections.abc import Mapping, Set
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from csbox.api.errors import ApiConfigError

_IDENTIFIER = r"[A-Za-z_][A-Za-z0-9_]*"
_PLACEHOLDER_RE = re.compile(r"{{(" + _IDENTIFIER + r")}}")
_IDENTIFIER_RE = re.compile(r"^" + _IDENTIFIER + r"$")
_TODO_PREFIX = "TODO_"


@dataclass(frozen=True, slots=True)
class VariableResolution:
    """The selected variable values and names that still need user input."""

    values: Mapping[str, str] = field(default_factory=dict)
    missing_names: frozenset[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        object.__setattr__(self, "values", MappingProxyType(dict(self.values)))
        object.__setattr__(self, "missing_names", frozenset(self.missing_names))

    @property
    def user_message(self) -> str:
        """A value-free message suitable for normal terminal output."""

        if not self.missing_names:
            return "变量解析完成。"
        names = "、".join(sorted(self.missing_names))
        return (
            f"发生了什么：缺少或不安全的变量占位符（{names}）。"
            "在哪里：变量解析。"
            "怎么处理：在项目 [api.variables]、环境变量、场景 [variables] "
            "或命令行 --var 中提供安全值后重试。"
        )


def resolve_variables(
    name_set: Set[str],
    project: Mapping[str, str],
    scenario: Mapping[str, str],
    environ: Mapping[str, str],
    cli: Mapping[str, str],
) -> VariableResolution:
    """Resolve only referenced names with project < environment < scenario < CLI."""

    values: dict[str, str] = {}
    missing_names: set[str] = set()
    for name in name_set:
        _validate_identifier(name)
        if _is_todo_name(name):
            missing_names.add(name)
            continue
        value = _selected_value(name, project, scenario, environ, cli)
        if value is None or _is_unresolved_value(value):
            missing_names.add(name)
            continue
        values[name] = value
    return VariableResolution(values=values, missing_names=frozenset(missing_names))


def interpolate(value: Any, variables: Mapping[str, str]) -> Any:
    """Perform static ``{{identifier}}`` substitution without evaluating expressions."""

    if isinstance(value, str):
        return _interpolate_string(value, variables)
    if isinstance(value, dict):
        return {key: interpolate(item, variables) for key, item in value.items()}
    if isinstance(value, list):
        return [interpolate(item, variables) for item in value]
    if isinstance(value, tuple):
        return tuple(interpolate(item, variables) for item in value)
    return value


def validate_placeholders(value: Any) -> None:
    """Reject non-static interpolation syntax while keeping valid templates unresolved."""

    if isinstance(value, str):
        _validate_placeholder_syntax(value)
        return
    if isinstance(value, Mapping):
        for item in value.values():
            validate_placeholders(item)
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            validate_placeholders(item)


def _selected_value(
    name: str,
    project: Mapping[str, str],
    scenario: Mapping[str, str],
    environ: Mapping[str, str],
    cli: Mapping[str, str],
) -> str | None:
    value = project.get(name)
    if name in environ:
        value = environ[name]
    elif f"CSBOX_VAR_{name}" in environ:
        value = environ[f"CSBOX_VAR_{name}"]
    if name in scenario:
        value = scenario[name]
    if name in cli:
        value = cli[name]
    if value is not None and not isinstance(value, str):
        raise _error("变量值类型无效", f"变量 {name}")
    return value


def _interpolate_string(value: str, variables: Mapping[str, str]) -> str:
    _validate_placeholder_syntax(value)

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if _is_todo_name(name) or name not in variables:
            raise _error("缺少或不安全的变量占位符", f"变量 {name}")
        replacement = variables[name]
        if not isinstance(replacement, str):
            raise _error("变量值类型无效", f"变量 {name}")
        if _is_unresolved_value(replacement):
            raise _error("缺少或不安全的变量占位符", f"变量 {name}")
        return replacement

    return _PLACEHOLDER_RE.sub(replace, value)


def _validate_placeholder_syntax(value: str) -> None:
    remainder = _PLACEHOLDER_RE.sub("", value)
    if "{{" in remainder or "}}" in remainder:
        raise _error("变量占位符格式不安全或不受支持", "变量占位符")


def _validate_identifier(name: str) -> None:
    if not _IDENTIFIER_RE.fullmatch(name):
        raise _error("变量名格式不安全或不受支持", "变量名")


def _is_todo_name(name: str) -> bool:
    return name == "TODO" or name.startswith(_TODO_PREFIX)


def _is_unresolved_value(value: str) -> bool:
    _validate_placeholder_syntax(value)
    return value == "TODO" or value.startswith("TODO_") or _PLACEHOLDER_RE.search(value) is not None


def _error(what: str, where: str) -> ApiConfigError:
    return ApiConfigError(
        f"发生了什么：{what}。在哪里：{where}。"
        "怎么处理：仅使用 {{identifier}}，并提供明确的变量值后重试。"
    )


__all__ = ["VariableResolution", "interpolate", "resolve_variables", "validate_placeholders"]
