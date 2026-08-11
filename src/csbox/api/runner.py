from __future__ import annotations

import json
import re
import time
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qsl, quote, quote_plus, urlsplit
from uuid import uuid4

from pydantic import ValidationError

from csbox.api.assertions import evaluate_assertions, redacted_assertion, skipped_assertion
from csbox.api.errors import ApiConfigError, ApiTransportError
from csbox.api.models import (
    ApiMultipartPart,
    ApiRequest,
    ApiRun,
    ApiRunResult,
    ApiRunStatus,
    ApiScenario,
    ApiStep,
)
from csbox.api.redaction import RedactionPolicy, Redactor
from csbox.api.transport import ApiTransport
from csbox.api.variables import interpolate

_GENERIC_CONFIG_ERROR = (
    "发生了什么：API 请求变量或字段配置无效。"
    "在哪里：场景步骤。"
    "怎么处理：检查静态 {{identifier}} 变量和值类型后重试。"
)
_GENERIC_RUNTIME_ERROR = "接口请求发生未预期错误，请稍后重试。"


class ApiRunner:
    """Resolve and execute scenario steps through the transport port in file order."""

    def __init__(self, transport: ApiTransport, redactor: Redactor) -> None:
        self._transport = transport
        self._redactor = redactor

    async def run(
        self,
        scenario: ApiScenario,
        variables: Mapping[str, str],
        fail_fast: bool = False,
    ) -> ApiRun:
        started_at = datetime.now(UTC)
        started_clock = time.perf_counter()
        merged_variables = {**scenario.variables, **dict(variables)}
        redactor = _run_redactor(self._redactor, merged_variables)
        safe_steps = [_redacted_step(step, redactor) for step in scenario.steps]
        results: list[ApiRunResult] = []

        for index, step in enumerate(scenario.steps):
            step_started = time.perf_counter()
            try:
                request = _resolved_request(step.request, merged_variables)
            except ApiConfigError as error:
                result = self._error_result(
                    step,
                    status="CONFIG_ERROR",
                    error=redactor.text(error.user_message),
                    reason="请求配置无效，未执行断言",
                    started_clock=step_started,
                    redactor=redactor,
                )
            except (TypeError, ValueError, ValidationError):
                result = self._error_result(
                    step,
                    status="CONFIG_ERROR",
                    error=_GENERIC_CONFIG_ERROR,
                    reason="请求配置无效，未执行断言",
                    started_clock=step_started,
                    redactor=redactor,
                )
            else:
                step_redactor = _request_redactor(redactor, request)
                safe_steps[index] = _redacted_step(
                    step.model_copy(update={"request": request}), step_redactor
                )
                result = await self._execute_step(step, request, step_started, step_redactor)

            results.append(result)
            if fail_fast and result.status != "PASS":
                break

        ended_at = datetime.now(UTC)
        status = _aggregate_status(results)
        safe_scenario = ApiScenario(
            name=redactor.text(scenario.name),
            steps=tuple(safe_steps),
            variables=_redacted_string_mapping(scenario.variables, redactor),
            source=None if scenario.source is None else redactor.text(scenario.source),
        )
        return ApiRun(
            id=_new_run_id(started_at),
            scenario=safe_scenario,
            started_at=started_at,
            status=status,
            results=tuple(results),
            ended_at=ended_at,
            elapsed_ms=_elapsed_ms(started_clock),
        )

    async def _execute_step(
        self,
        step: ApiStep,
        request: ApiRequest,
        started_clock: float,
        redactor: Redactor,
    ) -> ApiRunResult:
        try:
            response = await self._transport.send(request)
        except ApiTransportError as error:
            return self._error_result(
                step,
                status="RUNTIME_ERROR",
                error=redactor.text(error.user_message),
                reason="未收到响应",
                started_clock=started_clock,
                redactor=redactor,
            )
        except Exception:
            return self._error_result(
                step,
                status="RUNTIME_ERROR",
                error=_GENERIC_RUNTIME_ERROR,
                reason="未收到响应",
                started_clock=started_clock,
                redactor=redactor,
            )

        assertion_results = evaluate_assertions(
            step.assertions, response.assertion_view(), redactor
        )
        status: ApiRunStatus = (
            "PASS" if all(result.status == "PASS" for result in assertion_results) else "FAIL"
        )
        return ApiRunResult(
            step_name=redactor.text(step.name),
            status=status,
            response=response.redacted_copy(redactor),
            assertions=assertion_results,
            elapsed_ms=_elapsed_ms(started_clock),
        )

    def _error_result(
        self,
        step: ApiStep,
        *,
        status: ApiRunStatus,
        error: str,
        reason: str,
        started_clock: float,
        redactor: Redactor,
    ) -> ApiRunResult:
        assertion_results = tuple(
            skipped_assertion(assertion, redactor, reason=reason) for assertion in step.assertions
        )
        return ApiRunResult(
            step_name=redactor.text(step.name),
            status=status,
            assertions=assertion_results,
            error=redactor.text(error),
            elapsed_ms=_elapsed_ms(started_clock),
        )


def _resolved_request(request: ApiRequest, variables: Mapping[str, str]) -> ApiRequest:
    payload = interpolate(request.model_dump(mode="python"), variables)
    return ApiRequest.model_validate(payload)


def _redacted_step(step: ApiStep, redactor: Redactor) -> ApiStep:
    return ApiStep(
        name=redactor.text(step.name),
        request=_redacted_request(step.request, redactor),
        assertions=tuple(redacted_assertion(assertion, redactor) for assertion in step.assertions),
    )


def _redacted_request(request: ApiRequest, redactor: Redactor) -> ApiRequest:
    json_body = redactor.json_value(request.json_body)
    multipart = tuple(
        ApiMultipartPart(
            name=redactor.text(part.name),
            value=redactor.text(part.value),
        )
        for part in request.multipart
    )
    return ApiRequest(
        method=request.method,
        url=redactor.url(request.url),
        headers=redactor.headers(request.headers),
        query=_redacted_string_mapping(request.query, redactor),
        json_body=json_body,
        body=None if request.body is None else redactor.text(request.body),
        form=_redacted_string_mapping(request.form, redactor),
        multipart=multipart,
        timeout_seconds=request.timeout_seconds,
        follow_redirects=request.follow_redirects,
        verify_tls=request.verify_tls,
    )


def _redacted_string_mapping(values: Mapping[str, str], redactor: Redactor) -> dict[str, str]:
    redacted = redactor.json_value(dict(values))
    if not isinstance(redacted, dict):
        return {}
    return {str(name): str(value) for name, value in redacted.items()}


def _run_redactor(base: Redactor, variables: Mapping[str, object]) -> Redactor:
    normalized_secret_fields = {_normalize_field_name(field) for field in base.policy.secret_fields}
    variable_secrets = tuple(
        value
        for name, value in variables.items()
        if isinstance(value, str)
        and value
        and _normalize_field_name(name) in normalized_secret_fields
    )
    return Redactor(
        RedactionPolicy(
            secret_fields=base.policy.secret_fields,
            secret_values=(*base.policy.secret_values, *variable_secrets),
        )
    )


def _request_redactor(base: Redactor, request: ApiRequest) -> Redactor:
    normalized_secret_fields = {_normalize_field_name(field) for field in base.policy.secret_fields}
    values: list[str] = []
    try:
        parsed_url = urlsplit(request.url)
        _append_secret(values, parsed_url.username)
        _append_secret(values, parsed_url.password)
        _collect_named_values(
            parse_qsl(parsed_url.query, keep_blank_values=True),
            normalized_secret_fields,
            values,
        )
    except ValueError:
        pass

    for name, value in request.headers.items():
        if _normalize_field_name(name) not in normalized_secret_fields:
            continue
        _append_secret(values, value)
        normalized_name = _normalize_field_name(name)
        if normalized_name in {
            _normalize_field_name("authorization"),
            _normalize_field_name("proxy-authorization"),
        }:
            _append_secret(values, value.partition(" ")[2])
        if normalized_name == _normalize_field_name("cookie"):
            for cookie in value.split(";"):
                _append_secret(values, cookie.partition("=")[2].strip())

    _collect_named_values(request.query.items(), normalized_secret_fields, values)
    _collect_json_secret_values(request.json_body, normalized_secret_fields, values)
    _collect_named_values(request.form.items(), normalized_secret_fields, values)
    _collect_named_values(
        ((part.name, part.value) for part in request.multipart),
        normalized_secret_fields,
        values,
    )
    if request.body:
        _collect_body_secret_values(request.body, normalized_secret_fields, values)

    return Redactor(
        RedactionPolicy(
            secret_fields=base.policy.secret_fields,
            secret_values=(*base.policy.secret_values, *values),
        )
    )


def _collect_named_values(
    items: Iterable[tuple[object, Any]],
    normalized_secret_fields: set[str],
    values: list[str],
) -> None:
    for name, value in items:
        if _normalize_field_name(str(name)) in normalized_secret_fields:
            _collect_scalar_strings(value, values)


def _collect_json_secret_values(
    value: Any,
    normalized_secret_fields: set[str],
    values: list[str],
    *,
    sensitive: bool = False,
) -> None:
    if isinstance(value, Mapping):
        for name, nested_value in value.items():
            _collect_json_secret_values(
                nested_value,
                normalized_secret_fields,
                values,
                sensitive=sensitive or _normalize_field_name(str(name)) in normalized_secret_fields,
            )
    elif isinstance(value, (list, tuple)):
        for nested_value in value:
            _collect_json_secret_values(
                nested_value,
                normalized_secret_fields,
                values,
                sensitive=sensitive,
            )
    elif sensitive:
        _collect_scalar_strings(value, values)


def _collect_body_secret_values(
    body: str,
    normalized_secret_fields: set[str],
    values: list[str],
) -> None:
    try:
        parsed_body = json.loads(body)
    except (json.JSONDecodeError, ValueError):
        parsed_body = None
    if parsed_body is not None:
        _collect_json_secret_values(parsed_body, normalized_secret_fields, values)
    _collect_named_values(
        parse_qsl(body, keep_blank_values=True),
        normalized_secret_fields,
        values,
    )


def _collect_scalar_strings(value: Any, values: list[str]) -> None:
    if isinstance(value, str):
        _append_secret(values, value)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _collect_scalar_strings(item, values)


def _append_secret(values: list[str], value: str | None) -> None:
    if not value:
        return
    try:
        quoted = quote(value, safe="")
        form_quoted = quote_plus(value, safe="")
        variants = (
            value,
            quoted,
            _lower_percent_escape_hex(quoted),
            form_quoted,
            _lower_percent_escape_hex(form_quoted),
        )
    except (UnicodeError, ValueError):
        variants = (value,)
    values.extend(dict.fromkeys(variants))


def _lower_percent_escape_hex(value: str) -> str:
    return re.sub(r"%[0-9A-F]{2}", lambda match: match.group(0).lower(), value)


def _normalize_field_name(name: str) -> str:
    return "".join(character for character in name.casefold() if character.isalnum())


def _aggregate_status(results: list[ApiRunResult]) -> ApiRunStatus:
    statuses = {result.status for result in results}
    if "RUNTIME_ERROR" in statuses:
        return "RUNTIME_ERROR"
    if "CONFIG_ERROR" in statuses:
        return "CONFIG_ERROR"
    if "FAIL" in statuses:
        return "FAIL"
    return "PASS"


def _new_run_id(started_at: datetime) -> str:
    return f"{started_at:%Y%m%dT%H%M%S}-{uuid4().hex[:12]}"


def _elapsed_ms(started_clock: float) -> float:
    return max(0.0, (time.perf_counter() - started_clock) * 1000)


__all__ = ["ApiRunner"]
