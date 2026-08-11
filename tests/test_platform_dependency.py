import tomllib
from pathlib import Path


def test_pywinpty_is_windows_only_and_terminal_core_imports_without_it() -> None:
    project_root = Path(__file__).parents[1]
    pyproject = tomllib.loads((project_root / "pyproject.toml").read_text(encoding="utf-8"))
    dependencies = pyproject["project"]["dependencies"]

    pywinpty_dependencies = [dependency for dependency in dependencies if "pywinpty" in dependency]
    assert len(pywinpty_dependencies) == 1
    pywinpty_dependency = pywinpty_dependencies[0]
    assert "sys_platform == 'win32'" in pywinpty_dependency
    assert ">=3.0" in pywinpty_dependency
    assert "<4" in pywinpty_dependency

    from csbox.core import terminal

    assert terminal.TerminalBackend.__name__ == "TerminalBackend"
