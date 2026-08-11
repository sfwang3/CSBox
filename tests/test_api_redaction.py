from __future__ import annotations

import json
from urllib.parse import parse_qs, urlsplit

import pytest

from csbox.api.redaction import RedactionPolicy, Redactor

SECRET = "CSBOX_SECRET_SENTINEL_9f4d"
MASK = "••••••••"
SECRET_FIELDS = (
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
)


def _redactor() -> Redactor:
    return Redactor(
        RedactionPolicy.default(
            secret_values=(SECRET, f"prefix-{SECRET}"),
            additional_secret_fields=("x-extra-secret",),
        )
    )


def test_redactor_headers_is_case_insensitive_and_never_mutates_input() -> None:
    headers = {
        "authorization": f"Bearer {SECRET}",
        "Cookie": f"session={SECRET}",
        "set-cookie": f"session={SECRET}",
        "Proxy-Authorization": f"Basic {SECRET}",
        "X-Extra-Secret": SECRET,
        "X-Trace": f"prefix-{SECRET}",
    }

    redacted = _redactor().headers(headers)

    assert headers["authorization"] == f"Bearer {SECRET}"
    assert redacted["authorization"] == f"Bearer {MASK}"
    assert redacted["Cookie"] == MASK
    assert redacted["set-cookie"] == MASK
    assert redacted["Proxy-Authorization"] == MASK
    assert redacted["X-Extra-Secret"] == MASK
    assert redacted["X-Trace"] == MASK
    assert SECRET not in repr(redacted)


def test_redactor_headers_redacts_configured_secrets_in_names() -> None:
    headers = {
        f"X-{SECRET}": "trace-value",
        "Accept": "application/json",
    }

    redacted = _redactor().headers(headers)
    serialized = json.dumps({"response": {"headers": redacted}}, ensure_ascii=False)

    assert headers == {
        f"X-{SECRET}": "trace-value",
        "Accept": "application/json",
    }
    assert redacted == {
        f"X-{MASK}": "trace-value",
        "Accept": "application/json",
    }
    assert SECRET not in serialized


def test_redactor_headers_preserves_safe_name_and_suffixes_redacted_collisions() -> None:
    headers = {
        f"X-{SECRET}": "first-redacted-name",
        f"X-prefix-{SECRET}": "second-redacted-name",
        f"X-{MASK}": "safe-name-value",
        "Accept": "application/json",
    }

    redacted = _redactor().headers(headers)

    assert redacted == {
        f"X-{MASK}-2": "first-redacted-name",
        f"X-{MASK}-3": "second-redacted-name",
        f"X-{MASK}": "safe-name-value",
        "Accept": "application/json",
    }
    assert _redactor().headers(headers) == redacted
    assert SECRET not in json.dumps({"headers": redacted}, ensure_ascii=False)


def test_sensitive_field_names_normalize_separators_and_camel_case_across_surfaces() -> None:
    redactor = _redactor()
    field_secret = "unconfigured-field-secret"
    safe_value = "ordinary-value"
    headers = {
        "Proxy_Authorization": field_secret,
        "setCookie": field_secret,
        "X.Extra Secret": field_secret,
        "Token-Count": safe_value,
    }
    payload = {
        "accessToken": field_secret,
        "client-secret": field_secret,
        "token_count": safe_value,
        "nested": {"api key": field_secret, "safe.token.value": safe_value},
    }
    url = (
        f"https://example.test/a?refreshToken={field_secret}&api-key={field_secret}"
        f"&token_count={safe_value}&safe.token.value={safe_value}"
    )

    redacted_headers = redactor.headers(headers)
    redacted_payload = redactor.json_value(payload)
    redacted_query = parse_qs(urlsplit(redactor.url(url)).query)

    assert redacted_headers == {
        "Proxy_Authorization": MASK,
        "setCookie": MASK,
        "X.Extra Secret": MASK,
        "Token-Count": safe_value,
    }
    assert redacted_payload["accessToken"] == MASK
    assert redacted_payload["client-secret"] == MASK
    assert redacted_payload["nested"]["api key"] == MASK
    assert redacted_payload["token_count"] == safe_value
    assert redacted_payload["nested"]["safe.token.value"] == safe_value
    assert redacted_query["refreshToken"] == [MASK]
    assert redacted_query["api-key"] == [MASK]
    assert redacted_query["token_count"] == [safe_value]
    assert redacted_query["safe.token.value"] == [safe_value]


@pytest.mark.parametrize("field", SECRET_FIELDS)
def test_redactor_json_value_masks_all_default_secret_fields(field: str) -> None:
    payload = {
        field: SECRET,
        "nested": [{"safe": "value"}, {field.upper(): SECRET}],
        "items": [SECRET, {"x-extra-secret": SECRET}],
    }

    redacted = _redactor().json_value(payload)

    assert payload["nested"][1][field.upper()] == SECRET
    assert redacted[field] == MASK
    assert redacted["nested"][1][field.upper()] == MASK
    assert redacted["items"][0] == MASK
    assert redacted["items"][1]["x-extra-secret"] == MASK
    assert SECRET not in repr(redacted)


def test_redactor_json_value_redacts_configured_secrets_in_mapping_keys() -> None:
    payload = {f"key-{SECRET}": {"nested": ["safe", {"value": 1}]}}

    redacted = _redactor().json_value(payload)

    assert payload == {f"key-{SECRET}": {"nested": ["safe", {"value": 1}]}}
    assert redacted == {f"key-{MASK}": MASK}
    assert redacted is not payload
    assert SECRET not in json.dumps(redacted, ensure_ascii=False)


def test_redactor_json_value_preserves_safe_key_when_redacted_key_collides() -> None:
    payload = {
        f"key-{SECRET}": {"nested": ["redacted-name-value"]},
        f"key-{MASK}": {"nested": ["safe-name-value"]},
    }

    redacted = _redactor().json_value(payload)

    assert redacted == {
        f"key-{MASK}-2": MASK,
        f"key-{MASK}": {"nested": ["safe-name-value"]},
    }
    assert redacted is not payload
    assert redacted[f"key-{MASK}"] is not payload[f"key-{MASK}"]
    assert redacted[f"key-{MASK}"]["nested"] is not payload[f"key-{MASK}"]["nested"]
    assert _redactor().json_value(payload) == redacted
    assert SECRET not in json.dumps(redacted, ensure_ascii=False)


def test_redactor_json_value_suffixes_converging_redacted_keys() -> None:
    payload = {
        f"key-{SECRET}": {"source": "short-secret"},
        f"key-prefix-{SECRET}": {"source": "long-secret"},
    }

    redacted = _redactor().json_value(payload)

    assert redacted == {
        f"key-{MASK}": MASK,
        f"key-{MASK}-2": MASK,
    }
    assert len(redacted) == len(payload)
    assert _redactor().json_value(payload) == redacted
    assert SECRET not in json.dumps(redacted, ensure_ascii=False)


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (f'{{"to\\u006ben":"{SECRET}"}}', {"token": MASK}),
        (
            f'{{\n  "client_secret"\n  :\n  "{SECRET}"\n}}',
            {"client_secret": MASK},
        ),
    ],
)
def test_redactor_response_body_parses_json_before_masking_sensitive_fields(
    body: str, expected: dict[str, str]
) -> None:
    redacted = Redactor(RedactionPolicy.default()).response_body(body, "application/json")

    assert json.loads(redacted) == expected
    assert SECRET not in redacted


def test_redactor_response_body_falls_back_to_iterative_masker_for_malformed_json() -> None:
    body = f'{{"token":"{SECRET}"'

    redacted = Redactor(RedactionPolicy.default()).response_body(body, "application/json")

    assert redacted == f'{{"token":"{MASK}"'
    assert SECRET not in redacted


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (
            f'{{"to\\u006ben"\r\n:\n"{SECRET}",\n"safe"\n:\r\n"visible"',
            f'{{"to\\u006ben"\r\n:\n"{MASK}",\n"safe"\n:\r\n"visible"',
        ),
        (
            f'{{"client\\u005fsecret"\n:\r\n"{SECRET}","safe":"visible"',
            f'{{"client\\u005fsecret"\n:\r\n"{MASK}","safe":"visible"',
        ),
    ],
)
def test_redactor_response_body_fallback_decodes_escaped_keys_and_multiline_separators(
    body: str, expected: str
) -> None:
    redacted = Redactor(RedactionPolicy.default()).response_body(body, "application/json")

    assert redacted == expected
    assert SECRET not in redacted
    assert '"safe"' in redacted
    assert '"visible"' in redacted


def test_redactor_response_body_fallback_stays_iterative_for_deep_malformed_text() -> None:
    wrappers = "context=" * 2_000
    body = f'{{"safe":"{wrappers}"to\\u006ben"\n:\n"{SECRET}"'

    redacted = Redactor(RedactionPolicy.default()).response_body(body, "application/json")

    assert redacted == f'{{"safe":"{wrappers}"to\\u006ben"\n:\n"{MASK}"'
    assert SECRET not in redacted


def test_redactor_response_body_masks_configured_values_in_safe_json_fields() -> None:
    body = f'{{"safe":"{SECRET}"}}'

    redacted = _redactor().response_body(body, "application/problem+json; charset=utf-8")

    assert json.loads(redacted) == {"safe": MASK}
    assert SECRET not in redacted


def test_redactor_response_body_rejects_non_finite_json_numbers_before_fallback() -> None:
    body = '{"token":NaN}'

    redacted = Redactor(RedactionPolicy.default()).response_body(body, "application/json")

    assert redacted == f'{{"token":{MASK}}}'


def test_redactor_replaces_configured_values_longest_first_in_text() -> None:
    text = f"prefix-{SECRET} then {SECRET}"

    redacted = _redactor().text(text)

    assert redacted == f"{MASK} then {MASK}"
    assert SECRET not in redacted


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (f'{{"password":"{SECRET}"', f'{{"password":"{MASK}"'),
        (f"password={SECRET}", f"password={MASK}"),
        (f"token: {SECRET}", f"token: {MASK}"),
        (f"password={SECRET}/token: {SECRET}", f"password={MASK}/token: {MASK}"),
        (f"'access-token' = '{SECRET}'", f"'access-token' = '{MASK}'"),
        (f'token="{SECRET}\\', f'token="{MASK}\\'),
        (f'token="{SECRET}\\\nordinary text', f'token="{MASK}\\\nordinary text'),
    ],
)
def test_redactor_text_masks_response_only_sensitive_assignments(text: str, expected: str) -> None:
    redactor = Redactor(RedactionPolicy.default())

    redacted = redactor.text(text)

    assert redacted == expected
    assert SECRET not in redacted


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (f"context=token={SECRET}", f"context=token={MASK}"),
        (f'{{"context":"token={SECRET}"}}', f'{{"context":"token={MASK}"}}'),
        (f'{{"context":"token={SECRET}"', f'{{"context":"token={MASK}"'),
        (f'{{"context":"token={SECRET}', f'{{"context":"token={MASK}'),
    ],
)
def test_redactor_text_scans_safe_assignment_values_for_nested_secrets(
    text: str, expected: str
) -> None:
    redacted = Redactor(RedactionPolicy.default()).text(text)

    assert redacted == expected
    assert SECRET not in redacted


def test_redactor_text_does_not_scan_a_safe_assignments_key_for_nested_secrets() -> None:
    text = f'"token={SECRET}": safe-value'

    redacted = Redactor(RedactionPolicy.default()).text(text)

    assert redacted == text


def test_redactor_text_handles_arbitrarily_deep_safe_assignment_wrappers() -> None:
    wrappers = "context=" * 2_000
    text = f"{wrappers}token={SECRET}"

    redacted = Redactor(RedactionPolicy.default()).text(text)

    assert redacted == f"{wrappers}token={MASK}"


@pytest.mark.parametrize("separator", ["%3D", "%3d", "%3A", "%3a"])
def test_redactor_text_masks_response_only_percent_encoded_assignments(separator: str) -> None:
    encoded_secret = "response%20only%2Fsecret%2Bvalue"

    redacted = Redactor(RedactionPolicy.default()).text(f"token{separator}{encoded_secret}")

    assert redacted == f"token{separator}{MASK}"
    assert encoded_secret not in redacted


def test_redactor_text_does_not_broadly_match_sensitive_name_fragments() -> None:
    text = (
        f"tokenize={SECRET} not_token={SECRET} x-token={SECRET} safe.token={SECRET} "
        "tokenizer%3Dordinary not_token%3Dordinary x-token%3Dordinary "
        "safe.token%3Dordinary"
    )

    redacted = Redactor(RedactionPolicy.default()).text(text)

    assert redacted == text


def test_redactor_headers_use_sensitive_assignment_masking_for_safe_header_names() -> None:
    headers = {"X-Debug": f"token: {SECRET}", "X-Safe": f"not_token={SECRET}"}

    redacted = Redactor(RedactionPolicy.default()).headers(headers)

    assert redacted == {"X-Debug": f"token: {MASK}", "X-Safe": f"not_token={SECRET}"}
    assert headers == {"X-Debug": f"token: {SECRET}", "X-Safe": f"not_token={SECRET}"}


def test_redactor_headers_mask_nested_and_percent_encoded_assignments() -> None:
    encoded_secret = "response%20header%2Fsecret"
    headers = {
        "X-Nested": f"context=token={SECRET}",
        "X-Encoded": f"token%3D{encoded_secret}",
    }

    redacted = Redactor(RedactionPolicy.default()).headers(headers)

    assert redacted == {
        "X-Nested": f"context=token={MASK}",
        "X-Encoded": f"token%3D{MASK}",
    }
    assert headers == {
        "X-Nested": f"context=token={SECRET}",
        "X-Encoded": f"token%3D{encoded_secret}",
    }


def test_redactor_url_hides_credentials_and_sensitive_query_values() -> None:
    url = f"https://user:{SECRET}@example.test/a?token={SECRET}&safe=ok&access_token={SECRET}"

    redacted = _redactor().url(url)
    parsed = urlsplit(redacted)

    assert parsed.username == "user"
    assert parsed.password == MASK
    assert parse_qs(parsed.query)["token"] == [MASK]
    assert parse_qs(parsed.query)["access_token"] == [MASK]
    assert parse_qs(parsed.query)["safe"] == ["ok"]
    assert SECRET not in redacted


def test_redactor_url_does_not_apply_assignment_masking_to_path_or_safe_query_values() -> None:
    url = (
        "https://example.test/evidence/token=public-reference"
        "?safe=token%3Dpublic-reference&token=response-secret"
    )

    redacted = Redactor(RedactionPolicy.default()).url(url)
    parsed = urlsplit(redacted)

    assert parsed.path == "/evidence/token=public-reference"
    assert parse_qs(parsed.query) == {
        "safe": ["token=public-reference"],
        "token": [MASK],
    }


def test_redactor_url_still_exact_replaces_configured_secrets_in_path() -> None:
    url = f"https://example.test/evidence/{SECRET}?reference={SECRET}"

    redacted = _redactor().url(url)

    assert SECRET not in redacted
    assert urlsplit(redacted).path == f"/evidence/{MASK}"


def test_redactor_url_exact_replaces_a_configured_secret_in_the_port_component() -> None:
    redactor = Redactor.with_configured_values(["8443"])

    redacted = redactor.url("https://example.test:8443/evidence")

    assert "8443" not in redacted


def test_redactor_url_returns_safe_marker_for_credentials_without_hostname() -> None:
    url = f"https://user:credential-leak@?safe={SECRET}"

    redacted = _redactor().url(url)

    assert redacted == MASK
    assert "user" not in redacted
    assert "credential-leak" not in redacted
    assert SECRET not in redacted


@pytest.mark.parametrize(
    "url",
    [
        pytest.param(
            f"https://user:credential-leak@example.test:invalid/a?token=query-leak&safe={SECRET}",
            id="invalid-port",
        ),
        pytest.param(
            f"https://user:credential-leak@[invalid/a?accessToken=query-leak&safe={SECRET}",
            id="malformed-ipv6",
        ),
        pytest.param(
            f"https://user:credential-leak＠example.test/a?token=query-leak&safe={SECRET}",
            id="urlsplit-nfkc-validation",
        ),
    ],
)
def test_redactor_url_returns_safe_marker_when_url_validation_raises(url: str) -> None:
    redacted = _redactor().url(url)

    assert redacted == MASK
    assert "credential-leak" not in redacted
    assert "query-leak" not in redacted
    assert SECRET not in redacted
