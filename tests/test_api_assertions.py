from __future__ import annotations

import json

import pytest

import csbox.api.assertions as assertion_module
from csbox.api.assertions import evaluate_assertion, evaluate_assertions
from csbox.api.models import ApiAssertion, ApiResponse
from csbox.api.redaction import REDACTION_MARKER, Redactor

SECRET = "CSBOX_SECRET_SENTINEL_assertions"
OTHER_SECRET = "CSBOX_OTHER_SECRET_SENTINEL_assertions"
NESTED_SECRET = "CSBOX_NESTED_SECRET_SENTINEL_assertions"
LIST_SECRET = "CSBOX_LIST_SECRET_SENTINEL_assertions"
CONFIGURED_KEY_SECRET = "CSBOX_CONFIGURED_KEY_SENTINEL_assertions"


def _response(
    *,
    status_code: int = 200,
    headers: dict[str, str] | None = None,
    body: str = '{"status":"ok"}',
) -> ApiResponse:
    return ApiResponse(
        status_code=status_code,
        headers=headers or {"Content-Type": "application/json"},
        body=body,
        url="https://example.test/result",
        response_size=len(body.encode()),
    )


@pytest.mark.parametrize(
    ("assertion", "status_code", "expected_status"),
    [
        (ApiAssertion(kind="status", expected=200), 200, "PASS"),
        (ApiAssertion(kind="status", expected=201), 200, "FAIL"),
        (ApiAssertion(kind="status", expected="2xx"), 204, "PASS"),
        (ApiAssertion(kind="status", expected="2xx"), 302, "FAIL"),
        (ApiAssertion(kind="status", expected=[200, 299], operator="range"), 299, "PASS"),
        (ApiAssertion(kind="status", expected=[200, 299], operator="range"), 404, "FAIL"),
    ],
)
def test_status_assertions_support_exact_class_and_inclusive_range(
    assertion: ApiAssertion, status_code: int, expected_status: str
) -> None:
    result = evaluate_assertion(
        assertion,
        _response(status_code=status_code),
        Redactor.with_configured_values([]),
    )

    assert result.status == expected_status
    assert f"实际={status_code}" in result.message
    assert "位置=status" in result.message


def test_header_assertions_support_exists_and_case_insensitive_name_lookup() -> None:
    response = _response(headers={"content-TYPE": "application/json", "X-Trace": "trace-1"})
    redactor = Redactor.with_configured_values([])

    exists = evaluate_assertion(
        ApiAssertion(kind="header", location="CONTENT-type", operator="exists"),
        response,
        redactor,
    )
    equals = evaluate_assertion(
        ApiAssertion(kind="header", location="x-trace", expected="trace-1"),
        response,
        redactor,
    )
    missing = evaluate_assertion(
        ApiAssertion(kind="header", location="X-Missing", operator="exists"),
        response,
        redactor,
    )

    assert exists.status == "PASS"
    assert equals.status == "PASS"
    assert missing.status == "FAIL"
    assert "实际=不存在" in missing.message
    assert "位置=X-Missing" in missing.message


def test_json_path_assertions_support_exists_equals_and_type() -> None:
    response = _response(body='{"items":[{"id":1},{"id":2}],"active":true}')
    redactor = Redactor.with_configured_values([])

    results = evaluate_assertions(
        (
            ApiAssertion(kind="json_path", location="$.items[*].id", operator="exists"),
            ApiAssertion(kind="json_path", location="$.items[0].id", expected=1),
            ApiAssertion(
                kind="json_path",
                location="$.active",
                operator="type",
                expected="boolean",
            ),
        ),
        response,
        redactor,
    )

    assert tuple(result.status for result in results) == ("PASS", "PASS", "PASS")


def test_json_scalar_comparison_requires_exactly_one_match() -> None:
    result = evaluate_assertion(
        ApiAssertion(kind="json_path", location="$.items[*].id", expected=1),
        _response(body='{"items":[{"id":1},{"id":2}]}'),
        Redactor.with_configured_values([]),
    )

    assert result.status == "FAIL"
    assert "实际=匹配到 2 项" in result.message
    assert "位置=$.items[*].id" in result.message


@pytest.mark.parametrize(
    ("assertion", "body", "expected_status"),
    [
        (ApiAssertion(kind="json_path", location="$.missing", operator="exists"), "{}", "FAIL"),
        (ApiAssertion(kind="json_path", location="$[", expected=1), "{}", "SKIP"),
        (ApiAssertion(kind="json_path", location="$.value", expected=1), "not-json", "SKIP"),
    ],
)
def test_json_path_failures_are_safe_results_without_tracebacks(
    assertion: ApiAssertion, body: str, expected_status: str
) -> None:
    result = evaluate_assertion(
        assertion,
        _response(body=body),
        Redactor.with_configured_values([]),
    )

    assert result.status == expected_status
    assert result.message.startswith({"FAIL": "失败：", "SKIP": "跳过："}[expected_status])
    assert "Traceback" not in result.message
    assert assertion.location in result.message


def test_multiple_json_assertions_parse_the_response_once(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0
    original_loads = json.loads

    def counting_loads(value: str, **kwargs: object) -> object:
        nonlocal calls
        calls += 1
        return original_loads(value, **kwargs)

    monkeypatch.setattr(assertion_module.json, "loads", counting_loads)

    results = evaluate_assertions(
        (
            ApiAssertion(kind="json_path", location="$.value", expected=1),
            ApiAssertion(kind="json_path", location="$.value", operator="type", expected="integer"),
        ),
        _response(body='{"value":1}'),
        Redactor.with_configured_values([]),
    )

    assert tuple(result.status for result in results) == ("PASS", "PASS")
    assert calls == 1


@pytest.mark.parametrize(
    ("operator", "expected", "body", "expected_status"),
    [
        (None, "healthy", "service healthy", "PASS"),
        ("contains", "missing", "service healthy", "FAIL"),
        ("not_contains", "secret", "service healthy", "PASS"),
        ("not-contains", "healthy", "service healthy", "FAIL"),
    ],
)
def test_body_assertions_support_contains_and_not_contains(
    operator: str | None, expected: str, body: str, expected_status: str
) -> None:
    result = evaluate_assertion(
        ApiAssertion(kind="body", operator=operator, expected=expected),
        _response(body=body),
        Redactor.with_configured_values([]),
    )

    assert result.status == expected_status
    assert "位置=body" in result.message


def test_unsupported_assertion_configuration_returns_a_safe_failure() -> None:
    result = evaluate_assertion(
        ApiAssertion(kind="run_shell", expected="do not run"),
        _response(),
        Redactor.with_configured_values([]),
    )

    assert result.status == "FAIL"
    assert result.message.startswith("失败：")
    assert "配置" in result.message
    assert "Traceback" not in result.message


def test_assertion_result_redacts_expected_actual_location_message_repr_and_json() -> None:
    redactor = Redactor.with_configured_values([SECRET, OTHER_SECRET])
    result = evaluate_assertion(
        ApiAssertion(kind="json_path", location="$.value", expected=OTHER_SECRET),
        _response(body=json.dumps({"value": SECRET})),
        redactor,
    )

    serialized = result.model_dump_json()
    rendered = repr(result)
    assert result.status == "FAIL"
    assert result.actual == REDACTION_MARKER
    assert result.assertion.expected == REDACTION_MARKER
    assert "预期=••••••••" in result.message
    assert "实际=••••••••" in result.message
    assert "位置=$.value" in result.message
    for secret in (SECRET, OTHER_SECRET):
        assert secret not in result.message
        assert secret not in serialized
        assert secret not in rendered


def test_header_expected_and_actual_use_sensitive_header_context_for_redaction() -> None:
    result = evaluate_assertion(
        ApiAssertion(kind="header", location="Authorization", expected=f"Bearer {SECRET}"),
        _response(headers={"authorization": f"Bearer {OTHER_SECRET}"}),
        Redactor.with_configured_values([]),
    )

    serialized = result.model_dump_json()
    assert result.status == "FAIL"
    assert SECRET not in serialized
    assert OTHER_SECRET not in serialized
    assert "Bearer ••••••••" in result.message


def test_json_path_expected_and_actual_use_sensitive_path_context_for_redaction() -> None:
    result = evaluate_assertion(
        ApiAssertion(kind="json_path", location="$.token", expected=OTHER_SECRET),
        _response(body=json.dumps({"token": SECRET})),
        Redactor.with_configured_values([]),
    )

    serialized = result.model_dump_json()
    assert result.status == "FAIL"
    assert SECRET not in serialized
    assert OTHER_SECRET not in serialized
    assert result.actual == REDACTION_MARKER
    assert result.assertion.expected == REDACTION_MARKER


@pytest.mark.parametrize(
    ("location", "body", "configured_values", "expected", "expected_status", "secrets"),
    [
        (
            "$.token.nested",
            {"token": {"nested": NESTED_SECRET}},
            (),
            NESTED_SECRET,
            "PASS",
            (NESTED_SECRET,),
        ),
        (
            "$.token.nested",
            {"token": {"nested": NESTED_SECRET}},
            (),
            "different",
            "FAIL",
            (NESTED_SECRET,),
        ),
        (
            f"$['{CONFIGURED_KEY_SECRET}'].nested",
            {CONFIGURED_KEY_SECRET: {"nested": NESTED_SECRET}},
            (CONFIGURED_KEY_SECRET,),
            NESTED_SECRET,
            "PASS",
            (CONFIGURED_KEY_SECRET, NESTED_SECRET),
        ),
        (
            "$.items[0].token[0]",
            {"items": [{"token": [LIST_SECRET]}]},
            (),
            "different",
            "FAIL",
            (LIST_SECRET,),
        ),
    ],
)
def test_json_path_redacts_actual_when_any_ancestor_field_is_sensitive(
    location: str,
    body: object,
    configured_values: tuple[str, ...],
    expected: str,
    expected_status: str,
    secrets: tuple[str, ...],
) -> None:
    result = evaluate_assertion(
        ApiAssertion(kind="json_path", location=location, expected=expected),
        _response(body=json.dumps(body)),
        Redactor.with_configured_values(configured_values),
    )

    serialized = result.model_dump_json()
    rendered = repr(result)
    assert result.status == expected_status
    assert result.actual == REDACTION_MARKER
    assert "实际=••••••••" in result.message
    for secret in secrets:
        assert secret not in result.message
        assert secret not in serialized
        assert secret not in rendered


def test_json_path_equals_uses_raw_match_when_configured_secret_is_json_key() -> None:
    result = evaluate_assertion(
        ApiAssertion(kind="json_path", location=f"$['{SECRET}']", expected="visible"),
        _response(body=json.dumps({SECRET: "visible"})),
        Redactor.with_configured_values([SECRET]),
    )

    serialized = result.model_dump_json()
    assert result.status == "PASS"
    assert result.actual == REDACTION_MARKER
    assert SECRET not in result.message
    assert SECRET not in serialized
    assert SECRET not in repr(result)


def test_json_path_treats_non_standard_nan_as_invalid_json() -> None:
    result = evaluate_assertion(
        ApiAssertion(kind="json_path", location="$.value", operator="type", expected="number"),
        _response(body='{"value":NaN}'),
        Redactor.with_configured_values([]),
    )

    assert result.status == "SKIP"
    assert "无效 JSON" in result.message
