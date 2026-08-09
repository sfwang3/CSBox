from __future__ import annotations

from typing import Generic, TypeVar

T = TypeVar("T")


class RegistryError(Exception):
    """Base error for explicit in-process registries."""


class DuplicateRegistrationError(RegistryError):
    """Raised when a key is registered more than once."""


class UnknownRegistrationError(RegistryError):
    """Raised when a registry lookup has no matching key."""


class Registry(Generic[T]):
    """A small explicit registry with no discovery or lifecycle behavior."""

    def __init__(self, name: str) -> None:
        self.name = name
        self._items: dict[str, T] = {}

    def register(self, key: str, implementation: T) -> T:
        normalized_key = key.strip()
        if not normalized_key:
            raise ValueError("registry key must not be blank")
        if normalized_key in self._items:
            raise DuplicateRegistrationError(
                f"key {normalized_key!r} is already registered in {self.name!r}"
            )
        self._items[normalized_key] = implementation
        return implementation

    def get(self, key: str) -> T:
        try:
            return self._items[key]
        except KeyError as exc:
            raise UnknownRegistrationError(
                f"key {key!r} is not registered in {self.name!r}"
            ) from exc

    def has(self, key: str) -> bool:
        return key in self._items

    def items(self) -> tuple[tuple[str, T], ...]:
        return tuple(self._items.items())
