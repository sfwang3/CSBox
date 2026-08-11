from __future__ import annotations

import pytest


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--shell-kind",
        choices=("powershell", "pwsh"),
        default=None,
        help="选择 Windows 原生 Shell smoke 测试：PowerShell 5.1 或 PowerShell 7。",
    )


@pytest.fixture
def shell_kind(request: pytest.FixtureRequest) -> str | None:
    return request.config.getoption("--shell-kind")
