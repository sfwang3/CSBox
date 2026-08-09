import inspect

from csbox.core.terminal import (
    TerminalBackend,
    UnixPTYBackend,
    WindowsConPTYBackend,
    create_terminal_backend,
)


def test_terminal_backend_is_an_abstract_interface() -> None:
    assert inspect.isabstract(TerminalBackend)


def test_factory_selects_the_platform_backend_without_spawning() -> None:
    assert isinstance(create_terminal_backend(system="Linux"), UnixPTYBackend)
    assert isinstance(create_terminal_backend(system="Darwin"), UnixPTYBackend)
    assert isinstance(create_terminal_backend(system="Windows"), WindowsConPTYBackend)
