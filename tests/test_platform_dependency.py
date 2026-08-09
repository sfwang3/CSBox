import tomllib
from pathlib import Path


def test_pywinpty_is_windows_only_and_terminal_core_imports_without_it() -> None:
    project_root = Path(__file__).parents[1]
    pyproject = tomllib.loads((project_root / "pyproject.toml").read_text(encoding="utf-8"))
    dependencies = pyproject["project"]["dependencies"]

    assert any(
        "pywinpty" in dependency and "sys_platform == 'win32'" in dependency
        for dependency in dependencies
    )

    from csbox.core import terminal

    assert terminal.TerminalBackend.__name__ == "TerminalBackend"
