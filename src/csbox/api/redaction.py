from __future__ import annotations

import json
import math
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

REDACTION_MARKER = "••••••••"

_DEFAULT_SECRET_FIELDS = frozenset(
    {
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
        "authorization",
        "cookie",
        "set-cookie",
        "proxy-authorization",
    }
)

_SENSITIVE_ASSIGNMENT_PREFIX_PATTERN = re.compile(
    r"""
    (?<![A-Za-z0-9_.-])
    (?P<key>
        "(?P<double_key>(?:\\[^\r\n]|[^"\\\r\n]){1,64})"
        |
        '(?P<single_key>(?:\\[^\r\n]|[^'\\\r\n]){1,64})'
        |
        (?P<bare_key>[A-Za-z][A-Za-z0-9_.-]{0,63})
    )
    (?P<separator>
        [ \t\r\n]*[:=][ \t\r\n]*
        |
        %(?:3[Dd]|3[Aa])(?:%20)*
    )
    (?=["']|[^\s,;&/}\]"'])
    """,
    re.VERBOSE,
)


def _normalize_field_name(name: str) -> str:
    """Fold case and common separators without matching partial field names."""

    return "".join(character for character in name.casefold() if character.isalnum())


@dataclass(frozen=True, slots=True)
class RedactionPolicy:
    """Names and exact values which must never leave the API domain raw."""

    secret_fields: frozenset[str] = _DEFAULT_SECRET_FIELDS
    secret_values: tuple[str, ...] = ()

    @classmethod
    def default(
        cls,
        *,
        secret_values: Iterable[str] = (),
        additional_secret_fields: Iterable[str] = (),
    ) -> RedactionPolicy:
        fields = _DEFAULT_SECRET_FIELDS | frozenset(
            field.casefold() for field in additional_secret_fields
        )
        values = tuple(value for value in secret_values if value)
        return cls(secret_fields=fields, secret_values=values)


class Redactor:
    """Return deep redacted copies without mutating caller-owned values."""

    def __init__(self, policy: RedactionPolicy) -> None:
        self.policy = policy
        self._secret_fields = frozenset(
            _normalize_field_name(field) for field in policy.secret_fields
        )
        self._secret_values = tuple(
            sorted({value for value in policy.secret_values if value}, key=len, reverse=True)
        )

    @classmethod
    def with_configured_values(cls, values: Iterable[str]) -> Redactor:
        return cls(RedactionPolicy.default(secret_values=values))

    def is_sensitive_field_name(self, name: str) -> bool:
        """Return whether a JSON or header field name must mask its value."""

        return (
            _normalize_field_name(name) in self._secret_fields
            or self._replace_secret_values(name) != name
        )

    def headers(self, headers: Mapping[str, str]) -> dict[str, str]:
        """Return a redacted copy of headers with case-insensitive names."""

        redacted: dict[str, str] = {}
        safe_names = {name for name in headers if self.text(name) == name}
        unavailable_names = set(safe_names)
        for name, value in headers.items():
            redacted_name = self.text(name)
            if redacted_name != name:
                base_name = redacted_name
                suffix = 2
                while redacted_name in unavailable_names:
                    redacted_name = f"{base_name}-{suffix}"
                    suffix += 1
                unavailable_names.add(redacted_name)

            normalized_name = _normalize_field_name(name)
            if normalized_name == _normalize_field_name("authorization"):
                redacted[redacted_name] = self._authorization(value)
            elif normalized_name in self._secret_fields:
                redacted[redacted_name] = REDACTION_MARKER
            else:
                redacted[redacted_name] = self.text(value)
        return redacted

    def json_value(self, value: Any) -> Any:
        """Return a recursively redacted copy of a JSON-compatible value."""

        if isinstance(value, Mapping):
            redacted: dict[str, Any] = {}
            safe_keys = {str(key) for key in value if self.text(str(key)) == str(key)}
            unavailable_keys = set(safe_keys)
            for key, nested_value in value.items():
                key_text = str(key)
                redacted_key = self.text(key_text)
                if redacted_key != key_text:
                    base_key = redacted_key
                    suffix = 2
                    while redacted_key in unavailable_keys:
                        redacted_key = f"{base_key}-{suffix}"
                        suffix += 1
                    unavailable_keys.add(redacted_key)

                redacted[redacted_key] = (
                    REDACTION_MARKER
                    if self.is_sensitive_field_name(key_text)
                    else self.json_value(nested_value)
                )
            return redacted
        if isinstance(value, list):
            return [self.json_value(item) for item in value]
        if isinstance(value, tuple):
            return tuple(self.json_value(item) for item in value)
        if isinstance(value, str):
            return self.text(value)
        return value

    def response_body(self, value: str, content_type: str | None) -> str:
        """Redact JSON structurally and use the iterative text masker as a fallback."""

        if not _is_json_content_type(content_type):
            return self.text(value)
        try:
            parsed = json.loads(
                value,
                parse_constant=_reject_json_constant,
                parse_float=_finite_json_float,
            )
            redacted = self.json_value(parsed)
            return json.dumps(
                redacted,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            )
        except (json.JSONDecodeError, RecursionError, TypeError, ValueError):
            return self.text(value)

    def text(self, value: str) -> str:
        """Mask configured values and sensitive text assignments without mutation."""

        return self._redact_sensitive_assignments(self._replace_secret_values(value))

    def _replace_secret_values(self, value: str) -> str:
        redacted = value
        for secret in self._secret_values:
            redacted = redacted.replace(secret, REDACTION_MARKER)
        return redacted

    def _redact_sensitive_assignments(self, value: str) -> str:
        redacted: list[str] = []
        position = 0
        while match := _SENSITIVE_ASSIGNMENT_PREFIX_PATTERN.search(value, position):
            redacted.append(value[position : match.end()])
            key = match.group("double_key", "single_key", "bare_key")
            key_text = next(candidate for candidate in key if candidate is not None)
            if match.group("bare_key") is None:
                key_text = _decode_quoted_key(key_text)
            if _normalize_field_name(key_text) not in self._secret_fields:
                position = match.end()
                continue

            value_end, quote, has_closing_quote = _assignment_value_end(value, match.end())
            if quote is None:
                redacted.append(REDACTION_MARKER)
            else:
                closing_quote = quote if has_closing_quote else ""
                redacted.append(f"{quote}{REDACTION_MARKER}{closing_quote}")
            position = value_end
        redacted.append(value[position:])
        return "".join(redacted)

    def url(self, value: str) -> str:
        """Redact structured URL secrets and exact values without interpreting paths."""

        try:
            parsed = urlsplit(value)
            query = []
            for name, query_value in parse_qsl(parsed.query, keep_blank_values=True):
                redacted_value = (
                    REDACTION_MARKER
                    if _normalize_field_name(name) in self._secret_fields
                    else self._replace_secret_values(query_value)
                )
                query.append((self._replace_secret_values(name), redacted_value))
            netloc = self._redacted_netloc(parsed)
            redacted = urlunsplit(
                (
                    self._replace_secret_values(parsed.scheme),
                    netloc,
                    self._replace_secret_values(parsed.path),
                    urlencode(query, doseq=True),
                    self._replace_secret_values(parsed.fragment),
                )
            )
        except ValueError:
            return REDACTION_MARKER
        return redacted

    def _authorization(self, value: str) -> str:
        scheme, separator, _credential = value.partition(" ")
        if scheme.casefold() == "bearer" and separator:
            return f"Bearer {REDACTION_MARKER}"
        return REDACTION_MARKER

    def _redacted_netloc(self, parsed: Any) -> str:
        if parsed.hostname is None:
            if "@" in parsed.netloc:
                raise ValueError("URL authority contains credentials without a hostname")
            return self._replace_secret_values(parsed.netloc)
        host = self._replace_secret_values(parsed.hostname)
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        if parsed.port is not None:
            host = f"{host}:{self._replace_secret_values(str(parsed.port))}"
        if parsed.username is None:
            return host
        username = quote(self._replace_secret_values(parsed.username), safe="")
        username = username.replace(quote(REDACTION_MARKER, safe=""), REDACTION_MARKER)
        password = "" if parsed.password is None else f":{REDACTION_MARKER}"
        return f"{username}{password}@{host}"


def _assignment_value_end(value: str, start: int) -> tuple[int, str | None, bool]:
    quote_character = value[start] if value[start] in {'"', "'"} else None
    if quote_character is None:
        position = start
        while position < len(value) and value[position] not in " \t\r\n,;&/}]\"'":
            position += 1
        return position, None, False

    position = start + 1
    while position < len(value) and value[position] not in "\r\n":
        if value[position] == "\\":
            if position + 1 == len(value) or value[position + 1] in "\r\n":
                return position, quote_character, False
            position += 2
            continue
        if value[position] == quote_character:
            return position + 1, quote_character, True
        position += 1
    return position, quote_character, False


def _decode_quoted_key(value: str) -> str:
    decoded: list[str] = []
    position = 0
    simple_escapes = {
        '"': '"',
        "'": "'",
        "/": "/",
        "\\": "\\",
        "b": "\b",
        "f": "\f",
        "n": "\n",
        "r": "\r",
        "t": "\t",
    }
    while position < len(value):
        if value[position] != "\\" or position + 1 == len(value):
            decoded.append(value[position])
            position += 1
            continue

        escape = value[position + 1]
        if escape == "u" and position + 6 <= len(value):
            code_point = value[position + 2 : position + 6]
            if all(character in "0123456789abcdefABCDEF" for character in code_point):
                decoded.append(chr(int(code_point, 16)))
                position += 6
                continue
        if escape in simple_escapes:
            decoded.append(simple_escapes[escape])
            position += 2
            continue
        decoded.extend(("\\", escape))
        position += 2
    return "".join(decoded)


def _is_json_content_type(content_type: str | None) -> bool:
    if content_type is None:
        return False
    media_type = content_type.partition(";")[0].strip().casefold()
    return media_type == "application/json" or media_type.endswith("+json")


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant: {value}")


def _finite_json_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError(f"non-finite JSON number: {value}")
    return parsed


__all__ = ["REDACTION_MARKER", "RedactionPolicy", "Redactor"]
