import pytest

from csbox.core.registry import (
    DuplicateRegistrationError,
    Registry,
    UnknownRegistrationError,
)


def test_registry_starts_empty_and_returns_immutable_items() -> None:
    registry = Registry[object]("example")

    assert registry.items() == ()


def test_registry_registers_and_gets_an_implementation() -> None:
    implementation = object()
    registry = Registry[object]("example")

    registry.register("demo", implementation)

    assert registry.has("demo") is True
    assert registry.get("demo") is implementation
    assert registry.items() == (("demo", implementation),)


def test_registry_rejects_blank_duplicate_and_unknown_keys() -> None:
    registry = Registry[object]("example")
    implementation = object()

    with pytest.raises(ValueError, match="key"):
        registry.register("  ", implementation)

    registry.register("demo", implementation)

    with pytest.raises(DuplicateRegistrationError, match="demo"):
        registry.register("demo", object())

    with pytest.raises(UnknownRegistrationError, match="missing"):
        registry.get("missing")
