from __future__ import annotations

from pathlib import Path

from csbox.check.build import CommandResult
from csbox.check.models import CheckStatus
from csbox.check.service import CheckService


class FailingIfCalledRunner:
    def __init__(self) -> None:
        self.called = False

    def run(self, *args: object, **kwargs: object) -> CommandResult:
        del args, kwargs
        self.called = True
        raise AssertionError("build runner must not run without --build")


def test_check_does_not_build_by_default(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("# demo\n")
    (tmp_path / "pyproject.toml").write_text("[project]\nname='demo'\n")
    runner = FailingIfCalledRunner()

    report = CheckService(command_runner=runner).run(tmp_path)

    assert runner.called is False
    assert report.projects[0].kind == "python"


def test_check_build_appends_outcomes_and_never_exposes_raw_failure(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("# demo\n")
    (tmp_path / "pyproject.toml").write_text("[project]\nname='demo'\n")

    class Runner:
        def run(self, *args: object, **kwargs: object) -> CommandResult:
            del args, kwargs
            return CommandResult(returncode=1, stdout="secret build output", stderr="tool failed")

    report = CheckService(command_runner=Runner()).run(tmp_path, build=True)

    assert report.builds
    assert report.builds[0].status is CheckStatus.FAIL
    assert "secret build output" not in report.model_dump_json()
