from __future__ import annotations

import json
import re
import socket
import ssl
from collections.abc import AsyncIterator, Callable
from datetime import timedelta
from typing import Any
from urllib.parse import quote, quote_plus

import httpx
import pytest

from csbox.api.errors import ApiTransportError
from csbox.api.httpx_transport import HttpxTransport, map_httpx_error
from csbox.api.models import ApiMultipartPart, ApiRequest
from csbox.api.redaction import REDACTION_MARKER, Redactor
from csbox.api.transport import ApiTransport

SECRET = "CSBOX_SECRET_SENTINEL_transport"
CONFIGURED_SECRET = "CSBOX_CONFIGURED_SECRET_transport"


class FakeResponse:
    def __init__(
        self,
        chunks: tuple[bytes, ...] = (b'{"ok":true}',),
        *,
        headers: dict[str, str] | None = None,
        url: str = "https://example.test/result",
        error: Exception | None = None,
    ) -> None:
        self.status_code = 201
        self.headers = headers or {"Content-Type": "application/json; charset=utf-8"}
        self.url = url
        self.elapsed = timedelta(milliseconds=12)
        self._chunks = chunks
        self._error = error
        self.chunk_size: int | None = None

    async def aiter_bytes(self, chunk_size: int | None = None) -> AsyncIterator[bytes]:
        self.chunk_size = chunk_size
        for chunk in self._chunks:
            yield chunk
        if self._error is not None:
            raise self._error


class FakeStream:
    def __init__(
        self, response: FakeResponse | None = None, *, error: Exception | None = None
    ) -> None:
        self.response = response
        self.error = error

    async def __aenter__(self) -> FakeResponse:
        if self.error is not None:
            raise self.error
        assert self.response is not None
        return self.response

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> bool:
        return False


class FakeClient:
    def __init__(self, stream: FakeStream) -> None:
        self._stream = stream
        self.client_kwargs: dict[str, Any] | None = None
        self.stream_kwargs: dict[str, Any] | None = None
        self.closed = False

    async def __aenter__(self) -> FakeClient:
        return self

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> bool:
        self.closed = True
        return False

    def stream(self, **kwargs: Any) -> FakeStream:
        self.stream_kwargs = kwargs
        return self._stream


def _factory(client: FakeClient) -> Callable[..., FakeClient]:
    def factory(**kwargs: Any) -> FakeClient:
        client.client_kwargs = kwargs
        return client

    return factory


@pytest.mark.parametrize("response_max_bytes", [0, -1, True, 1.5])
def test_transport_requires_a_positive_integer_response_limit(response_max_bytes: object) -> None:
    with pytest.raises(ValueError, match="response_max_bytes"):
        HttpxTransport(response_max_bytes=response_max_bytes)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "timeout_seconds",
    [0, 0.0, -1, float("nan"), float("inf"), float("-inf"), True, "1"],
)
def test_transport_requires_a_finite_positive_timeout(timeout_seconds: object) -> None:
    with pytest.raises(ValueError, match="timeout_seconds"):
        HttpxTransport(timeout_seconds=timeout_seconds)  # type: ignore[arg-type]


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"])
async def test_transport_maps_allowed_methods_query_headers_and_client_security_defaults(
    method: str,
) -> None:
    client = FakeClient(FakeStream(FakeResponse()))
    transport: ApiTransport = HttpxTransport(client_factory=_factory(client))
    request = ApiRequest(
        method=method,  # type: ignore[arg-type]
        url="https://example.test/items",
        query={"page": "2"},
        headers={"X-Trace": "trace-1", "Authorization": "Bearer parser-value"},
        timeout_seconds=2.5,
    )

    response = await transport.send(request)

    assert response.status_code == 201
    assert response.headers == {"Content-Type": "application/json; charset=utf-8"}
    assert response.content_type == "application/json; charset=utf-8"
    assert response.response_size == len(b'{"ok":true}')
    assert response.response_size_exact is True
    assert response.elapsed_ms == 12.0
    assert client.stream_kwargs == {
        "method": method,
        "url": "https://example.test/items",
        "params": {"page": "2"},
        "headers": {"X-Trace": "trace-1", "Authorization": "Bearer parser-value"},
    }
    assert client.client_kwargs is not None
    assert client.client_kwargs["verify"] is True
    assert client.client_kwargs["follow_redirects"] is False
    timeout = client.client_kwargs["timeout"]
    assert (timeout.connect, timeout.read, timeout.write, timeout.pool) == (2.5, 2.5, 2.5, 2.5)
    assert client.closed is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("api_request", "expected_body"),
    [
        (
            ApiRequest(method="POST", url="https://example.test", json_body={"name": "CSBox"}),
            {"json": {"name": "CSBox"}},
        ),
        (
            ApiRequest(method="POST", url="https://example.test", form={"name": "CSBox"}),
            {"data": {"name": "CSBox"}},
        ),
        (
            ApiRequest(
                method="POST",
                url="https://example.test",
                multipart=(
                    ApiMultipartPart(name="description", value="报告"),
                    ApiMultipartPart(name="attachment", value="report.pdf"),
                ),
            ),
            {"files": [("description", (None, "报告")), ("attachment", (None, "report.pdf"))]},
        ),
        (
            ApiRequest(method="POST", url="https://example.test", body="raw body"),
            {"content": "raw body"},
        ),
    ],
)
async def test_transport_maps_each_supported_body_kind(
    api_request: ApiRequest, expected_body: dict[str, object]
) -> None:
    client = FakeClient(FakeStream(FakeResponse()))

    await HttpxTransport(client_factory=_factory(client)).send(api_request)

    assert client.stream_kwargs is not None
    assert {key: client.stream_kwargs[key] for key in expected_body} == expected_body


@pytest.mark.asyncio
async def test_transport_bounds_response_without_materializing_unbounded_content() -> None:
    response = FakeResponse(chunks=(b"abcd", b"efgh"))
    client = FakeClient(FakeStream(response))

    result = await HttpxTransport(client_factory=_factory(client), response_max_bytes=5).send(
        ApiRequest(method="GET", url="https://example.test")
    )

    assert result.body == "abcde"
    assert result.response_size == 5
    assert result.response_size_exact is False
    assert result.truncated is True
    assert response.chunk_size == 6
    assert client.closed is True


@pytest.mark.asyncio
async def test_transport_preserves_content_length_for_a_truncated_response() -> None:
    response = FakeResponse(
        chunks=(b"abcd", b"efgh"),
        headers={"Content-Type": "text/plain", "Content-Length": "8"},
    )
    client = FakeClient(FakeStream(response))

    result = await HttpxTransport(client_factory=_factory(client), response_max_bytes=5).send(
        ApiRequest(method="GET", url="https://example.test")
    )

    assert result.body == "abcde"
    assert result.response_size == 8
    assert result.response_size_exact is True
    assert result.truncated is True
    assert response.chunk_size == 6
    assert client.closed is True


@pytest.mark.asyncio
async def test_transport_reports_an_exact_observed_size_after_a_complete_stream() -> None:
    response = FakeResponse(
        chunks=(b"complete",),
        headers={"Content-Type": "text/plain"},
    )
    client = FakeClient(FakeStream(response))

    result = await HttpxTransport(client_factory=_factory(client), response_max_bytes=20).send(
        ApiRequest(method="GET", url="https://example.test")
    )

    assert result.body == "complete"
    assert result.response_size == len(b"complete")
    assert result.response_size_exact is True
    assert result.truncated is False


@pytest.mark.asyncio
async def test_transport_uses_observed_size_when_completed_content_length_disagrees() -> None:
    response = FakeResponse(
        chunks=(b"complete",),
        headers={"Content-Type": "text/plain", "Content-Length": "99"},
    )
    client = FakeClient(FakeStream(response))

    result = await HttpxTransport(client_factory=_factory(client), response_max_bytes=20).send(
        ApiRequest(method="GET", url="https://example.test")
    )

    assert result.response_size == len(b"complete")
    assert result.response_size_exact is True
    assert result.truncated is False


@pytest.mark.asyncio
async def test_transport_rejects_inconsistent_content_length_for_a_truncated_stream() -> None:
    response = FakeResponse(
        chunks=(b"abcd", b"efgh"),
        headers={"Content-Type": "text/plain", "Content-Length": "4"},
    )
    client = FakeClient(FakeStream(response))

    result = await HttpxTransport(client_factory=_factory(client), response_max_bytes=5).send(
        ApiRequest(method="GET", url="https://example.test")
    )

    assert result.response_size == 5
    assert result.response_size_exact is False
    assert result.truncated is True


@pytest.mark.asyncio
async def test_transport_returns_only_a_request_derived_redacted_response() -> None:
    authorization_secret = f"{SECRET}_authorization"
    cookie_secret = f"{SECRET}_cookie"
    password_secret = f"{SECRET}_password"
    token_secret = f"{SECRET}_token"
    username_secret = f"{SECRET}_username"
    url_secret = f"{SECRET}_url"
    response_only_secret = f"{SECRET}_response_only"
    raw_secrets = (
        authorization_secret,
        cookie_secret,
        password_secret,
        token_secret,
        username_secret,
        url_secret,
        response_only_secret,
    )
    response_body = json.dumps(
        {
            "echo": [
                authorization_secret,
                cookie_secret,
                password_secret,
                token_secret,
                username_secret,
                url_secret,
            ],
            "token": response_only_secret,
            "safe": "visible",
        }
    ).encode()
    response = FakeResponse(
        chunks=(response_body,),
        headers={
            "Content-Type": "application/problem+json",
            "Set-Cookie": f"session={response_only_secret}",
            "X-Echo": authorization_secret,
        },
        url=(
            f"https://{username_secret}:{url_secret}@example.test/result"
            f"?token={token_secret}&echo={password_secret}"
        ),
    )
    client = FakeClient(FakeStream(response))
    request = ApiRequest(
        method="POST",
        url=f"https://{username_secret}:{url_secret}@example.test/source",
        headers={
            "Authorization": f"Bearer {authorization_secret}",
            "Cookie": f"session={cookie_secret}",
        },
        query={"access_token": token_secret},
        json_body={"password": password_secret},
    )

    result = await HttpxTransport(client_factory=_factory(client)).send(request)

    serialized = result.model_dump_json()
    rendered = repr(result)
    parsed_body = json.loads(result.body)
    for secret in raw_secrets:
        assert secret not in serialized
        assert secret not in rendered
    assert parsed_body["echo"] == [REDACTION_MARKER] * 6
    assert parsed_body["token"] == REDACTION_MARKER
    assert parsed_body["safe"] == "visible"
    assert result.headers["Set-Cookie"] == REDACTION_MARKER
    assert result.headers["X-Echo"] == REDACTION_MARKER
    assert result.url.startswith(
        f"https://{REDACTION_MARKER}:{REDACTION_MARKER}@example.test/result"
    )
    assertion_response = result.assertion_view()
    assert assertion_response is not result
    assert json.loads(assertion_response.body)["token"] == response_only_secret
    assert assertion_response.headers["Set-Cookie"] == f"session={response_only_secret}"
    assert url_secret in assertion_response.url
    assert token_secret in assertion_response.url


@pytest.mark.asyncio
async def test_transport_combines_configured_and_request_derived_redaction_for_text() -> None:
    request_secret = f"{SECRET}_request"
    response = FakeResponse(
        chunks=(f"configured={CONFIGURED_SECRET}; request={request_secret}".encode(),),
        headers={"Content-Type": "text/plain"},
    )
    client = FakeClient(FakeStream(response))
    redactor = Redactor.with_configured_values([CONFIGURED_SECRET])

    result = await HttpxTransport(client_factory=_factory(client), redactor=redactor).send(
        ApiRequest(
            method="GET",
            url="https://example.test",
            query={"token": request_secret},
        )
    )

    assert result.body == f"configured={REDACTION_MARKER}; request={REDACTION_MARKER}"
    assert CONFIGURED_SECRET not in result.model_dump_json()
    assert request_secret not in repr(result)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("content_type", "body", "expected_body"),
    [
        (
            "application/json",
            f'{{"password":"{SECRET}"',
            f'{{"password":"{REDACTION_MARKER}"',
        ),
        (
            "text/plain",
            f"password={SECRET}; token: {SECRET}",
            f"password={REDACTION_MARKER}; token: {REDACTION_MARKER}",
        ),
    ],
)
async def test_transport_masks_response_only_secrets_in_text_fallbacks(
    content_type: str, body: str, expected_body: str
) -> None:
    response = FakeResponse(
        chunks=(body.encode(),),
        headers={"Content-Type": content_type, "X-Debug": f"token: {SECRET}"},
    )
    client = FakeClient(FakeStream(response))

    result = await HttpxTransport(client_factory=_factory(client)).send(
        ApiRequest(method="GET", url="https://example.test")
    )

    assert result.body == expected_body
    assert result.headers["X-Debug"] == f"token: {REDACTION_MARKER}"
    assert SECRET not in result.model_dump_json()


@pytest.mark.asyncio
async def test_transport_masks_nested_response_only_assignments_in_body_and_headers() -> None:
    body = f'{{"context":"token={SECRET}"'
    response = FakeResponse(
        chunks=(body.encode(),),
        headers={
            "Content-Type": "application/json",
            "X-Debug": f"context=token={SECRET}",
        },
    )
    client = FakeClient(FakeStream(response))

    result = await HttpxTransport(client_factory=_factory(client)).send(
        ApiRequest(method="GET", url="https://example.test")
    )

    assert result.body == f'{{"context":"token={REDACTION_MARKER}"'
    assert result.headers["X-Debug"] == f"context=token={REDACTION_MARKER}"
    assert SECRET not in result.model_dump_json()


@pytest.mark.asyncio
async def test_transport_masks_percent_encoded_response_only_assignments() -> None:
    body_secret = "response%20body%2Fsecret%2Bvalue"
    header_secret = "response%20header%2Fsecret%2Bvalue"
    response = FakeResponse(
        chunks=(f"token%3D{body_secret}".encode(),),
        headers={
            "Content-Type": "text/plain",
            "X-Debug": f"token%3A{header_secret}",
        },
    )
    client = FakeClient(FakeStream(response))

    result = await HttpxTransport(client_factory=_factory(client)).send(
        ApiRequest(method="GET", url="https://example.test")
    )

    assert result.body == f"token%3D{REDACTION_MARKER}"
    assert result.headers["X-Debug"] == f"token%3A{REDACTION_MARKER}"
    assert body_secret not in result.model_dump_json()
    assert header_secret not in result.model_dump_json()


@pytest.mark.asyncio
async def test_transport_redacts_percent_encoded_query_secret_echoes() -> None:
    request_secret = f"{SECRET} with /plus+percent%?"
    quoted_secret = quote(request_secret, safe="")
    form_quoted_secret = quote_plus(request_secret, safe="")
    response = FakeResponse(
        chunks=(f"echo={form_quoted_secret}".encode(),),
        headers={"Content-Type": "text/plain", "X-Echo": quoted_secret},
    )
    client = FakeClient(FakeStream(response))

    result = await HttpxTransport(client_factory=_factory(client)).send(
        ApiRequest(
            method="GET",
            url="https://example.test",
            query={"token": request_secret},
        )
    )

    assert result.headers["X-Echo"] == REDACTION_MARKER
    assert result.body == f"echo={REDACTION_MARKER}"
    assert quoted_secret not in result.model_dump_json()
    assert form_quoted_secret not in result.model_dump_json()


@pytest.mark.asyncio
async def test_transport_redacts_lowercase_percent_escape_secret_echoes() -> None:
    request_secret = f"{SECRET}_MiXeD with /plus+percent%?"
    quoted_secret = re.sub(
        r"%[0-9A-F]{2}", lambda match: match.group(0).lower(), quote(request_secret, safe="")
    )
    form_quoted_secret = re.sub(
        r"%[0-9A-F]{2}",
        lambda match: match.group(0).lower(),
        quote_plus(request_secret, safe=""),
    )
    response = FakeResponse(
        chunks=(f"echo={form_quoted_secret}".encode(),),
        headers={"Content-Type": "text/plain", "X-Echo": quoted_secret},
    )
    client = FakeClient(FakeStream(response))

    result = await HttpxTransport(client_factory=_factory(client)).send(
        ApiRequest(
            method="GET",
            url="https://example.test",
            query={"token": request_secret},
        )
    )

    assert result.headers["X-Echo"] == REDACTION_MARKER
    assert result.body == f"echo={REDACTION_MARKER}"
    assert quoted_secret not in result.model_dump_json()
    assert form_quoted_secret not in result.model_dump_json()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        FakeResponse(chunks=(b"\xff",)),
        FakeResponse(error=httpx.ReadError(f"read failed {SECRET}")),
    ],
)
async def test_transport_maps_invalid_encoding_and_response_read_failure(
    response: FakeResponse,
) -> None:
    client = FakeClient(FakeStream(response))

    with pytest.raises(ApiTransportError) as captured:
        await HttpxTransport(client_factory=_factory(client)).send(
            ApiRequest(method="GET", url=f"https://user:{SECRET}@example.test")
        )

    assert captured.value.user_message == "接口响应无法读取，请检查服务响应后重试。"
    assert SECRET not in str(captured.value)
    assert SECRET not in repr(captured.value)
    assert client.closed is True


@pytest.mark.asyncio
async def test_transport_closes_client_when_request_opening_fails() -> None:
    client = FakeClient(FakeStream(error=httpx.ConnectError(f"cannot reach {SECRET}")))

    with pytest.raises(ApiTransportError):
        await HttpxTransport(client_factory=_factory(client)).send(
            ApiRequest(method="GET", url="https://example.test")
        )

    assert client.closed is True


def _dns_error() -> httpx.ConnectError:
    error = httpx.ConnectError(f"name lookup {SECRET}")
    error.__cause__ = socket.gaierror(socket.EAI_NONAME, "host missing")
    return error


def _tls_error() -> httpx.ConnectError:
    error = httpx.ConnectError(f"TLS failure {SECRET}")
    error.__cause__ = ssl.SSLError("certificate verify failed")
    return error


@pytest.mark.parametrize(
    ("error", "message"),
    [
        (httpx.InvalidURL(f"https://user:{SECRET}@"), "接口地址无效，请检查地址后重试。"),
        (_dns_error(), "无法解析接口域名，请检查网络或域名后重试。"),
        (
            httpx.ConnectError(f"connection refused {SECRET}"),
            "无法连接到接口服务，请检查网络或服务状态后重试。",
        ),
        (httpx.ConnectTimeout(f"connect timeout {SECRET}"), "连接接口超时，请检查网络后重试。"),
        (httpx.ReadTimeout(f"read timeout {SECRET}"), "等待接口响应超时，请稍后重试。"),
        (httpx.TimeoutException(f"timeout {SECRET}"), "接口请求超时，请稍后重试。"),
        (_tls_error(), "接口 TLS 连接验证失败，请检查证书或地址后重试。"),
        (httpx.ReadError(f"response failed {SECRET}"), "接口响应无法读取，请检查服务响应后重试。"),
        (
            httpx.RemoteProtocolError(f"malformed response {SECRET}"),
            "接口响应无法读取，请检查服务响应后重试。",
        ),
    ],
)
def test_map_httpx_error_has_stable_secret_safe_user_messages(
    error: Exception, message: str
) -> None:
    mapped = map_httpx_error(error)

    assert mapped.user_message == message
    assert str(mapped) == message
    assert SECRET not in str(mapped)
    assert SECRET not in repr(mapped)
    assert mapped.debug_message == f"httpx.{type(error).__name__}"
