from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC

import httpx
import pytest

from csbox.api.errors import ApiTransportError
from csbox.api.httpx_transport import HttpxTransport
from csbox.api.models import (
    ApiAssertion,
    ApiRequest,
    ApiResponse,
    ApiScenario,
    ApiStep,
)
from csbox.api.redaction import REDACTION_MARKER, Redactor
from csbox.api.runner import ApiRunner

SECRET = "CSBOX_SECRET_SENTINEL_runner"


class StubTransport:
    def __init__(self, outcomes: Sequence[ApiResponse | Exception]) -> None:
        self._outcomes = list(outcomes)
        self.requests: list[ApiRequest] = []

    async def send(self, request: ApiRequest) -> ApiResponse:
        self.requests.append(request)
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _response(
    status_code: int = 200,
    *,
    body: str = '{"status":"ok"}',
    headers: dict[str, str] | None = None,
) -> ApiResponse:
    return ApiResponse(
        status_code=status_code,
        headers=headers or {"Content-Type": "application/json"},
        body=body,
        url="https://example.test/result",
        elapsed_ms=4.5,
        response_size=len(body.encode()),
    )


def _step(name: str, *, status: int = 200) -> ApiStep:
    return ApiStep(
        name=name,
        request=ApiRequest(method="GET", url=f"https://example.test/{name}"),
        assertions=(ApiAssertion(kind="status", expected=status),),
    )


@pytest.mark.asyncio
async def test_runner_interpolates_request_fields_with_scenario_variables_and_overrides() -> None:
    transport = StubTransport([_response()])
    scenario = ApiScenario(
        name="变量",
        variables={"base_url": "https://scenario.test", "name": "scenario-name"},
        steps=(
            ApiStep(
                name="提交",
                request=ApiRequest(
                    method="POST",
                    url="{{base_url}}/items/{{item_id}}",
                    headers={"X-Name": "{{name}}"},
                    query={"page": "{{page}}"},
                    json_body={"name": "{{name}}", "ids": ["{{item_id}}"]},
                    form={},
                ),
                assertions=(ApiAssertion(kind="status", expected=200),),
            ),
        ),
    )

    run = await ApiRunner(transport, Redactor.with_configured_values([])).run(
        scenario,
        {"base_url": "https://override.test", "item_id": "42", "page": "2"},
    )

    assert transport.requests[0].url == "https://override.test/items/42"
    assert transport.requests[0].headers == {"X-Name": "scenario-name"}
    assert transport.requests[0].query == {"page": "2"}
    assert transport.requests[0].json_body == {"name": "scenario-name", "ids": ["42"]}
    assert run.scenario.steps[0].request == transport.requests[0]
    assert run.status == "PASS"
    assert run.results[0].status == "PASS"


@pytest.mark.asyncio
async def test_runner_executes_in_file_order_and_continues_after_assertion_failure() -> None:
    transport = StubTransport([_response(500), _response(200)])
    scenario = ApiScenario(name="顺序", steps=(_step("first"), _step("second")))

    run = await ApiRunner(transport, Redactor.with_configured_values([])).run(scenario, {})

    assert [request.url.rsplit("/", 1)[-1] for request in transport.requests] == ["first", "second"]
    assert tuple(result.status for result in run.results) == ("FAIL", "PASS")
    assert run.status == "FAIL"


@pytest.mark.asyncio
async def test_runner_fail_fast_stops_after_first_failing_assertion() -> None:
    transport = StubTransport([_response(500), _response(200)])
    scenario = ApiScenario(name="快速失败", steps=(_step("first"), _step("second")))

    run = await ApiRunner(transport, Redactor.with_configured_values([])).run(
        scenario, {}, fail_fast=True
    )

    assert len(transport.requests) == 1
    assert len(run.results) == 1
    assert run.status == "FAIL"


@pytest.mark.asyncio
async def test_transport_error_is_safe_skips_assertions_and_continues() -> None:
    transport = StubTransport(
        [
            ApiTransportError(
                "接口请求失败，请稍后重试。",
                debug_message=f"socket detail {SECRET}",
            ),
            _response(200),
        ]
    )
    scenario = ApiScenario(
        name="运行错误",
        steps=(
            ApiStep(
                name="broken",
                request=ApiRequest(method="GET", url="https://example.test/broken"),
                assertions=(
                    ApiAssertion(kind="status", expected=200),
                    ApiAssertion(kind="body", expected="ok"),
                ),
            ),
            _step("healthy"),
        ),
    )

    run = await ApiRunner(transport, Redactor.with_configured_values([SECRET])).run(scenario, {})

    broken = run.results[0]
    assert len(transport.requests) == 2
    assert broken.status == "RUNTIME_ERROR"
    assert tuple(assertion.status for assertion in broken.assertions) == ("SKIP", "SKIP")
    assert all("未收到响应" in assertion.message for assertion in broken.assertions)
    assert broken.response is None
    assert broken.error == "接口请求失败，请稍后重试。"
    assert run.results[1].status == "PASS"
    assert run.status == "RUNTIME_ERROR"
    assert SECRET not in run.model_dump_json()
    assert SECRET not in repr(run)


@pytest.mark.asyncio
async def test_runner_fail_fast_stops_after_transport_error() -> None:
    transport = StubTransport([ApiTransportError("连接失败。"), _response()])
    scenario = ApiScenario(name="快速运行错误", steps=(_step("first"), _step("second")))

    run = await ApiRunner(transport, Redactor.with_configured_values([])).run(
        scenario, {}, fail_fast=True
    )

    assert len(transport.requests) == 1
    assert len(run.results) == 1
    assert run.status == "RUNTIME_ERROR"


@pytest.mark.asyncio
async def test_runner_turns_missing_interpolation_into_config_error_without_a_traceback() -> None:
    transport = StubTransport([_response()])
    scenario = ApiScenario(
        name="配置错误",
        steps=(
            ApiStep(
                name="missing",
                request=ApiRequest(method="GET", url="https://example.test/{{missing}}"),
                assertions=(ApiAssertion(kind="status", expected=200),),
            ),
            _step("healthy"),
        ),
    )

    run = await ApiRunner(transport, Redactor.with_configured_values([])).run(scenario, {})

    missing = run.results[0]
    assert missing.status == "CONFIG_ERROR"
    assert missing.response is None
    assert missing.error is not None
    assert "发生了什么" in missing.error
    assert "Traceback" not in missing.error
    assert missing.assertions[0].status == "SKIP"
    assert len(transport.requests) == 1
    assert run.results[1].status == "PASS"
    assert run.status == "CONFIG_ERROR"


@pytest.mark.asyncio
async def test_unexpected_transport_exception_is_a_generic_secret_safe_runtime_error() -> None:
    transport = StubTransport([RuntimeError(f"raw socket failure {SECRET}")])

    run = await ApiRunner(transport, Redactor.with_configured_values([SECRET])).run(
        ApiScenario(name="异常", steps=(_step("broken"),)),
        {},
    )

    result = run.results[0]
    assert result.status == "RUNTIME_ERROR"
    assert result.error is not None
    assert "未预期" in result.error
    assert SECRET not in result.error
    assert SECRET not in run.model_dump_json()
    assert SECRET not in repr(run)


@pytest.mark.asyncio
async def test_runner_keeps_raw_secrets_transient_and_stores_only_redacted_views() -> None:
    raw_response = _response(
        body=f'{{"value":"{SECRET}"}}',
        headers={"X-Echo": SECRET, "Set-Cookie": f"session={SECRET}"},
    )
    transport = StubTransport([raw_response])
    scenario = ApiScenario(
        name="安全",
        variables={"token": SECRET},
        steps=(
            ApiStep(
                name="secret",
                request=ApiRequest(
                    method="POST",
                    url="https://example.test?token={{token}}",
                    headers={"Authorization": "Bearer {{token}}"},
                    json_body={"password": "{{token}}"},
                ),
                assertions=(ApiAssertion(kind="json_path", location="$.value", expected=SECRET),),
            ),
        ),
    )
    redactor = Redactor.with_configured_values([SECRET])

    run = await ApiRunner(transport, redactor).run(scenario, {})

    assert SECRET in transport.requests[0].url
    assert transport.requests[0].headers["Authorization"] == f"Bearer {SECRET}"
    assert raw_response.body == f'{{"value":"{SECRET}"}}'
    stored_request = run.scenario.steps[0].request
    stored_response = run.results[0].response
    assert stored_request.headers["Authorization"] == f"Bearer {REDACTION_MARKER}"
    assert stored_response is not None
    assert stored_response is not raw_response
    assert SECRET not in stored_response.body
    assert SECRET not in run.model_dump_json()
    assert SECRET not in repr(run)


@pytest.mark.asyncio
async def test_runner_never_stores_response_only_secret_behind_an_escaped_json_key() -> None:
    body = f'{{\n  "to\\u006ben"\n  :\n  "{SECRET}"\n}}'
    raw_response = ApiResponse(
        status_code=200,
        headers={"Content-Type": "application/json"},
        body=body,
        url="https://example.test/result",
        content_type="application/json",
        response_size=len(body.encode()),
    )
    transport = StubTransport([raw_response])
    scenario = ApiScenario(
        name="响应脱敏",
        steps=(
            ApiStep(
                name="response-only",
                request=ApiRequest(method="GET", url="https://example.test/response-only"),
                assertions=(ApiAssertion(kind="json_path", location="$.token", expected=SECRET),),
            ),
        ),
    )

    run = await ApiRunner(transport, Redactor.with_configured_values([])).run(scenario, {})

    stored_response = run.results[0].response
    assert raw_response.body == body
    assert SECRET in raw_response.body
    assert stored_response is not None
    assert stored_response is not raw_response
    assert run.results[0].status == "PASS"
    assert run.results[0].assertions[0].status == "PASS"
    assert run.results[0].assertions[0].assertion.expected == REDACTION_MARKER
    assert run.results[0].assertions[0].actual == REDACTION_MARKER
    assert SECRET not in stored_response.body
    assert SECRET not in run.model_dump_json()
    assert SECRET not in repr(run)


@pytest.mark.asyncio
async def test_runner_never_serializes_malformed_escaped_multiline_response_secret() -> None:
    body = f'{{\n"to\\u006ben"\r\n:\n"{SECRET}",\n"safe"\n:\n"visible"'
    raw_response = ApiResponse(
        status_code=200,
        headers={"Content-Type": "application/json"},
        body=body,
        url="https://example.test/result",
        content_type="application/json",
        response_size=len(body.encode()),
    )
    scenario = ApiScenario(
        name="畸形响应脱敏",
        steps=(
            ApiStep(
                name="malformed",
                request=ApiRequest(method="GET", url="https://example.test/malformed"),
                assertions=(ApiAssertion(kind="status", expected=200),),
            ),
        ),
    )

    run = await ApiRunner(StubTransport([raw_response]), Redactor.with_configured_values([])).run(
        scenario, {}
    )

    stored_response = run.results[0].response
    assert run.results[0].status == "PASS"
    assert stored_response is not None
    assert stored_response.body == body.replace(SECRET, REDACTION_MARKER)
    assert '"safe"\n:\n"visible"' in stored_response.body
    assert SECRET not in run.model_dump_json()
    assert SECRET not in repr(run)


@pytest.mark.asyncio
async def test_httpx_runner_asserts_against_raw_echo_but_stores_only_redacted_response() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["token"] == SECRET
        return httpx.Response(
            200,
            json={"token": request.url.params["token"], "safe": "visible"},
            request=request,
        )

    def client_factory(**kwargs: object) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(handler), **kwargs)

    scenario = ApiScenario(
        name="HTTPX 原始断言",
        steps=(
            ApiStep(
                name="echo",
                request=ApiRequest(
                    method="GET",
                    url="https://example.test/echo",
                    query={"token": SECRET},
                ),
                assertions=(ApiAssertion(kind="json_path", location="$.token", expected=SECRET),),
            ),
        ),
    )

    run = await ApiRunner(
        HttpxTransport(client_factory=client_factory),
        Redactor.with_configured_values([]),
    ).run(scenario, {})

    stored_response = run.results[0].response
    assert run.status == "PASS"
    assert run.results[0].assertions[0].status == "PASS"
    assert stored_response is not None
    assert json.loads(stored_response.body) == {
        "token": REDACTION_MARKER,
        "safe": "visible",
    }
    assert SECRET not in run.model_dump_json()
    assert SECRET not in repr(run)


@pytest.mark.asyncio
async def test_runner_adds_sensitive_variable_values_to_run_scoped_redaction() -> None:
    transport = StubTransport([_response(body=f'{{"echo":"{SECRET}"}}')])
    scenario = ApiScenario(
        name="变量安全",
        variables={"token": SECRET},
        steps=(
            ApiStep(
                name="echo",
                request=ApiRequest(
                    method="GET",
                    url="https://example.test/echo",
                    headers={"X-Echo": "{{token}}"},
                ),
                assertions=(
                    ApiAssertion(kind="json_path", location="$.echo", expected="different"),
                ),
            ),
        ),
    )

    run = await ApiRunner(transport, Redactor.with_configured_values([])).run(scenario, {})

    assert transport.requests[0].headers["X-Echo"] == SECRET
    assert run.scenario.steps[0].request.headers["X-Echo"] == REDACTION_MARKER
    assert run.results[0].assertions[0].actual == REDACTION_MARKER
    assert SECRET not in run.model_dump_json()
    assert SECRET not in repr(run)


@pytest.mark.asyncio
async def test_runner_adds_direct_request_secrets_to_step_scoped_redaction() -> None:
    transport = StubTransport([_response(body=f'{{"echo":"{SECRET}"}}')])
    scenario = ApiScenario(
        name="请求安全",
        steps=(
            ApiStep(
                name="echo",
                request=ApiRequest(
                    method="GET",
                    url="https://example.test/echo",
                    headers={"Authorization": f"Bearer {SECRET}"},
                ),
                assertions=(
                    ApiAssertion(kind="json_path", location="$.echo", expected="different"),
                ),
            ),
        ),
    )

    run = await ApiRunner(transport, Redactor.with_configured_values([])).run(scenario, {})

    assert transport.requests[0].headers["Authorization"] == f"Bearer {SECRET}"
    assert run.scenario.steps[0].request.headers["Authorization"] == (f"Bearer {REDACTION_MARKER}")
    assert run.results[0].assertions[0].actual == REDACTION_MARKER
    assert SECRET not in run.model_dump_json()
    assert SECRET not in repr(run)


@pytest.mark.asyncio
async def test_runner_collects_identity_timestamps_and_non_negative_elapsed_time() -> None:
    run = await ApiRunner(StubTransport([_response()]), Redactor.with_configured_values([])).run(
        ApiScenario(name="metadata", steps=(_step("one"),)), {}
    )

    assert run.id
    assert run.started_at.tzinfo is UTC
    assert run.ended_at is not None
    assert run.ended_at.tzinfo is UTC
    assert run.ended_at >= run.started_at
    assert run.elapsed_ms >= 0
    assert run.results[0].elapsed_ms >= 0


@pytest.mark.asyncio
async def test_runner_never_persists_nested_json_path_ancestor_secrets() -> None:
    nested_secret = "CSBOX_NESTED_SECRET_SENTINEL_runner"
    body = json.dumps({"token": {"nested": nested_secret}})
    transport = StubTransport(
        [
            ApiResponse(
                status_code=200,
                headers={"Content-Type": "application/json"},
                body=body,
                url="https://example.test/result",
                content_type="application/json",
                response_size=len(body.encode()),
            )
        ]
    )
    scenario = ApiScenario(
        name="嵌套响应安全",
        steps=(
            ApiStep(
                name="nested",
                request=ApiRequest(method="GET", url="https://example.test/nested"),
                assertions=(
                    ApiAssertion(
                        kind="json_path",
                        location="$.token.nested",
                        expected="different",
                    ),
                ),
            ),
        ),
    )

    run = await ApiRunner(transport, Redactor.with_configured_values([])).run(scenario, {})

    assertion = run.results[0].assertions[0]
    assert run.status == "FAIL"
    assert assertion.actual == REDACTION_MARKER
    assert nested_secret not in run.model_dump_json()
    assert nested_secret not in repr(run)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("expected", "expected_status"),
    [
        ("CSBOX_NESTED_SECRET_SENTINEL_configured_key_runner", "PASS"),
        ("different", "FAIL"),
    ],
)
async def test_runner_redacts_configured_secret_json_key_and_nested_response_value(
    expected: str, expected_status: str
) -> None:
    configured_key_secret = "CSBOX_CONFIGURED_KEY_SECRET_SENTINEL_runner"
    nested_secret = "CSBOX_NESTED_SECRET_SENTINEL_configured_key_runner"
    body = json.dumps({configured_key_secret: {"nested": nested_secret}})
    raw_response = ApiResponse(
        status_code=200,
        headers={"Content-Type": "application/json"},
        body=body,
        url="https://example.test/result",
        content_type="application/json",
        response_size=len(body.encode()),
    )
    transport = StubTransport([raw_response])
    scenario = ApiScenario(
        name="配置键响应脱敏",
        steps=(
            ApiStep(
                name="configured-key",
                request=ApiRequest(method="GET", url="https://example.test/configured-key"),
                assertions=(
                    ApiAssertion(
                        kind="json_path",
                        location=f"$['{configured_key_secret}'].nested",
                        expected=expected,
                    ),
                ),
            ),
        ),
    )

    run = await ApiRunner(transport, Redactor.with_configured_values([configured_key_secret])).run(
        scenario, {}
    )

    stored_response = run.results[0].response
    assertion = run.results[0].assertions[0]
    assert raw_response.body == body
    assert nested_secret in raw_response.body
    assert run.status == expected_status
    assert assertion.status == expected_status
    assert stored_response is not None
    assert json.loads(stored_response.body) == {REDACTION_MARKER: REDACTION_MARKER}
    for sentinel in (configured_key_secret, nested_secret):
        assert sentinel not in stored_response.body
        assert sentinel not in run.model_dump_json()
        assert sentinel not in repr(run)
        assert sentinel not in assertion.message
