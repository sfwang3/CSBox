from __future__ import annotations


class ApiDomainError(Exception):
    """A stable, user-safe API domain failure.

    ``debug_message`` is intentionally kept out of the exception string so
    default CLI, logs, and persistence paths cannot accidentally expose a raw
    transport/configuration value.
    """

    __slots__ = ("_debug_message", "_user_message")

    def __init__(self, user_message: str, *, debug_message: str | None = None) -> None:
        super().__init__(user_message)
        self._user_message = user_message
        self._debug_message = debug_message
        self.__suppress_context__ = True

    @property
    def user_message(self) -> str:
        """Stable Chinese-facing message used by all normal output paths."""

        return self._user_message

    @property
    def debug_message(self) -> str | None:
        """Technical detail available only through explicit verbose handling."""

        return self._debug_message

    @property
    def __cause__(self) -> None:
        """Exclude raw chained exceptions from Python's default formatting."""

        return None

    @__cause__.setter
    def __cause__(self, value: BaseException | None) -> None:
        # Callers must copy intentionally safe detail into ``debug_message``.
        del value

    @property
    def __context__(self) -> None:
        """Exclude raw implicit contexts from public exception inspection."""

        return None

    @__context__.setter
    def __context__(self, value: BaseException | None) -> None:
        # Python may still maintain internal chaining state while raising.
        del value

    def __str__(self) -> str:
        return self.user_message

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self.user_message!r})"


class ApiConfigError(ApiDomainError):
    """The scenario or API configuration cannot be used safely."""

    __slots__ = ()


class ApiTransportError(ApiDomainError):
    """The request could not be sent or its response could not be read."""

    __slots__ = ()


class ApiPersistenceError(ApiDomainError):
    """A redacted API run could not be persisted or loaded."""

    __slots__ = ()


__all__ = [
    "ApiConfigError",
    "ApiDomainError",
    "ApiPersistenceError",
    "ApiTransportError",
]
