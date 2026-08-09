import inspect

import pytest

from csbox.core.terminal import TerminalBackend, UnixPTYBackend, WindowsConPTYBackend


def test_terminal_backend_is_an_abstract_interface() -> None:
    assert inspect.isabstract(TerminalBackend)


@pytest.mark.parametrize("backend_type", [WindowsConPTYBackend, UnixPTYBackend])
def test_reserved_terminal_adapters_are_explicitly_unimplemented(backend_type: type) -> None:
    backend = backend_type()

    with pytest.raises(NotImplementedError, match="not implemented"):
        backend.spawn(["bash"])
    with pytest.raises(NotImplementedError, match="not implemented"):
        backend.read()
    with pytest.raises(NotImplementedError, match="not implemented"):
        backend.write("demo")
    with pytest.raises(NotImplementedError, match="not implemented"):
        backend.resize(80, 24)
    with pytest.raises(NotImplementedError, match="not implemented"):
        backend.is_alive()
    with pytest.raises(NotImplementedError, match="not implemented"):
        backend.close()
