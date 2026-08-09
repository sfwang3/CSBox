from __future__ import annotations

import json
from dataclasses import dataclass
from importlib import resources
from typing import Any


class MissingLocaleKey(KeyError):
    """Raised when a requested translation key is not in the active locale."""


@dataclass(frozen=True, slots=True)
class Translator:
    locale: str
    messages: dict[str, str]

    def __call__(self, key: str, **values: Any) -> str:
        try:
            message = self.messages[key]
        except KeyError as exc:
            raise MissingLocaleKey(key) from exc
        try:
            return message.format(**values)
        except KeyError as exc:
            raise MissingLocaleKey(f"{key}.{exc.args[0]}") from exc


def load_locale(locale: str = "zh_CN") -> Translator:
    resource = resources.files("csbox.locales").joinpath(f"{locale}.json")
    try:
        raw = resource.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise MissingLocaleKey(f"locale:{locale}") from exc
    messages = json.loads(raw)
    if not isinstance(messages, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in messages.items()
    ):
        raise TypeError(f"locale {locale!r} must contain a flat string mapping")
    return Translator(locale=locale, messages=messages)
