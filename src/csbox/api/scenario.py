from __future__ import annotations

import json
import re
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from csbox.api.errors import ApiConfigError
from csbox.api.models import ApiAssertion, ApiMultipartPart, ApiRequest, ApiScenario, ApiStep
from csbox.api.variables import validate_placeholders

_ROOT_KEYS = frozenset({"name", "variables", "steps"})
_STEP_KEYS = frozenset(
    {
        "name",
        "method",
        "url",
        "headers",
        "query",
        "bearer",
        "timeout",
        "follow_redirects",
        "verify_tls",
        "json",
        "form",
        "multipart",
        "assertions",
    }
)
_ASSERTION_KEYS = frozenset({"type", "expected", "path", "operator"})
_ASSERTION_TYPES = frozenset({"status", "header", "json_path", "body"})
_MULTIPART_KEYS = frozenset({"name", "value"})
_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"})
_TOML_LOCATION_RE = re.compile(r"line (?P<line>\d+)(?:, column (?P<column>\d+))?")


class ScenarioLoader:
    """Load static TOML with status/header/json_path/body assertions only."""

    def load(self, path: Path) -> ApiScenario:
        source = Path(path)
        data = self._read(source)
        self._reject_unknown_keys(data, _ROOT_KEYS, source, None)
        name = self._required_string(data, "name", source, None)
        variables = self._string_mapping(data.get("variables", {}), "variables", source, None)
        steps_data = data.get("steps")
        if not isinstance(steps_data, list) or not steps_data:
            raise self._error(source, None, "steps 必须是至少包含一个步骤的数组")
        steps = tuple(
            self._step(step_data, source, index) for index, step_data in enumerate(steps_data)
        )
        try:
            return ApiScenario(name=name, steps=steps, variables=variables, source=str(source))
        except ValidationError as error:
            raise self._error(source, None, "场景结构不符合 API 模型") from error

    def _read(self, path: Path) -> dict[str, Any]:
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
            location = _TOML_LOCATION_RE.search(str(error))
            detail = "TOML 场景文件无法解析"
            if location is not None:
                detail += f"（第 {location.group('line')} 行）"
            raise self._error(path, None, detail) from error
        if not isinstance(data, dict):
            raise self._error(path, None, "TOML 根节点必须是表")
        return data

    def _step(self, raw_step: Any, path: Path, index: int) -> ApiStep:
        step_location = f"steps[{index}]"
        if not isinstance(raw_step, Mapping):
            raise self._error(path, step_location, "steps 的每项必须是表")
        step_name = self._required_string(raw_step, "name", path, step_location)
        self._reject_unknown_keys(raw_step, _STEP_KEYS, path, step_location)
        method = self._required_string(raw_step, "method", path, step_location)
        if method not in _METHODS:
            raise self._error(path, step_location, "method 必须是受支持的 HTTP 方法")
        url = self._required_string(raw_step, "url", path, step_location)
        headers = self._string_mapping(raw_step.get("headers", {}), "headers", path, step_location)
        query = self._string_mapping(raw_step.get("query", {}), "query", path, step_location)
        bearer = raw_step.get("bearer")
        if bearer is not None:
            if not isinstance(bearer, str):
                raise self._error(path, step_location, "bearer 必须是字符串")
            if any(key.casefold() == "authorization" for key in headers):
                raise self._error(
                    path, step_location, "bearer 不能与 headers.Authorization 同时声明"
                )
            self._validate_static(bearer, path, step_location)
            headers = {**headers, "Authorization": f"Bearer {bearer}"}
        body_keys = [key for key in ("json", "form", "multipart") if key in raw_step]
        if len(body_keys) > 1:
            raise self._error(path, step_location, "json、form 与 multipart 只能声明其中一种 body")
        json_body = self._json_value(raw_step.get("json"), path, step_location)
        form = self._string_mapping(raw_step.get("form", {}), "form", path, step_location)
        multipart = self._multipart(raw_step.get("multipart", []), path, step_location)
        timeout = raw_step.get("timeout")
        if timeout is not None:
            if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
                raise self._error(path, step_location, "timeout 必须是大于零的数值")
            timeout = float(timeout)
        follow_redirects = self._bool(
            raw_step.get("follow_redirects", False), "follow_redirects", path, step_location
        )
        verify_tls = self._bool(raw_step.get("verify_tls", True), "verify_tls", path, step_location)
        assertions = self._assertions(raw_step.get("assertions", []), path, step_location)
        try:
            request = ApiRequest(
                method=method,
                url=url,
                headers=headers,
                query=query,
                json_body=json_body,
                form=form,
                multipart=multipart,
                timeout_seconds=timeout,
                follow_redirects=follow_redirects,
                verify_tls=verify_tls,
            )
            return ApiStep(name=step_name, request=request, assertions=assertions)
        except ValidationError as error:
            raise self._error(path, step_location, "步骤结构不符合 API 模型") from error

    def _assertions(self, raw: Any, path: Path, step_location: str) -> tuple[ApiAssertion, ...]:
        if not isinstance(raw, list):
            raise self._error(path, step_location, "assertions 必须是数组")
        assertions: list[ApiAssertion] = []
        seen: set[str] = set()
        for item in raw:
            if not isinstance(item, Mapping):
                raise self._error(path, step_location, "assertions 的每项必须是表")
            self._reject_unknown_keys(item, _ASSERTION_KEYS, path, step_location)
            kind = self._required_string(item, "type", path, step_location)
            if kind not in _ASSERTION_TYPES:
                raise self._error(path, step_location, "assertions.type 必须是受支持的断言类型")
            location = item.get("path")
            operator = item.get("operator")
            if location is not None and not isinstance(location, str):
                raise self._error(path, step_location, "assertions.path 必须是字符串")
            if operator is not None and not isinstance(operator, str):
                raise self._error(path, step_location, "assertions.operator 必须是字符串")
            expected = self._json_value(item.get("expected"), path, step_location)
            try:
                assertion = ApiAssertion(
                    kind=kind,
                    expected=expected,
                    location=location,
                    operator=operator,
                )
            except ValidationError as error:
                raise self._error(path, step_location, "断言结构不符合 API 模型") from error
            identity = json.dumps(
                assertion.model_dump(mode="json"), ensure_ascii=False, sort_keys=True
            )
            if identity in seen:
                raise self._error(path, step_location, "不允许重复的 assertions")
            seen.add(identity)
            assertions.append(assertion)
        return tuple(assertions)

    def _multipart(self, raw: Any, path: Path, step_location: str) -> tuple[ApiMultipartPart, ...]:
        if not isinstance(raw, list):
            raise self._error(path, step_location, "multipart 必须是数组")
        parts: list[ApiMultipartPart] = []
        for item in raw:
            if not isinstance(item, Mapping):
                raise self._error(path, step_location, "multipart 的每项必须是表")
            self._reject_unknown_keys(item, _MULTIPART_KEYS, path, step_location)
            parts.append(
                ApiMultipartPart(
                    name=self._required_string(item, "name", path, step_location),
                    value=self._required_string(item, "value", path, step_location),
                )
            )
        return tuple(parts)

    def _json_value(self, value: Any, path: Path, step_location: str) -> Any:
        if value is None:
            return None
        if isinstance(value, (str, int, float, bool)):
            self._validate_static(value, path, step_location)
            return value
        if isinstance(value, list):
            return [self._json_value(item, path, step_location) for item in value]
        if isinstance(value, Mapping):
            if not all(isinstance(key, str) for key in value):
                raise self._error(path, step_location, "JSON 对象键必须是字符串")
            return {key: self._json_value(item, path, step_location) for key, item in value.items()}
        raise self._error(path, step_location, "json/expected 只能包含 JSON 值")

    def _string_mapping(
        self, raw: Any, field_name: str, path: Path, step_location: str | None
    ) -> dict[str, str]:
        if not isinstance(raw, Mapping):
            raise self._error(path, step_location, f"{field_name} 必须是键值表")
        result: dict[str, str] = {}
        for key, value in raw.items():
            if not isinstance(key, str) or not isinstance(value, str):
                raise self._error(path, step_location, f"{field_name} 的键和值必须是字符串")
            self._validate_static(value, path, step_location)
            result[key] = value
        return result

    def _required_string(
        self, raw: Mapping[str, Any], key: str, path: Path, step_location: str | None
    ) -> str:
        value = raw.get(key)
        if not isinstance(value, str) or not value.strip():
            raise self._error(path, step_location, f"{key} 必须是非空字符串")
        self._validate_static(value, path, step_location)
        return value

    def _validate_static(self, value: Any, path: Path, step_location: str | None) -> None:
        try:
            validate_placeholders(value)
        except ApiConfigError as error:
            raise self._error(path, step_location, "包含不安全或不受支持的变量占位符") from error

    def _bool(self, value: Any, field_name: str, path: Path, step_location: str) -> bool:
        if not isinstance(value, bool):
            raise self._error(path, step_location, f"{field_name} 必须是 true 或 false")
        return value

    def _reject_unknown_keys(
        self,
        raw: Mapping[str, Any],
        allowed: frozenset[str],
        path: Path,
        step_location: str | None,
    ) -> None:
        unknown = sorted(set(raw) - allowed)
        if unknown:
            raise self._error(path, step_location, "包含不支持或脚本式的配置键")

    def _error(self, path: Path, step_location: str | None, what: str) -> ApiConfigError:
        where = str(path) if step_location is None else f"{path} 的 {step_location}"
        return ApiConfigError(
            f"发生了什么：{what}。在哪里：{where}。"
            "怎么处理：检查 TOML 键、类型和静态 {{identifier}} 占位符后重试。"
        )


__all__ = ["ScenarioLoader"]
