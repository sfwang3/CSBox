from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from jsonpath_ng import parse
from jsonpath_ng.jsonpath import Fields

from csbox.api.models import ApiAssertion, ApiAssertionResult, ApiResponse
from csbox.api.redaction import REDACTION_MARKER, Redactor

_STATUS_CLASS_RE = re.compile(r"^(?P<class>[1-5])xx$", re.IGNORECASE)
_PASS_LABEL = "通过"
_FAIL_LABEL = "失败"
_SKIP_LABEL = "跳过"


@dataclass(slots=True)
class _EvaluationContext:
    response: ApiResponse
    redactor: Redactor
    _json_loaded: bool = field(default=False, init=False)
    _json_value: Any = field(default=None, init=False)
    _json_valid: bool = field(default=False, init=False)

    def evaluate(self, assertion: ApiAssertion) -> ApiAssertionResult:
        try:
            if assertion.kind == "status":
                return self._status(assertion)
            if assertion.kind == "header":
                return self._header(assertion)
            if assertion.kind == "json_path":
                return self._json_path(assertion)
            if assertion.kind == "body":
                return self._body(assertion)
        except Exception:
            return self._result(
                assertion,
                "FAIL",
                reason="断言配置无法安全执行",
                actual="配置错误",
                location=assertion.location or assertion.kind or "assertion",
            )
        return self._result(
            assertion,
            "FAIL",
            reason="断言配置不受支持",
            actual="配置错误",
            location=assertion.location or "assertion",
        )

    def _status(self, assertion: ApiAssertion) -> ApiAssertionResult:
        expected = assertion.expected
        operator = assertion.operator
        actual = self.response.status_code
        location = assertion.location or "status"

        if _is_int(expected) and operator in {None, "equal", "equals"}:
            passed = actual == expected
            return self._comparison(assertion, passed, actual=actual, location=location)

        status_class = _status_class(expected)
        if status_class is not None and operator in {None, "class", "range"}:
            passed = status_class * 100 <= actual <= status_class * 100 + 99
            return self._comparison(assertion, passed, actual=actual, location=location)

        if operator in {None, "range"} and _is_status_range(expected):
            lower, upper = expected
            passed = lower <= actual <= upper
            return self._comparison(assertion, passed, actual=actual, location=location)

        return self._configuration_failure(assertion, actual=actual, location=location)

    def _header(self, assertion: ApiAssertion) -> ApiAssertionResult:
        location = assertion.location
        if not isinstance(location, str) or not location:
            return self._configuration_failure(assertion, actual="配置错误", location="header")

        found, value = _header(self.response.headers, location)
        operator = assertion.operator or ("exists" if assertion.expected is None else "equals")
        if operator == "exists":
            return self._comparison(
                assertion,
                found,
                actual="存在" if found else "不存在",
                location=location,
            )
        if operator in {"equal", "equals"} and isinstance(assertion.expected, str):
            safe_value = (
                _redacted_header_value(location, value, self.redactor) if found else "不存在"
            )
            return self._comparison(
                assertion,
                found and value == assertion.expected,
                actual=safe_value,
                location=location,
            )
        return self._configuration_failure(
            assertion,
            actual=value if found else "不存在",
            location=location,
        )

    def _json_path(self, assertion: ApiAssertion) -> ApiAssertionResult:
        location = assertion.location
        if not isinstance(location, str) or not location:
            return self._configuration_failure(assertion, actual="配置错误", location="json_path")
        if not self._load_json():
            return self._result(
                assertion,
                "SKIP",
                reason="响应不是有效 JSON，无法执行 JSONPath 断言",
                actual="无效 JSON",
                location=location,
            )

        try:
            expression = parse(location)
            raw_matches = expression.find(self._json_value)
            matches = [match.value for match in raw_matches]
            safe_matches = [
                _redacted_json_path_value(match, self.redactor) for match in raw_matches
            ]
        except Exception:
            return self._result(
                assertion,
                "SKIP",
                reason="JSONPath 无效，无法执行断言",
                actual="无效 JSONPath",
                location=location,
            )

        operator = assertion.operator or "equals"
        if operator == "exists":
            return self._comparison(
                assertion,
                bool(matches),
                actual=f"匹配到 {len(matches)} 项" if matches else "未找到",
                location=location,
            )
        if operator in {"equal", "equals"}:
            if len(matches) != 1:
                return self._comparison(
                    assertion,
                    False,
                    actual=f"匹配到 {len(matches)} 项" if matches else "未找到",
                    location=location,
                )
            actual = matches[0]
            safe_actual = safe_matches[0]
            return self._comparison(
                assertion,
                _json_equal(actual, assertion.expected),
                actual=safe_actual,
                location=location,
            )
        if operator == "type":
            if len(matches) != 1:
                return self._comparison(
                    assertion,
                    False,
                    actual=f"匹配到 {len(matches)} 项" if matches else "未找到",
                    location=location,
                )
            actual_type = _json_type(matches[0])
            expected_type = _expected_json_type(assertion.expected)
            if expected_type is None:
                return self._configuration_failure(
                    assertion,
                    actual=actual_type,
                    location=location,
                )
            return self._comparison(
                assertion,
                actual_type == expected_type,
                actual=actual_type,
                location=location,
            )
        return self._configuration_failure(
            assertion,
            actual=f"匹配到 {len(matches)} 项" if matches else "未找到",
            location=location,
        )

    def _body(self, assertion: ApiAssertion) -> ApiAssertionResult:
        location = assertion.location or "body"
        expected = assertion.expected
        if not isinstance(expected, str):
            return self._configuration_failure(assertion, actual="配置错误", location=location)
        operator = assertion.operator or "contains"
        if operator == "contains":
            passed = expected in self.response.body
        elif operator in {"not_contains", "not-contains"}:
            passed = expected not in self.response.body
        else:
            return self._configuration_failure(assertion, actual="配置错误", location=location)
        return self._comparison(
            assertion,
            passed,
            actual="包含" if expected in self.response.body else "不包含",
            location=location,
        )

    def _load_json(self) -> bool:
        if not self._json_loaded:
            self._json_loaded = True
            try:
                self._json_value = json.loads(
                    self.response.body,
                    parse_constant=_reject_json_constant,
                )
            except (json.JSONDecodeError, TypeError, ValueError):
                self._json_valid = False
            else:
                self._json_valid = True
        return self._json_valid

    def _comparison(
        self,
        assertion: ApiAssertion,
        passed: bool,
        *,
        actual: Any,
        location: str,
    ) -> ApiAssertionResult:
        return self._result(
            assertion,
            "PASS" if passed else "FAIL",
            reason="断言符合预期" if passed else "断言不符合预期",
            actual=actual,
            location=location,
        )

    def _configuration_failure(
        self,
        assertion: ApiAssertion,
        *,
        actual: Any,
        location: str,
    ) -> ApiAssertionResult:
        return self._result(
            assertion,
            "FAIL",
            reason="断言配置不受支持",
            actual=actual,
            location=location,
        )

    def _result(
        self,
        assertion: ApiAssertion,
        status: str,
        *,
        reason: str,
        actual: Any,
        location: str,
    ) -> ApiAssertionResult:
        safe_assertion = redacted_assertion(assertion, self.redactor)
        safe_actual = self.redactor.json_value(actual)
        safe_location = self.redactor.text(location)
        label = {"PASS": _PASS_LABEL, "FAIL": _FAIL_LABEL, "SKIP": _SKIP_LABEL}[status]
        message = (
            f"{label}：{reason}；预期={_format_value(safe_assertion.expected)}；"
            f"实际={_format_value(safe_actual)}；位置={safe_location}"
        )
        return ApiAssertionResult(
            assertion=safe_assertion,
            status=status,
            message=self.redactor.text(message),
            actual=safe_actual,
        )


def evaluate_assertion(
    assertion: ApiAssertion,
    response: ApiResponse,
    redactor: Redactor,
) -> ApiAssertionResult:
    """Evaluate one assertion and return a redacted result instead of raising."""

    return _EvaluationContext(response=response, redactor=redactor).evaluate(assertion)


def evaluate_assertions(
    assertions: Sequence[ApiAssertion],
    response: ApiResponse,
    redactor: Redactor,
) -> tuple[ApiAssertionResult, ...]:
    """Evaluate assertions while decoding a JSON response at most once."""

    context = _EvaluationContext(response=response, redactor=redactor)
    return tuple(context.evaluate(assertion) for assertion in assertions)


def skipped_assertion(
    assertion: ApiAssertion,
    redactor: Redactor,
    *,
    reason: str,
) -> ApiAssertionResult:
    """Build a redacted SKIP result when no response can be evaluated."""

    safe_assertion = redacted_assertion(assertion, redactor)
    location = redactor.text(assertion.location or assertion.kind or "assertion")
    safe_reason = redactor.text(reason)
    message = (
        f"{_SKIP_LABEL}：{safe_reason}；预期={_format_value(safe_assertion.expected)}；"
        f"实际=未收到响应；位置={location}"
    )
    return ApiAssertionResult(
        assertion=safe_assertion,
        status="SKIP",
        message=redactor.text(message),
        actual="未收到响应",
    )


def redacted_assertion(assertion: ApiAssertion, redactor: Redactor) -> ApiAssertion:
    expected = assertion.expected
    if assertion.kind == "header" and assertion.location and isinstance(expected, str):
        expected = _redacted_header_value(assertion.location, expected, redactor)
    elif assertion.kind == "json_path" and assertion.location:
        expected = _redacted_json_path_expected(assertion.location, expected, redactor)
    return ApiAssertion(
        kind=redactor.text(assertion.kind),
        expected=redactor.json_value(expected),
        location=None if assertion.location is None else redactor.text(assertion.location),
        operator=None if assertion.operator is None else redactor.text(assertion.operator),
    )


def _header(headers: dict[str, str], name: str) -> tuple[bool, str]:
    for header_name, value in headers.items():
        if header_name.casefold() == name.casefold():
            return True, value
    return False, ""


def _redacted_header_value(name: str, value: str, redactor: Redactor) -> str:
    redacted = redactor.headers({name: value})
    return next(iter(redacted.values()))


def _redacted_json_path_value(match: Any, redactor: Redactor) -> Any:
    context = match
    while context is not None:
        path = context.path
        if isinstance(path, Fields) and any(
            redactor.is_sensitive_field_name(field_name) for field_name in path.fields
        ):
            return REDACTION_MARKER
        context = context.context
    return redactor.json_value(match.value)


def _redacted_json_path_expected(location: str, value: Any, redactor: Redactor) -> Any:
    try:
        expression = parse(location)
    except Exception:
        return redactor.json_value(value)
    if _json_path_has_sensitive_field(expression, redactor):
        return REDACTION_MARKER
    return redactor.json_value(value)


def _json_path_has_sensitive_field(expression: Any, redactor: Redactor) -> bool:
    if isinstance(expression, Fields):
        return any(redactor.is_sensitive_field_name(field_name) for field_name in expression.fields)
    return any(
        _json_path_has_sensitive_field(child, redactor)
        for child in (
            getattr(expression, "left", None),
            getattr(expression, "right", None),
            getattr(expression, "child", None),
        )
        if child is not None
    )


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_status_range(value: Any) -> bool:
    return (
        isinstance(value, list)
        and len(value) == 2
        and all(_is_int(item) for item in value)
        and 100 <= value[0] <= value[1] <= 599
    )


def _status_class(value: Any) -> int | None:
    if not isinstance(value, str):
        return None
    match = _STATUS_CLASS_RE.fullmatch(value)
    return None if match is None else int(match.group("class"))


def _json_equal(actual: Any, expected: Any) -> bool:
    if isinstance(actual, bool) or isinstance(expected, bool):
        return type(actual) is type(expected) and actual == expected
    return actual == expected


def _json_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return "unknown"


def _expected_json_type(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    aliases = {
        "bool": "boolean",
        "boolean": "boolean",
        "int": "integer",
        "integer": "integer",
        "float": "number",
        "number": "number",
        "str": "string",
        "string": "string",
        "list": "array",
        "array": "array",
        "dict": "object",
        "object": "object",
        "none": "null",
        "null": "null",
    }
    return aliases.get(value.casefold())


def _format_value(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return value
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    return str(value)


def _reject_json_constant(value: str) -> None:
    del value
    raise ValueError("non-standard JSON constant")


__all__ = [
    "evaluate_assertion",
    "evaluate_assertions",
    "redacted_assertion",
    "skipped_assertion",
]
