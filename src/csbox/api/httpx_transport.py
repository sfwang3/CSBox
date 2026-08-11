from __future__ import annotations

import json
import math
import re
import socket
import ssl
import time
from collections.abc import AsyncIterator, Callable, Iterable, Iterator, Mapping
from datetime import timedelta
from typing import Any
from urllib.parse import parse_qsl, quote, quote_plus, urlsplit

import httpx

from csbox.api.errors import ApiTransportError
from csbox.api.models import ApiRequest, ApiResponse
from csbox.api.redaction import RedactionPolicy, Redactor

_DEFAULT_RESPONSE_MAX_BYTES = 262144
_DEFAULT_TIMEOUT_SECONDS = 10.0


class HttpxTransport:
    """HTTPX adapter that keeps transport details outside the domain model."""

    def __init__(
        self,
        client_factory: Callable[..., httpx.AsyncClient] | None = None,
        *,
        response_max_bytes: int = _DEFAULT_RESPONSE_MAX_BYTES,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
        redactor: Redactor | None = None,
    ) -> None:
        if (
            isinstance(response_max_bytes, bool)
            or not isinstance(response_max_bytes, int)
            or response_max_bytes < 1
        ):
            raise ValueError("response_max_bytes 必须是正整数")
        timeout_seconds = _positive_finite_timeout(timeout_seconds)
        self._client_factory = client_factory or httpx.AsyncClient
        self._response_max_bytes = response_max_bytes
        self._timeout_seconds = timeout_seconds
        self._redactor = redactor

    async def send(self, request: ApiRequest) -> ApiResponse:
        """Send a request through a short-lived client and safely stream its response."""

        started_at = time.perf_counter()
        redactor = self._redactor_for_request(request)
        try:
            client = self._client_factory(
                timeout=self._timeout(request.timeout_seconds),
                verify=request.verify_tls,
                follow_redirects=request.follow_redirects,
            )
            async with client, client.stream(**self._request_arguments(request)) as response:
                body_bytes, truncated = await self._read_response_body(response)
                headers = dict(response.headers)
                response_url = str(response.url)
                status_code = response.status_code
                content_type = _header_value(headers, "content-type")
                content_length = _content_length(headers)
                encoding = getattr(response, "encoding", None) or "utf-8"
                body = body_bytes.decode(encoding, errors="strict")
            elapsed_ms = _elapsed_ms(response, started_at)
            response_size, response_size_exact = _response_size(
                len(body_bytes), truncated, content_length
            )
            raw_response = ApiResponse(
                status_code=status_code,
                headers=headers,
                body=body,
                url=response_url,
                elapsed_ms=elapsed_ms,
                truncated=truncated,
                content_type=content_type,
                response_size=response_size,
                response_size_exact=response_size_exact,
            )
            public_response = raw_response.redacted_copy(redactor)
            public_response._attach_assertion_view(raw_response)
            return public_response
        except ApiTransportError:
            raise
        except Exception as error:
            raise map_httpx_error(error) from None

    def _timeout(self, request_timeout: float | None) -> httpx.Timeout:
        timeout = request_timeout if request_timeout is not None else self._timeout_seconds
        return httpx.Timeout(connect=timeout, read=timeout, write=timeout, pool=timeout)

    def _request_arguments(self, request: ApiRequest) -> dict[str, Any]:
        arguments: dict[str, Any] = {
            "method": request.method,
            "url": request.url,
            "params": request.query,
            "headers": request.headers,
        }
        if request.json_body is not None:
            arguments["json"] = request.json_body
        elif request.form:
            arguments["data"] = request.form
        elif request.multipart:
            arguments["files"] = [(part.name, (None, part.value)) for part in request.multipart]
        elif request.body is not None:
            arguments["content"] = request.body
        return arguments

    def _redactor_for_request(self, request: ApiRequest) -> Redactor:
        policy = self._redactor.policy if self._redactor is not None else RedactionPolicy.default()
        request_values = _request_secret_values(request, policy.secret_fields)
        return Redactor(
            RedactionPolicy(
                secret_fields=policy.secret_fields,
                secret_values=(*policy.secret_values, *request_values),
            )
        )

    async def _read_response_body(self, response: httpx.Response) -> tuple[bytes, bool]:
        body = bytearray()
        async for chunk in _iter_response_chunks(response, self._response_max_bytes + 1):
            remaining = self._response_max_bytes - len(body)
            if remaining <= 0:
                return bytes(body), True
            if len(chunk) > remaining:
                body.extend(chunk[:remaining])
                return bytes(body), True
            body.extend(chunk)
        return bytes(body), False


async def _iter_response_chunks(response: httpx.Response, chunk_size: int) -> AsyncIterator[bytes]:
    async for chunk in response.aiter_bytes(chunk_size=chunk_size):
        yield chunk


def map_httpx_error(error: Exception) -> ApiTransportError:
    """Convert HTTPX/runtime failures into stable, non-secret-facing errors."""

    chain = tuple(_exception_chain(error))
    if isinstance(error, httpx.InvalidURL):
        message = "接口地址无效，请检查地址后重试。"
    elif _contains(chain, ssl.SSLError):
        message = "接口 TLS 连接验证失败，请检查证书或地址后重试。"
    elif _contains(chain, socket.gaierror):
        message = "无法解析接口域名，请检查网络或域名后重试。"
    elif isinstance(error, httpx.ConnectTimeout):
        message = "连接接口超时，请检查网络后重试。"
    elif isinstance(error, httpx.ReadTimeout):
        message = "等待接口响应超时，请稍后重试。"
    elif isinstance(error, httpx.TimeoutException):
        message = "接口请求超时，请稍后重试。"
    elif isinstance(
        error,
        (
            httpx.ReadError,
            httpx.DecodingError,
            httpx.ProtocolError,
            UnicodeError,
            LookupError,
        ),
    ):
        message = "接口响应无法读取，请检查服务响应后重试。"
    elif isinstance(error, httpx.ConnectError):
        message = "无法连接到接口服务，请检查网络或服务状态后重试。"
    else:
        message = "接口请求失败，请稍后重试。"
    return ApiTransportError(message, debug_message=f"httpx.{type(error).__name__}")


def _exception_chain(error: Exception) -> Iterator[Exception]:
    current: BaseException | None = error
    seen: set[int] = set()
    while isinstance(current, Exception) and id(current) not in seen:
        seen.add(id(current))
        yield current
        current = current.__cause__ or current.__context__


def _contains(errors: tuple[Exception, ...], error_type: type[BaseException]) -> bool:
    return any(isinstance(error, error_type) for error in errors)


def _header_value(headers: Mapping[str, str], name: str) -> str | None:
    for header_name, value in headers.items():
        if header_name.casefold() == name:
            return value
    return None


def _content_length(headers: Mapping[str, str]) -> int | None:
    value = _header_value(headers, "content-length")
    if value is None:
        return None
    value = value.strip()
    if not value or not value.isascii() or not value.isdecimal():
        return None
    return int(value)


def _response_size(
    observed_size: int, truncated: bool, content_length: int | None
) -> tuple[int, bool]:
    if not truncated:
        return observed_size, True
    if content_length is not None and content_length > observed_size:
        return content_length, True
    return observed_size, False


def _positive_finite_timeout(value: object) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value <= 0
    ):
        raise ValueError("timeout_seconds 必须是有限正数")
    return float(value)


def _request_secret_values(request: ApiRequest, secret_fields: frozenset[str]) -> tuple[str, ...]:
    normalized_fields = {_normalize_field_name(field) for field in secret_fields}
    values: list[str] = []

    try:
        parsed_url = urlsplit(request.url)
        if parsed_url.username:
            _append_secret(values, parsed_url.username)
        if parsed_url.password:
            _append_secret(values, parsed_url.password)
        _collect_named_values(
            parse_qsl(parsed_url.query, keep_blank_values=True), normalized_fields, values
        )
    except ValueError:
        pass

    for name, value in request.headers.items():
        if _normalize_field_name(name) not in normalized_fields:
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

    _collect_named_values(request.query.items(), normalized_fields, values)
    _collect_json_secret_values(request.json_body, normalized_fields, values)
    _collect_named_values(request.form.items(), normalized_fields, values)
    _collect_named_values(
        ((part.name, part.value) for part in request.multipart), normalized_fields, values
    )
    if request.body:
        _collect_body_secret_values(request.body, normalized_fields, values)
    return tuple(values)


def _collect_named_values(
    items: Iterable[tuple[object, Any]],
    normalized_fields: set[str],
    values: list[str],
) -> None:
    for name, value in items:
        if _normalize_field_name(str(name)) in normalized_fields:
            _collect_scalar_strings(value, values)


def _collect_json_secret_values(
    value: Any,
    normalized_fields: set[str],
    values: list[str],
    *,
    sensitive: bool = False,
) -> None:
    if isinstance(value, Mapping):
        for name, nested_value in value.items():
            _collect_json_secret_values(
                nested_value,
                normalized_fields,
                values,
                sensitive=sensitive or _normalize_field_name(str(name)) in normalized_fields,
            )
    elif isinstance(value, (list, tuple)):
        for nested_value in value:
            _collect_json_secret_values(
                nested_value, normalized_fields, values, sensitive=sensitive
            )
    elif sensitive:
        _collect_scalar_strings(value, values)


def _collect_body_secret_values(body: str, normalized_fields: set[str], values: list[str]) -> None:
    try:
        parsed_body = json.loads(body)
    except (json.JSONDecodeError, ValueError):
        parsed_body = None
    if parsed_body is not None:
        _collect_json_secret_values(parsed_body, normalized_fields, values)
    _collect_named_values(parse_qsl(body, keep_blank_values=True), normalized_fields, values)


def _collect_scalar_strings(value: Any, values: list[str]) -> None:
    if isinstance(value, str):
        _append_secret(values, value)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _collect_scalar_strings(item, values)


def _append_secret(values: list[str], value: str) -> None:
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


def _elapsed_ms(response: httpx.Response, started_at: float) -> float:
    try:
        elapsed = response.elapsed
    except (AttributeError, RuntimeError):
        return (time.perf_counter() - started_at) * 1000
    if isinstance(elapsed, timedelta):
        return elapsed.total_seconds() * 1000
    return (time.perf_counter() - started_at) * 1000


__all__ = ["HttpxTransport", "map_httpx_error"]
