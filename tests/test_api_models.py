from __future__ import annotations

import copy
import json
import logging
import pickle
import traceback
from collections.abc import Callable
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from csbox.api import ApiMultipartPart
from csbox.api.errors import ApiConfigError, ApiPersistenceError, ApiTransportError
from csbox.api.models import (
    ApiAssertion,
    ApiAssertionResult,
    ApiEvidence,
    ApiRequest,
    ApiResponse,
    ApiRun,
    ApiRunResult,
    ApiScenario,
    ApiStep,
)
from csbox.api.redaction import Redactor

SECRET = "CSBOX_SECRET_SENTINEL_9f4d"


def test_api_domain_models_are_strict_and_form_a_serializable_run() -> None:
    request = ApiRequest(
        method="GET",
        url="https://example.test/health",
        headers={"Accept": "application/json"},
    )
    response = ApiResponse(
        status_code=200,
        headers={"Content-Type": "application/json"},
        body='{"status":"ok"}',
        url=request.url,
    )
    assertion = ApiAssertion(kind="status", expected=200)
    result = ApiAssertionResult(assertion=assertion, status="PASS", message="状态码符合预期")
    step = ApiStep(name="health", request=request, assertions=(assertion,))
    scenario = ApiScenario(name="health check", steps=(step,))
    run_result = ApiRunResult(step_name="health", response=response, assertions=(result,))
    run = ApiRun(
        id="run-001",
        scenario=scenario,
        started_at=datetime(2026, 8, 11, 0, 0, tzinfo=UTC),
        results=(run_result,),
    )
    evidence = ApiEvidence(title="health", request=request, response=response, result=run_result)

    assert run.results[0].response.status_code == 200
    assert run.results[0].response.response_size_exact is True
    assert evidence.title == "health"
    with pytest.raises(ValidationError):
        ApiRequest.model_validate({"method": "GET", "url": request.url, "unknown": True})
    with pytest.raises(ValidationError):
        ApiRequest.model_validate({"method": "GET", "url": request.url, "headers": []})


@pytest.mark.parametrize(
    "timeout_seconds",
    [0, 0.0, -1, -0.1, float("nan"), float("inf"), float("-inf")],
)
def test_api_request_rejects_non_finite_or_non_positive_timeout(timeout_seconds: float) -> None:
    with pytest.raises(ValidationError):
        ApiRequest(method="GET", url="https://example.test", timeout_seconds=timeout_seconds)


def test_json_facing_fields_accept_recursive_json_values_and_serialize() -> None:
    json_value = {
        "null": None,
        "bool": True,
        "int": 1,
        "float": 1.5,
        "string": "value",
        "list": [None, False, 2, 2.5, "nested", {"object": "value"}],
    }
    request = ApiRequest(method="POST", url="https://example.test", json_body=json_value)
    assertion = ApiAssertion(kind="json", expected=json_value)
    result = ApiAssertionResult(
        assertion=assertion,
        status="PASS",
        message="JSON 符合预期",
        actual=json_value,
    )

    assert request.model_dump_json()
    assert assertion.model_dump_json()
    assert result.model_dump_json()


def test_api_request_multipart_parts_have_a_strict_public_shape() -> None:
    request = ApiRequest(
        method="POST",
        url="https://example.test/upload",
        multipart=({"name": "attachment", "value": "report.pdf"},),
    )

    assert request.multipart == (ApiMultipartPart(name="attachment", value="report.pdf"),)
    assert request.model_dump(mode="json")["multipart"] == [
        {"name": "attachment", "value": "report.pdf"}
    ]

    with pytest.raises(ValidationError):
        ApiRequest(
            method="POST",
            url="https://example.test/upload",
            multipart=({"name": "attachment"},),
        )
    with pytest.raises(ValidationError):
        ApiRequest(
            method="POST",
            url="https://example.test/upload",
            multipart=({"name": "attachment", "value": "report.pdf", "command": "run_shell"},),
        )


@pytest.mark.parametrize(
    "build_model",
    [
        lambda value: ApiRequest(method="POST", url="https://example.test", json_body=value),
        lambda value: ApiAssertion(kind="json", expected=value),
        lambda value: ApiAssertionResult(
            assertion=ApiAssertion(kind="json"),
            status="FAIL",
            message="JSON 不符合预期",
            actual=value,
        ),
    ],
)
@pytest.mark.parametrize("value", [object(), ("tuple",), {"not-json": object()}, float("nan")])
def test_json_facing_fields_reject_non_json_values(
    build_model: Callable[[object], object], value: object
) -> None:
    with pytest.raises(ValidationError):
        build_model(value)


def test_redacted_model_json_never_serializes_a_secret() -> None:
    redactor = Redactor.with_configured_values([SECRET])
    request = ApiRequest(
        method="POST",
        url=redactor.url(f"https://user:{SECRET}@example.test/a?token={SECRET}"),
        headers=redactor.headers(
            {
                "Authorization": f"Bearer {SECRET}",
                f"X-{SECRET}": "trace-value",
            }
        ),
        json_body=redactor.json_value({"nested": [{"password": SECRET}]}),
    )
    response = ApiResponse(
        status_code=200,
        headers=redactor.headers({"Set-Cookie": f"session={SECRET}"}),
        body=redactor.text(f"body={SECRET}"),
        url=request.url,
    )
    result = ApiRunResult(step_name="redacted", response=response)
    run = ApiRun(
        id="run-redacted",
        scenario=ApiScenario(
            name="redacted",
            steps=(ApiStep(name="redacted", request=request),),
        ),
        started_at=datetime(2026, 8, 11, tzinfo=UTC),
        results=(result,),
    )

    assert SECRET not in run.model_dump_json()


def test_api_response_redacted_copy_is_the_safe_external_view() -> None:
    redactor = Redactor.with_configured_values([SECRET])
    response = ApiResponse(
        status_code=200,
        headers={"Set-Cookie": "session=unknown-cookie", "X-Echo": SECRET},
        body=f"ordinary response evidence; configured={SECRET}; token=unknown-token",
        url=(
            f"https://user:{SECRET}@example.test/evidence/token=public-reference/{SECRET}"
            "?safe=token%3Dpublic-reference&token=unknown-query-token"
        ),
        elapsed_ms=12.5,
        content_type="text/plain",
        response_size=120,
    )

    safe_response = response.redacted_copy(redactor)
    serialized = safe_response.model_dump_json()

    assert response.headers["X-Echo"] == SECRET
    assert SECRET in response.body
    assert SECRET in response.url
    assert SECRET not in serialized
    assert safe_response.headers == {"Set-Cookie": "••••••••", "X-Echo": "••••••••"}
    assert safe_response.body == ("ordinary response evidence; configured=••••••••; token=••••••••")
    assert "/evidence/token=public-reference/••••••••" in safe_response.url
    assert safe_response.elapsed_ms == response.elapsed_ms
    assert safe_response.response_size == response.response_size


def test_api_response_redacted_copy_masks_escaped_and_multiline_sensitive_json_keys() -> None:
    body = f'{{\n  "to\\u006ben"\n  :\n  "{SECRET}",\n  "safe": "visible"\n}}'
    response = ApiResponse(
        status_code=200,
        headers={"Content-Type": "application/json"},
        body=body,
        url="https://example.test/result",
        content_type="application/json; charset=utf-8",
        response_size=len(body.encode()),
    )

    safe_response = response.redacted_copy(Redactor.with_configured_values([]))
    serialized = safe_response.model_dump_json()

    assert response.body == body
    assert json.loads(safe_response.body) == {"token": "••••••••", "safe": "visible"}
    assert SECRET not in serialized
    assert SECRET not in repr(safe_response)


def test_api_response_redacted_copy_masks_malformed_escaped_multiline_json() -> None:
    body = f'{{\n  "to\\u006ben"\r\n  :\n  "{SECRET}",\n  "safe"\n  : "visible"'
    response = ApiResponse(
        status_code=200,
        headers={"Content-Type": "application/json"},
        body=body,
        url="https://example.test/result",
        content_type="application/json",
        response_size=len(body.encode()),
    )

    safe_response = response.redacted_copy(Redactor.with_configured_values([]))

    assert response.body == body
    assert safe_response.body == body.replace(SECRET, "••••••••")
    assert '"safe"\n  : "visible"' in safe_response.body
    assert SECRET not in safe_response.model_dump_json()
    assert SECRET not in repr(safe_response)


def test_api_response_assertion_view_stays_on_its_short_lived_original_only() -> None:
    raw_response = ApiResponse(
        status_code=200,
        body=f'{{"token":"{SECRET}"}}',
        url="https://example.test/raw",
        content_type="application/json",
    )
    public_response = ApiResponse(
        status_code=200,
        body='{"token":"••••••••"}',
        url="https://example.test/redacted",
        content_type="application/json",
    )

    public_response._attach_assertion_view(raw_response)

    assert public_response.assertion_view() is raw_response
    assert SECRET not in public_response.model_dump_json()
    assert SECRET not in repr(public_response)

    safe_copy = public_response.redacted_copy(Redactor.with_configured_values([]))
    assert safe_copy.assertion_view() is safe_copy
    assert SECRET not in safe_copy.model_dump_json()
    assert SECRET not in repr(safe_copy)

    pickled_response = pickle.dumps(public_response)
    assert SECRET.encode() not in pickled_response
    copies = (
        copy.deepcopy(public_response),
        public_response.model_copy(deep=True),
        pickle.loads(pickled_response),
    )
    for response_copy in copies:
        assert response_copy.assertion_view() is response_copy
        assert SECRET not in response_copy.model_dump_json()
        assert SECRET not in repr(response_copy)

    evidence = ApiEvidence(
        title="private assertion view",
        request=ApiRequest(method="GET", url="https://example.test/evidence"),
        response=public_response,
    )
    assert evidence.response is not None
    assert evidence.response is not public_response
    assert evidence.response.assertion_view() is evidence.response
    assert SECRET not in evidence.model_dump_json()
    assert SECRET not in repr(evidence)


@pytest.mark.parametrize("error_type", [ApiConfigError, ApiTransportError, ApiPersistenceError])
def test_domain_error_default_formatting_exposes_only_user_message(
    error_type: type[Exception],
) -> None:
    user_message = "接口配置无效，请检查后重试。"
    try:
        try:
            raise RuntimeError(f"cause={SECRET}")
        except RuntimeError:
            # Exercise implicit context; the domain error must suppress it by default.
            raise error_type(user_message, debug_message=f"debug={SECRET}")  # noqa: B904
    except error_type as error:
        captured_error = error
        formatted_traceback = "".join(traceback.format_exception(error))
        record = logging.LogRecord(
            name="csbox.api",
            level=logging.ERROR,
            pathname=__file__,
            lineno=0,
            msg="API failure",
            args=(),
            exc_info=(type(error), error, error.__traceback__),
        )
        formatted_log = logging.Formatter().format(record)

    assert captured_error.user_message == user_message
    assert captured_error.args == (user_message,)
    assert captured_error.debug_message == f"debug={SECRET}"
    assert str(captured_error) == user_message
    assert repr(captured_error) == f"{error_type.__name__}({user_message!r})"
    assert vars(captured_error) == {}
    assert SECRET not in repr(captured_error)
    assert SECRET not in formatted_traceback
    assert SECRET not in formatted_log
    assert captured_error.__cause__ is None
    assert captured_error.__context__ is None


def test_domain_error_discards_raw_explicit_cause_from_default_traceback() -> None:
    try:
        try:
            raise RuntimeError(f"cause={SECRET}")
        except RuntimeError as cause:
            raise ApiTransportError(
                "接口请求失败，请稍后重试。", debug_message=f"debug={SECRET}"
            ) from cause
    except ApiTransportError as error:
        captured_error = error
        formatted_traceback = "".join(traceback.format_exception(error))

    assert captured_error.__cause__ is None
    assert captured_error.__context__ is None
    assert captured_error.debug_message == f"debug={SECRET}"
    assert SECRET not in formatted_traceback
