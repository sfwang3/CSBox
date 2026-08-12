"""Build immutable, redacted API evidence views from persisted run data."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Iterator, Mapping
from typing import Any
from urllib.parse import parse_qsl, quote, quote_plus, urlsplit

from csbox.api.errors import ApiPersistenceError
from csbox.api.models import (
    ApiAssertion,
    ApiAssertionResult,
    ApiEvidence,
    ApiMultipartPart,
    ApiRequest,
    ApiRun,
    ApiRunResult,
    ApiStep,
)
from csbox.api.redaction import REDACTION_MARKER, Redactor
from csbox.core.safe_paths import safe_relative_path

_FILENAME_BUDGET = 240
_FILENAME_RE = re.compile(r"[<>:\"/\\|?*]+")
_RESERVED_WINDOWS_NAMES = frozenset(
    {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{index}" for index in range(1, 10)),
        *(f"LPT{index}" for index in range(1, 10)),
    }
)


class ApiEvidenceBuilder:
    """Convert one safe run into one safe evidence item per executed step."""

    @classmethod
    def from_run(cls, run: ApiRun) -> tuple[ApiEvidence, ...]:
        if not isinstance(run, ApiRun):
            raise TypeError("run must be an ApiRun")
        if len(run.results) > len(run.scenario.steps):
            raise _invalid_evidence("运行结果数量超过场景步骤数量。")

        redactor = _run_redactor(run)
        scenario_name = redactor.text(run.scenario.name)
        scenario_source = (
            None if run.scenario.source is None else redactor.text(run.scenario.source)
        )
        safe_run_id = redactor.text(run.id)
        items: list[ApiEvidence] = []
        for index, (step, result) in enumerate(
            zip(run.scenario.steps, run.results, strict=False), 1
        ):
            safe_step = _redacted_step(step, redactor)
            safe_result = _redacted_result(result, redactor)
            items.append(
                ApiEvidence(
                    run_id=safe_run_id,
                    scenario_name=scenario_name,
                    scenario_source=scenario_source,
                    step_index=index,
                    run_status=run.status,
                    started_at=run.started_at,
                    title=redactor.text(step.name),
                    request=safe_step.request,
                    response=safe_result.response,
                    result=safe_result,
                )
            )
        return tuple(items)


def redact_evidence(evidence: ApiEvidence) -> ApiEvidence:
    """Defensively reapply field, URL, and text redaction to an evidence item."""

    if not isinstance(evidence, ApiEvidence):
        raise TypeError("evidence must be an ApiEvidence")
    redactor = Redactor.with_configured_values(_evidence_secret_values(evidence))
    request = _redacted_request(evidence.request, redactor)
    result = None if evidence.result is None else _redacted_result(evidence.result, redactor)
    response = None if result is None else result.response
    if response is None and evidence.response is not None:
        response = evidence.response.redacted_copy(redactor)
    return ApiEvidence(
        run_id=None if evidence.run_id is None else redactor.text(evidence.run_id),
        scenario_name=(
            None if evidence.scenario_name is None else redactor.text(evidence.scenario_name)
        ),
        scenario_source=(
            None if evidence.scenario_source is None else redactor.text(evidence.scenario_source)
        ),
        step_index=evidence.step_index,
        run_status=evidence.run_status,
        started_at=evidence.started_at,
        title=redactor.text(evidence.title),
        request=request,
        response=response,
        result=result,
    )


def evidence_filename(title: str, index: int) -> str:
    """Return a safe, deterministic PNG filename component for one step."""

    normalized = unicodedata.normalize("NFKC", title).replace(REDACTION_MARKER, "redacted")
    characters: list[str] = []
    for character in normalized:
        category = unicodedata.category(character)
        if category.startswith("C"):
            continue
        if character.isspace() or character in '<>:"/\\|?*':
            characters.append("-")
        else:
            characters.append(character)
    slug = re.sub(r"-+", "-", "".join(characters)).strip(" .-_")
    slug = _FILENAME_RE.sub("-", slug)
    while ".." in slug:
        slug = slug.replace("..", "-")
    slug = re.sub(r"-+", "-", slug).strip(" .-_")
    if not slug:
        slug = f"step-{index}"
    if slug.split(".", maxsplit=1)[0].upper() in _RESERVED_WINDOWS_NAMES:
        slug = f"_{slug}"

    prefix = f"{index:02d}-"
    suffix = ".png"
    byte_budget = _FILENAME_BUDGET - len((prefix + suffix).encode())
    windows_budget = _windows_units(prefix + suffix)
    windows_budget = _FILENAME_BUDGET - windows_budget
    if (
        len((prefix + slug + suffix).encode()) > _FILENAME_BUDGET
        or _windows_units(slug) > windows_budget
    ):
        digest = hashlib.sha256(normalized.encode()).hexdigest()[:8]
        digest_suffix = f"-{digest}"
        byte_budget -= len(digest_suffix.encode())
        windows_budget -= _windows_units(digest_suffix)
        shortened: list[str] = []
        for character in slug:
            candidate = "".join(shortened) + character
            if len(candidate.encode()) > byte_budget or _windows_units(candidate) > windows_budget:
                break
            shortened.append(character)
        slug = "".join(shortened).rstrip(" .-_") + digest_suffix
    filename = f"{prefix}{slug}{suffix}"
    try:
        safe_relative_path(f"evidence/{filename}")
    except (AttributeError, TypeError, ValueError) as error:
        raise _invalid_evidence("证据文件名无效。") from error
    return filename


def _redacted_step(step: ApiStep, redactor: Redactor) -> ApiStep:
    return ApiStep(
        name=redactor.text(step.name),
        request=_redacted_request(step.request, redactor),
        assertions=tuple(_redacted_assertion(item, redactor) for item in step.assertions),
    )


def _redacted_request(request: ApiRequest, redactor: Redactor) -> ApiRequest:
    return ApiRequest(
        method=request.method,
        url=redactor.url(request.url),
        query=_redacted_mapping(request.query, redactor),
        headers=redactor.headers(request.headers),
        json_body=redactor.json_value(request.json_body),
        body=None if request.body is None else redactor.text(request.body),
        form=_redacted_mapping(request.form, redactor),
        multipart=tuple(
            ApiMultipartPart(name=redactor.text(part.name), value=redactor.text(part.value))
            for part in request.multipart
        ),
        timeout_seconds=request.timeout_seconds,
        follow_redirects=request.follow_redirects,
        verify_tls=request.verify_tls,
    )


def _redacted_result(result: ApiRunResult, redactor: Redactor) -> ApiRunResult:
    return ApiRunResult(
        step_name=redactor.text(result.step_name),
        status=result.status,
        response=None if result.response is None else result.response.redacted_copy(redactor),
        assertions=tuple(
            ApiAssertionResult(
                assertion=_redacted_assertion(item.assertion, redactor),
                status=item.status,
                message=redactor.text(item.message),
                actual=redactor.json_value(item.actual),
            )
            for item in result.assertions
        ),
        error=None if result.error is None else redactor.text(result.error),
        elapsed_ms=result.elapsed_ms,
    )


def _redacted_assertion(assertion: ApiAssertion, redactor: Redactor) -> ApiAssertion:
    return ApiAssertion(
        kind=redactor.text(assertion.kind),
        expected=redactor.json_value(assertion.expected),
        location=None if assertion.location is None else redactor.text(assertion.location),
        operator=None if assertion.operator is None else redactor.text(assertion.operator),
    )


def _redacted_mapping(values: Mapping[str, str], redactor: Redactor) -> dict[str, str]:
    redacted = redactor.json_value(dict(values))
    if not isinstance(redacted, dict):
        return {}
    return {str(name): str(value) for name, value in redacted.items()}


def _configured_values(run: ApiRun) -> tuple[str, ...]:
    return tuple(
        value for value in run.scenario.variables.values() if isinstance(value, str) and value
    )


def _run_redactor(run: ApiRun) -> Redactor:
    values = list(_configured_values(run))
    base = Redactor.with_configured_values(values)
    for step in run.scenario.steps:
        _collect_request_secrets(step.request, base, values)
    for result in run.results:
        if result.response is not None:
            _collect_response_secrets(result.response, base, values)
    return Redactor.with_configured_values(_unique_secret_values(values))


def _evidence_secret_values(evidence: ApiEvidence) -> tuple[str, ...]:
    values: list[str] = []
    base = Redactor.with_configured_values(())
    _collect_request_secrets(evidence.request, base, values)
    if evidence.response is not None:
        _collect_response_secrets(evidence.response, base, values)
    return _unique_secret_values(values)


def _collect_request_secrets(request: ApiRequest, redactor: Redactor, values: list[str]) -> None:
    _collect_url_secrets(request.url, redactor, values)
    _collect_named_secrets(request.headers.items(), redactor, values)
    _collect_named_secrets(request.query.items(), redactor, values)
    _collect_named_secrets(request.form.items(), redactor, values)
    _collect_json_secrets(request.json_body, redactor, values)
    _collect_named_secrets(
        ((part.name, part.value) for part in request.multipart),
        redactor,
        values,
    )
    if request.body:
        _collect_text_secrets(request.body, redactor, values)


def _collect_response_secrets(response: Any, redactor: Redactor, values: list[str]) -> None:
    _collect_url_secrets(response.url, redactor, values)
    _collect_named_secrets(response.headers.items(), redactor, values)
    if response.body:
        if response.content_type and _is_json_content_type(response.content_type):
            try:
                parsed = json.loads(response.body, parse_constant=_reject_json_constant)
            except (TypeError, ValueError, json.JSONDecodeError, RecursionError):
                parsed = None
            _collect_json_secrets(parsed, redactor, values)
        _collect_text_secrets(response.body, redactor, values)


def _collect_url_secrets(url: str, redactor: Redactor, values: list[str]) -> None:
    try:
        parsed = urlsplit(url)
    except ValueError:
        return
    _append_secret(values, parsed.username)
    _append_secret(values, parsed.password)
    _collect_named_secrets(parse_qsl(parsed.query, keep_blank_values=True), redactor, values)


def _collect_named_secrets(items: Any, redactor: Redactor, values: list[str]) -> None:
    for name, value in items:
        if redactor.is_sensitive_field_name(str(name)):
            _collect_scalar_secrets(value, values)


def _collect_json_secrets(
    value: Any,
    redactor: Redactor,
    values: list[str],
    *,
    sensitive: bool = False,
) -> None:
    pending = [iter(((value, sensitive),))]
    while pending:
        try:
            current, current_sensitive = next(pending[-1])
        except StopIteration:
            pending.pop()
            continue
        if isinstance(current, Mapping):
            pending.append(_mapping_children(current, current_sensitive, redactor))
        elif isinstance(current, (list, tuple)):
            pending.append(_sequence_children(current, current_sensitive))
        elif current_sensitive:
            _collect_scalar_secrets(current, values)


def _mapping_children(
    value: Mapping[Any, Any], sensitive: bool, redactor: Redactor
) -> Iterator[tuple[Any, bool]]:
    for name, nested in value.items():
        yield nested, sensitive or redactor.is_sensitive_field_name(str(name))


def _sequence_children(
    value: list[Any] | tuple[Any, ...], sensitive: bool
) -> Iterator[tuple[Any, bool]]:
    for nested in value:
        yield nested, sensitive


def _collect_text_secrets(value: str, redactor: Redactor, values: list[str]) -> None:
    _collect_named_secrets(parse_qsl(value, keep_blank_values=True), redactor, values)


def _collect_scalar_secrets(value: object, values: list[str]) -> None:
    if isinstance(value, str) and value:
        _append_secret(values, value)
    elif isinstance(value, (list, tuple)):
        for nested in value:
            _collect_scalar_secrets(nested, values)


def _append_secret(values: list[str], value: str | None) -> None:
    if not value:
        return
    values.append(value)
    try:
        values.extend((quote(value, safe=""), quote_plus(value, safe="")))
    except (UnicodeError, ValueError):
        return


def _unique_secret_values(values: list[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(value for value in values if value))


def _is_json_content_type(content_type: str) -> bool:
    media_type = content_type.partition(";")[0].strip().casefold()
    return media_type == "application/json" or media_type.endswith("+json")


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-standard JSON constant: {value}")


def _windows_units(value: str) -> int:
    return len(value.encode("utf-16-le")) // 2


def _invalid_evidence(message: str) -> ApiPersistenceError:
    return ApiPersistenceError(
        f"发生了什么：{message}在哪里：API Evidence。怎么处理：检查已保存的脱敏运行记录后重试。"
    )


__all__ = ["ApiEvidenceBuilder", "evidence_filename", "redact_evidence"]
