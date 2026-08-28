"""Restricted OpenAPI 3.x structure import for static API scenario templates."""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

import yaml
from pydantic import PrivateAttr
from yaml.nodes import MappingNode, Node, ScalarNode, SequenceNode

from csbox.api.errors import ApiConfigError
from csbox.api.models import ApiRequest, ApiScenario, ApiStep
from csbox.api.scenario_writer import ScenarioFile, write_scenario_files
from csbox.core.safe_paths import (
    read_regular_text,
)

MAX_OPENAPI_BYTES = 16 * 1024 * 1024
_MAX_OPENAPI_DEPTH = 64
_MAX_OPENAPI_NODES = 100_000

_METHODS = frozenset({"get", "post", "put", "patch", "delete", "head", "options"})
_PARAMETER_LOCATIONS = frozenset({"path", "query", "header"})
_OPENAPI_VERSION_RE = re.compile(r"^3\.(?:0|1)(?:\.\d+)?(?:[-+][0-9A-Za-z.-]+)?$")
_RESPONSE_STATUS_RE = re.compile(r"^[1-5](?:\d|X|x){2}$")
_SERVER_VARIABLE_RE = re.compile(r"\{([^{}]+)\}")
_PLACEHOLDER_NAME_RE = re.compile(r"[^A-Za-z0-9_]+")
_SECRET_FIELD_NAMES = frozenset(
    {
        "password",
        "passwd",
        "token",
        "access_token",
        "refresh_token",
        "jwt",
        "secret",
        "api_key",
        "apikey",
        "client_secret",
        "authorization",
        "cookie",
        "set-cookie",
        "proxy-authorization",
    }
)
_NORMALIZED_SECRET_FIELDS = frozenset(
    "".join(character for character in field.casefold() if character.isalnum())
    for field in _SECRET_FIELD_NAMES
)


@dataclass(frozen=True)
class OpenApiOperation:
    """A validated subset of one OpenAPI operation."""

    path: str
    method: str
    operation_id: str | None
    parameters: tuple[Mapping[str, Any], ...]
    request_schema: Mapping[str, Any] | None
    request_example: Any
    response_statuses: tuple[str, ...]


@dataclass(frozen=True)
class OpenApiDocument:
    """Validated OpenAPI metadata retained without resolving external references."""

    version: str
    server_url: str | None
    operations: tuple[OpenApiOperation, ...]


class _ImportedScenario(ApiScenario):
    """An ``ApiScenario`` carrying non-serialised import comments for its writer."""

    _response_statuses: tuple[tuple[str, ...], ...] = PrivateAttr(default=())


class OpenApiImporter:
    """Import only static OpenAPI 3.0/3.1 request structure.

    This intentionally does not resolve ``$ref``, execute JavaScript, or infer
    assertions. Unsupported structures produce a controlled configuration error.
    """

    def load(self, path: Path) -> OpenApiDocument:
        source = Path(path)
        root = self._read(source)
        if not isinstance(root, Mapping):
            raise self._error(source, "OpenAPI 根节点必须是对象")

        version = root.get("openapi")
        if not isinstance(version, str) or not _OPENAPI_VERSION_RE.fullmatch(version):
            raise self._error(source, "仅支持 OpenAPI 3.0 和 3.1")
        paths = root.get("paths")
        if not isinstance(paths, Mapping):
            raise self._error(source, "paths 必须是对象")

        operations: list[OpenApiOperation] = []
        operation_ids: set[str] = set()
        for path_name, path_item in sorted(paths.items(), key=lambda item: str(item[0])):
            if not isinstance(path_name, str) or not path_name.startswith("/"):
                raise self._error(source, "paths 的键必须是以 / 开头的路径")
            if not isinstance(path_item, Mapping):
                raise self._error(source, "每个 path 项必须是对象")
            if "$ref" in path_item:
                raise self._error(source, "不支持需要解析 $ref 的 path 项")
            path_parameters = self._parameters(path_item.get("parameters", []), source)
            for method, operation in sorted(path_item.items(), key=lambda item: str(item[0])):
                if method not in _METHODS:
                    continue
                if not isinstance(operation, Mapping):
                    raise self._error(source, "HTTP operation 必须是对象")
                if "$ref" in operation:
                    raise self._error(source, "不支持需要解析 $ref 的 HTTP operation")
                operation_id = operation.get("operationId")
                if operation_id is not None and (
                    not isinstance(operation_id, str) or not operation_id.strip()
                ):
                    raise self._error(source, "operationId 必须是非空字符串")
                if operation_id is not None:
                    if operation_id in operation_ids:
                        raise self._error(source, "不允许重复的 operationId")
                    operation_ids.add(operation_id)
                parameters = self._combine_parameters(
                    path_parameters,
                    self._parameters(operation.get("parameters", []), source),
                )
                request_schema, request_example = self._request_body(
                    operation.get("requestBody"), source
                )
                statuses = self._response_statuses(operation.get("responses"), source)
                operations.append(
                    OpenApiOperation(
                        path=path_name,
                        method=method,
                        operation_id=operation_id,
                        parameters=parameters,
                        request_schema=request_schema,
                        request_example=request_example,
                        response_statuses=statuses,
                    )
                )
        if not operations:
            raise self._error(source, "paths 中必须至少包含一个受支持的 HTTP operation")
        return OpenApiDocument(
            version=version,
            server_url=self._server_url(root.get("servers"), source),
            operations=tuple(
                sorted(operations, key=lambda operation: (operation.path, operation.method))
            ),
        )

    def to_scenario(self, document: OpenApiDocument, source_name: str) -> ApiScenario:
        if not isinstance(source_name, str) or not source_name.strip():
            raise ApiConfigError(
                "发生了什么：导入来源名称必须是非空字符串。"
                "在哪里：OpenAPI 导入参数。"
                "怎么处理：提供文件名或其他简短的来源名称后重试。"
            )
        try:
            steps: list[ApiStep] = []
            statuses: list[tuple[str, ...]] = []
            for index, operation in enumerate(document.operations, start=1):
                parameter_values = {
                    (parameter["name"], parameter["in"]): self._parameter_value(parameter)
                    for parameter in operation.parameters
                }
                url = self._request_url(document.server_url, operation.path, parameter_values)
                query = {
                    name: value
                    for (name, location), value in parameter_values.items()
                    if location == "query"
                }
                headers = {
                    name: value
                    for (name, location), value in parameter_values.items()
                    if location == "header"
                }
                json_body = self._body_value(
                    operation.request_schema,
                    operation.request_example,
                    "body",
                )
                default_name = f"{operation.method.upper()} {operation.path}"
                name = _safe_import_name(operation.operation_id, f"operation-{index}")
                if operation.operation_id is None:
                    name = default_name
                steps.append(
                    ApiStep(
                        name=name,
                        request=ApiRequest(
                            method=operation.method.upper(),
                            url=url,
                            headers=headers,
                            query=query,
                            json_body=json_body,
                        ),
                    )
                )
                statuses.append(operation.response_statuses)
            safe_source_name = _safe_import_name(source_name, "openapi-import")
            scenario = _ImportedScenario(
                name=safe_source_name,
                steps=tuple(steps),
                source=safe_source_name,
            )
            scenario._response_statuses = tuple(statuses)
            return scenario
        except (TypeError, ValueError, RecursionError) as error:
            raise ApiConfigError(
                "发生了什么：OpenAPI 导入结构无法安全转换为场景。"
                "在哪里：OpenAPI 导入。"
                "怎么处理：检查静态 schema、示例值和字段类型后重试。"
            ) from error

    def _read(self, source: Path) -> Any:
        try:
            text = read_regular_text(source, max_bytes=MAX_OPENAPI_BYTES)
        except (OSError, UnicodeDecodeError, ValueError) as error:
            raise self._error(source, "OpenAPI 文件无法读取") from error
        try:
            if source.suffix.casefold() == ".json":
                value = json.loads(text, object_pairs_hook=_reject_duplicate_json_keys)
                _validate_json_value(value)
                return value
            if source.suffix.casefold() in {".yaml", ".yml"}:
                _reject_duplicate_yaml_keys(text)
                value = yaml.safe_load(text)
                _validate_json_value(value)
                return value
        except (
            json.JSONDecodeError,
            yaml.YAMLError,
            TypeError,
            ValueError,
            RecursionError,
        ) as error:
            raise self._error(source, "OpenAPI JSON/YAML 无法安全解析") from error
        raise self._error(source, "文件扩展名必须是 .json、.yaml 或 .yml")

    def _parameters(self, raw: Any, source: Path) -> tuple[Mapping[str, Any], ...]:
        if not isinstance(raw, list):
            raise self._error(source, "parameters 必须是数组")
        result: list[Mapping[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for parameter in raw:
            if not isinstance(parameter, Mapping) or "$ref" in parameter:
                raise self._error(source, "parameters 的每项必须是不含 $ref 的对象")
            name = parameter.get("name")
            location = parameter.get("in")
            if (
                not isinstance(name, str)
                or not name.strip()
                or location not in _PARAMETER_LOCATIONS
            ):
                raise self._error(source, "parameter 必须使用 name 和 path/query/header in")
            identity = (name, location)
            if identity in seen:
                raise self._error(source, "不允许重复的 parameter")
            seen.add(identity)
            schema = parameter.get("schema")
            if schema is not None and (not isinstance(schema, Mapping) or "$ref" in schema):
                raise self._error(source, "parameter.schema 必须是不含 $ref 的对象")
            if isinstance(schema, Mapping):
                self._validate_schema(schema, source)
            if "content" in parameter:
                raise self._error(source, "不支持 parameter.content")
            result.append(parameter)
        return tuple(result)

    def _combine_parameters(
        self,
        path_parameters: tuple[Mapping[str, Any], ...],
        operation_parameters: tuple[Mapping[str, Any], ...],
    ) -> tuple[Mapping[str, Any], ...]:
        combined = {
            (parameter["name"], parameter["in"]): parameter for parameter in path_parameters
        }
        combined.update(
            {(parameter["name"], parameter["in"]): parameter for parameter in operation_parameters}
        )
        return tuple(combined[key] for key in sorted(combined))

    def _request_body(self, raw: Any, source: Path) -> tuple[Mapping[str, Any] | None, Any]:
        if raw is None:
            return None, _MISSING
        if not isinstance(raw, Mapping) or "$ref" in raw:
            raise self._error(source, "requestBody 必须是不含 $ref 的对象")
        content = raw.get("content")
        if not isinstance(content, Mapping):
            raise self._error(source, "requestBody.content 必须是对象")
        if not all(
            isinstance(key, str) and isinstance(value, Mapping) for key, value in content.items()
        ):
            raise self._error(
                source, "requestBody.content 的键必须是字符串且 media type 必须是对象"
            )
        media_type = content.get("application/json")
        if media_type is None:
            media_type = next(
                (value for key, value in sorted(content.items()) if key.endswith("+json")),
                None,
            )
        if media_type is None:
            return None, _MISSING
        if not isinstance(media_type, Mapping):
            raise self._error(source, "JSON requestBody media type 必须是对象")
        schema = media_type.get("schema")
        if schema is not None and (not isinstance(schema, Mapping) or "$ref" in schema):
            raise self._error(source, "requestBody schema 必须是不含 $ref 的对象")
        if isinstance(schema, Mapping):
            self._validate_schema(schema, source)
        return schema, media_type.get("example", _MISSING)

    def _validate_schema(self, schema: Mapping[str, Any], source: Path) -> None:
        """Validate the recursively imported schema subset before conversion."""

        if _contains_schema_reference(schema):
            raise self._error(source, "不支持需要解析 $ref 的 schema")
        properties = schema.get("properties")
        if properties is not None:
            if not isinstance(properties, Mapping) or not all(
                isinstance(name, str) and isinstance(value, Mapping)
                for name, value in properties.items()
            ):
                raise self._error(source, "schema.properties 必须是对象且属性 schema 必须是对象")
            for property_schema in properties.values():
                self._validate_schema(property_schema, source)
        items = schema.get("items")
        if items is not None:
            if not isinstance(items, Mapping):
                raise self._error(source, "schema.items 必须是对象")
            self._validate_schema(items, source)

    def _response_statuses(self, raw: Any, source: Path) -> tuple[str, ...]:
        if not isinstance(raw, Mapping) or not raw:
            raise self._error(source, "responses 必须是至少包含一个状态的对象")
        statuses: list[str] = []
        for status, response in raw.items():
            if not isinstance(status, str) or (
                status != "default" and not _RESPONSE_STATUS_RE.fullmatch(status)
            ):
                raise self._error(source, "responses 的状态键无效")
            if not isinstance(response, Mapping) or "$ref" in response:
                raise self._error(source, "response 必须是不含 $ref 的对象")
            statuses.append(status)
        return tuple(sorted(statuses))

    def _server_url(self, raw: Any, source: Path) -> str | None:
        if raw is None:
            return None
        if not isinstance(raw, list):
            raise self._error(source, "servers 必须是数组")
        if not raw:
            return None
        server = raw[0]
        if not isinstance(server, Mapping) or not isinstance(server.get("url"), str):
            raise self._error(source, "servers 的首项必须包含 url 字符串")
        variables = server.get("variables", {})
        if not isinstance(variables, Mapping):
            raise self._error(source, "server.variables 必须是对象")

        def replace(match: re.Match[str]) -> str:
            name = match.group(1)
            variable = variables.get(name)
            if _is_sensitive_field_name(name):
                return _todo(name)
            if isinstance(variable, Mapping) and "default" in variable:
                return _scalar_string(variable["default"], name)
            return _todo(name)

        try:
            parsed = urlsplit(_SERVER_VARIABLE_RE.sub(replace, server["url"]))
            query = urlencode(
                [
                    (name, _todo(name) if _is_sensitive_field_name(name) else value)
                    for name, value in parse_qsl(parsed.query, keep_blank_values=True)
                ],
                quote_via=quote,
                safe="{}",
            )
        except ValueError as error:
            raise self._error(source, "servers.url 无法安全解析") from error
        return urlunsplit((parsed.scheme, parsed.netloc.rsplit("@", 1)[-1], parsed.path, query, ""))

    def _parameter_value(self, parameter: Mapping[str, Any]) -> str:
        name = parameter["name"]
        if _is_sensitive_field_name(name):
            return _todo(name)
        if "example" in parameter:
            return _scalar_string(parameter["example"], name)
        schema = parameter.get("schema")
        if isinstance(schema, Mapping):
            if "example" in schema:
                return _scalar_string(schema["example"], name)
            if "default" in schema:
                return _scalar_string(schema["default"], name)
        return _todo(name)

    def _request_url(
        self,
        server_url: str | None,
        path: str,
        parameter_values: Mapping[tuple[str, str], str],
    ) -> str:
        def replace(match: re.Match[str]) -> str:
            return parameter_values.get((match.group(1), "path"), _todo(match.group(1)))

        base_url = server_url or _todo("base_url")
        request_path = _SERVER_VARIABLE_RE.sub(replace, path)
        parsed = urlsplit(base_url)
        if not parsed.query:
            return f"{base_url.rstrip('/')}{request_path}"
        joined_path = f"{parsed.path.rstrip('/')}/{request_path.lstrip('/')}"
        return urlunsplit((parsed.scheme, parsed.netloc, joined_path, parsed.query, ""))

    def _body_value(self, schema: Mapping[str, Any] | None, example: Any, name: str) -> Any:
        if _is_sensitive_field_name(name):
            return _todo(name)
        if example is not _MISSING:
            return _json_value(example, name)
        if schema is None:
            return None
        if "$ref" in schema:
            raise ValueError("schema reference was not rejected")
        if "example" in schema:
            return _json_value(schema["example"], name)
        if "default" in schema:
            return _json_value(schema["default"], name)
        schema_type = schema.get("type")
        if schema_type == "object" or "properties" in schema:
            properties = schema.get("properties", {})
            if not isinstance(properties, Mapping) or not all(
                isinstance(property_name, str) for property_name in properties
            ):
                raise ValueError("object schema properties are invalid")
            return {
                property_name: self._body_value(
                    property_schema if isinstance(property_schema, Mapping) else None,
                    _MISSING,
                    property_name,
                )
                for property_name, property_schema in sorted(properties.items())
            }
        if schema_type == "array":
            items = schema.get("items")
            if items is None:
                return []
            if not isinstance(items, Mapping):
                raise ValueError("array schema items are invalid")
            return [self._body_value(items, _MISSING, f"{name}_item")]
        return _todo(name)

    def _error(self, source: Path, what: str) -> ApiConfigError:
        return ApiConfigError(
            f"发生了什么：{what}。在哪里：{source}。"
            "怎么处理：检查 OpenAPI 3.0/3.1 的静态 JSON/YAML 结构后重试。"
        )


def write_scenario_templates(
    scenario: ApiScenario, destination: Path, force: bool = False
) -> tuple[Path, ...]:
    statuses = getattr(scenario, "_response_statuses", ())
    source_name = _safe_import_name(scenario.name, "openapi-import")
    files = tuple(
        ScenarioFile(
            filename_stem=_safe_import_name(step.name, f"operation-{index}"),
            scenario=ApiScenario(name=source_name, steps=(step,), source=source_name),
            comments=(
                "# 由 CSBox 从 OpenAPI 导入；请补全 {{TODO_name}} 占位符。",
                *(
                    (f"# 已声明响应状态：{', '.join(statuses[index - 1])}（未自动生成断言）",)
                    if index <= len(statuses) and statuses[index - 1]
                    else ()
                ),
            ),
        )
        for index, step in enumerate(scenario.steps, start=1)
    )
    return write_scenario_files(files, destination, force=force)


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _validate_json_value(value: Any) -> None:
    """Reject non-JSON OpenAPI values before structure-specific traversal."""

    stack: list[tuple[Any, int]] = [(value, 0)]
    nodes = 0
    seen_containers: set[int] = set()
    while stack:
        item, depth = stack.pop()
        nodes += 1
        if nodes > _MAX_OPENAPI_NODES or depth > _MAX_OPENAPI_DEPTH:
            raise ValueError("OpenAPI structure exceeds the safe budget")
        if item is None or isinstance(item, (str, bool, int)):
            continue
        if isinstance(item, float):
            if not math.isfinite(item):
                raise ValueError("OpenAPI values must not contain non-finite numbers")
            continue
        if isinstance(item, (Mapping, list)):
            identity = id(item)
            if identity in seen_containers:
                raise ValueError("OpenAPI aliases are not supported")
            seen_containers.add(identity)
            if isinstance(item, Mapping):
                for key, nested_value in item.items():
                    if not isinstance(key, str):
                        raise ValueError("OpenAPI object keys must be strings")
                    stack.append((nested_value, depth + 1))
            else:
                stack.extend((nested_value, depth + 1) for nested_value in item)
            continue
        raise ValueError("OpenAPI values must be JSON-compatible")


def _contains_schema_reference(value: Any) -> bool:
    if isinstance(value, Mapping):
        return "$ref" in value or any(_contains_schema_reference(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_schema_reference(item) for item in value)
    return False


def _reject_duplicate_yaml_keys(text: str) -> None:
    """Reject duplicate YAML mapping keys before ``safe_load`` can overwrite one."""

    root = yaml.compose(text)
    if root is not None:
        stack: list[tuple[Node, int]] = [(root, 0)]
        seen_nodes: set[int] = set()
        nodes = 0
        while stack:
            node, depth = stack.pop()
            nodes += 1
            if nodes > _MAX_OPENAPI_NODES or depth > _MAX_OPENAPI_DEPTH:
                raise ValueError("YAML structure exceeds the safe budget")
            node_id = id(node)
            if node_id in seen_nodes:
                raise ValueError("YAML aliases are not supported")
            seen_nodes.add(node_id)
            if isinstance(node, MappingNode):
                keys: set[tuple[str, str]] = set()
                for key, nested in node.value:
                    if not isinstance(key, ScalarNode):
                        raise ValueError("YAML mapping keys must be scalars")
                    identity = (key.tag, key.value)
                    if identity in keys:
                        raise ValueError("duplicate YAML mapping key")
                    keys.add(identity)
                    stack.append((key, depth + 1))
                    stack.append((nested, depth + 1))
            elif isinstance(node, SequenceNode):
                stack.extend((nested, depth + 1) for nested in node.value)


def _todo(name: str) -> str:
    normalized = _PLACEHOLDER_NAME_RE.sub("_", name).strip("_") or "value"
    if normalized[0].isdigit():
        normalized = f"value_{normalized}"
    return f"{{{{TODO_{normalized}}}}}"


def _is_sensitive_field_name(name: str) -> bool:
    normalized = "".join(character for character in name.casefold() if character.isalnum())
    return bool(normalized) and any(secret in normalized for secret in _NORMALIZED_SECRET_FIELDS)


def _safe_import_name(name: str | None, fallback: str) -> str:
    """Keep sensitive OpenAPI identifiers out of persistent template metadata."""

    if not isinstance(name, str) or not name.strip() or _is_sensitive_field_name(name):
        return fallback
    if "{{" in name or "}}" in name:
        return fallback
    return name


def _scalar_string(value: Any, name: str) -> str:
    if _is_sensitive_field_name(name):
        return _todo(name)
    if isinstance(value, str):
        return value
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if not math.isfinite(value):
            raise ValueError("OpenAPI example must not contain non-finite numbers")
        return str(value)
    return _todo(name)


def _json_value(value: Any, name: str) -> Any:
    if _is_sensitive_field_name(name):
        return _todo(name)
    if value is None:
        return _todo(name)
    if isinstance(value, (str, int, float, bool)):
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError("OpenAPI example must not contain non-finite numbers")
        return value
    if isinstance(value, list):
        return [_json_value(item, f"{name}_item") for item in value]
    if isinstance(value, Mapping) and all(isinstance(key, str) for key in value):
        return {key: _json_value(item, key) for key, item in sorted(value.items())}
    raise ValueError("OpenAPI example must contain JSON values")


_MISSING = object()


__all__ = ["OpenApiDocument", "OpenApiImporter", "OpenApiOperation", "write_scenario_templates"]
