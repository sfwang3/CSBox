import io
import sys
from importlib import import_module
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from csbox.cli.main import app
from csbox.core.models import EnvironmentSnapshot
from csbox.core.shell import BashProfile
from csbox.core.terminal import TerminalBackendError
from csbox.lab.models import SessionPaths
from csbox.lab.proxy import TerminalCleanupError
from csbox.lab.service import LabRunResult
from csbox.tui.lab_workflow import LabStartRequest

runner = CliRunner()
cli_module = import_module("csbox.cli.main")


class TTYStream(io.StringIO):
    def __init__(self, *, is_tty: bool) -> None:
        super().__init__()
        self._is_tty = is_tty

    def isatty(self) -> bool:
        return self._is_tty


def _environment() -> EnvironmentSnapshot:
    return EnvironmentSnapshot(
        os_name="Linux",
        os_version="test",
        python_version="3.12.3",
        shell="Bash",
        shell_executable="bash",
        powershell_51_available=False,
        powershell_7_available=False,
        is_wsl=False,
        terminal_columns=80,
        terminal_rows=24,
    )


class FakeTuiFactory:
    def __init__(self, results: list[LabStartRequest | None]) -> None:
        self.results = results
        self.calls: list[dict[str, object]] = []

    def __call__(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        result = self.results.pop(0)
        return type("FakeTui", (), {"run": lambda self: result})()


class FakeTuiRepository:
    def __init__(self) -> None:
        self.recovery_calls = 0

    def recover_stale_running(self) -> tuple[()]:
        self.recovery_calls += 1
        return ()


class FakeTuiLabService:
    def __init__(self, result: LabRunResult | BaseException) -> None:
        self.repository = FakeTuiRepository()
        self.result = result
        self.start_calls: list[tuple[str, str]] = []

    def available_shells(self) -> tuple[object, ...]:
        return (BashProfile,)

    def start(self, name: str, *, shell: str) -> LabRunResult:
        self.start_calls.append((name, shell))
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


def test_tui_workflow_cancel_does_not_start_a_lab(tmp_path: Path) -> None:
    service = FakeTuiLabService(
        LabRunResult(SessionPaths(tmp_path / "unused"), "completed", 0, "F12 Capture")
    )
    app_factory = FakeTuiFactory([None])

    cli_module._run_tui_workflow(
        tmp_path,
        environment=_environment(),
        service=service,
        app_factory=app_factory,
    )

    assert service.repository.recovery_calls == 1
    assert service.start_calls == []
    assert len(app_factory.calls) == 1


def test_tui_workflow_windows_ready_resumes_home_with_target_session(
    tmp_path: Path,
) -> None:
    paths = SessionPaths(tmp_path / ".csbox" / "sessions" / "ready-session")
    service = FakeTuiLabService(LabRunResult(paths, "running", None, "F12 Capture"))
    request = LabStartRequest("中文实验", "bash")
    app_factory = FakeTuiFactory([request, None])

    cli_module._run_tui_workflow(
        tmp_path,
        environment=_environment(),
        service=service,
        app_factory=app_factory,
    )

    assert service.repository.recovery_calls == 1
    assert service.start_calls == [("中文实验", "bash")]
    assert len(app_factory.calls) == 2
    resumed = app_factory.calls[1]
    assert resumed["active_session"].paths is paths  # type: ignore[union-attr]
    assert resumed["home_notice"].kind == "running"  # type: ignore[union-attr]


def test_tui_workflow_unix_completion_resumes_home_without_monitor(
    tmp_path: Path,
) -> None:
    paths = SessionPaths(tmp_path / ".csbox" / "sessions" / "completed-session")
    service = FakeTuiLabService(LabRunResult(paths, "completed", 0, "F12 Capture"))
    app_factory = FakeTuiFactory([LabStartRequest("编译原理实验", "bash"), None])

    cli_module._run_tui_workflow(
        tmp_path,
        environment=_environment(),
        service=service,
        app_factory=app_factory,
    )

    resumed = app_factory.calls[1]
    assert resumed["active_session"] is None
    assert resumed["home_notice"].kind == "completed"  # type: ignore[union-attr]


def test_tui_workflow_start_failure_returns_to_home_without_cli_instruction(
    tmp_path: Path,
) -> None:
    error = RuntimeError("private native detail")
    service = FakeTuiLabService(error)
    app_factory = FakeTuiFactory([LabStartRequest("操作系统实验", "bash"), None])

    cli_module._run_tui_workflow(
        tmp_path,
        environment=_environment(),
        service=service,
        app_factory=app_factory,
    )

    resumed = app_factory.calls[1]
    notice = resumed["home_notice"]
    assert notice.kind == "failed"  # type: ignore[union-attr]
    assert "private native detail" not in notice.message  # type: ignore[union-attr]
    assert "csbox" not in notice.message.lower()  # type: ignore[union-attr]


def test_tui_workflow_pre_ready_interrupt_does_not_fabricate_a_saved_session(
    tmp_path: Path,
) -> None:
    service = FakeTuiLabService(KeyboardInterrupt())
    app_factory = FakeTuiFactory([LabStartRequest("操作系统实验", "bash"), None])

    with pytest.raises(KeyboardInterrupt):
        cli_module._run_tui_workflow(
            tmp_path,
            environment=_environment(),
            service=service,
            app_factory=app_factory,
        )

    assert service.start_calls == [("操作系统实验", "bash")]
    assert len(app_factory.calls) == 1


@pytest.mark.parametrize(
    ("stdin_tty", "stdout_tty"),
    ((True, False), (False, True), (False, False)),
    ids=("stdout-non-tty", "stdin-non-tty", "both-non-tty"),
)
def test_no_arg_non_tty_refuses_before_tui(
    monkeypatch: pytest.MonkeyPatch,
    stdin_tty: bool,
    stdout_tty: bool,
) -> None:
    stdin = TTYStream(is_tty=stdin_tty)
    stdout = TTYStream(is_tty=stdout_tty)
    stderr = TTYStream(is_tty=False)
    monkeypatch.setattr(cli_module.sys, "stdin", stdin)
    monkeypatch.setattr(cli_module.sys, "stdout", stdout)
    monkeypatch.setattr(cli_module.sys, "stderr", stderr)
    monkeypatch.setattr(
        cli_module,
        "_run_tui_workflow",
        lambda *_args, **_kwargs: pytest.fail("non-TTY must not initialize the TUI"),
    )

    with pytest.raises(typer.Exit) as caught:
        cli_module._run_default(
            type("Context", (), {"invoked_subcommand": None})(),
            False,
        )

    assert caught.value.exit_code == 1
    assert stdout.getvalue() == ""
    assert "当前环境不是交互式终端" in stderr.getvalue()
    assert "csbox --help" in stderr.getvalue()
    assert "\x1b" not in stdout.getvalue()


def test_no_arg_tty_runs_existing_tui_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli_module.sys, "stdin", TTYStream(is_tty=True))
    monkeypatch.setattr(cli_module.sys, "stdout", TTYStream(is_tty=True))
    monkeypatch.setattr(cli_module.sys, "stderr", TTYStream(is_tty=True))
    calls: list[Path] = []
    monkeypatch.setattr(cli_module, "_run_tui_workflow", calls.append)

    cli_module._run_default(
        type("Context", (), {"invoked_subcommand": None})(),
        False,
    )

    assert calls == [Path.cwd()]


def test_no_arg_runner_non_tty_exits_one_without_stdout() -> None:
    result = runner.invoke(app, [])

    assert result.exit_code == 1
    assert result.stdout == ""
    assert "当前环境不是交互式终端" in result.stderr
    assert "csbox --help" in result.stderr


def test_cli_help_lists_doctor() -> None:
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    assert "doctor" in result.stdout


def test_top_level_help_is_task_first_and_uses_chinese_copy() -> None:
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    for line in (
        "CSBox — 计算机实验与项目交付工具",
        "直接运行 csbox",
        "csbox lab",
        "csbox check",
        "csbox pack",
        "csbox api",
        "csbox doctor",
    ):
        assert line in result.stdout
    assert "Show this message and exit" not in result.stdout
    for jargon in ("PTY", "ConPTY", "asciicast", "CaptureStore", "Pydantic", "Textual"):
        assert jargon not in result.stdout


def test_help_bypasses_non_tty_gate() -> None:
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    assert "CSBox" in result.stdout
    assert "当前环境不是交互式终端" not in result.stderr


def test_doctor_prints_the_requested_chinese_environment_checks() -> None:
    result = runner.invoke(app, ["doctor"])

    assert result.exit_code == 0
    for label in (
        "操作系统",
        "Python 版本",
        "当前 Shell",
        "Windows PowerShell 5.1",
        "PowerShell 7",
        "处于 WSL",
        "终端尺寸",
    ):
        assert label in result.stdout


def test_doctor_has_friendly_shell_labels_and_next_step(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli_module, "detect_environment", _environment)

    result = runner.invoke(app, ["doctor"])

    assert result.exit_code == 0
    assert "PowerShell 7" in result.stdout
    assert "Windows PowerShell 5.1" in result.stdout
    assert "检测到 pwsh.exe" not in result.stdout
    assert "可以开始实验" in result.stdout
    assert "直接运行 csbox" in result.stdout


def test_doctor_no_shell_prints_installation_guidance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = EnvironmentSnapshot(
        os_name="Linux",
        os_version="test",
        python_version="3.12.3",
        shell="",
        shell_executable=None,
        powershell_51_available=False,
        powershell_7_available=False,
        is_wsl=False,
        terminal_columns=80,
        terminal_rows=24,
    )
    monkeypatch.setattr(cli_module, "detect_environment", lambda: environment)

    result = runner.invoke(app, ["doctor"])

    assert result.exit_code == 0
    assert "未找到 Bash 或 Zsh" in result.stdout
    assert "Traceback" not in result.stdout


def test_explicit_check_bypasses_no_arg_tty_gate(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("# demo\n", encoding="utf-8")

    result = runner.invoke(app, ["check", str(tmp_path), "--plain"])

    assert result.exit_code == 0
    assert "当前环境不是交互式终端" not in result.stderr
    assert "README" in result.stdout


def test_cli_main_reconfigures_non_utf8_stdout_before_chinese_help(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = io.BytesIO()
    stream = io.TextIOWrapper(output, encoding="cp1252")
    monkeypatch.setattr(sys, "stdout", stream)
    monkeypatch.setattr(sys, "argv", ["csbox", "--help"])

    with pytest.raises(SystemExit) as exit_info:
        cli_module.main()

    stream.flush()
    assert exit_info.value.code == 0
    assert stream.encoding.lower().replace("-", "") == "utf8"
    assert "doctor" in output.getvalue().decode("utf-8")


def test_verbose_failure_keeps_native_exception_chain(capsys: pytest.CaptureFixture[str]) -> None:
    cause = OSError("The handle is invalid")
    cause.winerror = 6  # type: ignore[attr-defined]
    error = TerminalCleanupError(cause, 1)

    cli_module._print_safe_failure("failure", error, verbose=True)

    captured = capsys.readouterr()
    assert "调试类型：TerminalCleanupError" in captured.err
    assert "TerminalCleanupError" in captured.err
    verbose_error = captured.err.replace("\n", " ")
    assert "OSError:" in verbose_error
    assert "The handle is invalid" in verbose_error
    assert "native_code=6" in verbose_error


def test_verbose_failure_includes_bounded_backend_spawn_context(
    capsys: pytest.CaptureFixture[str],
) -> None:
    cause = OSError("not a valid Win32 application")
    cause.winerror = 193  # type: ignore[attr-defined]
    error = TerminalBackendError(
        "Shell 可执行文件无效或无法启动。",
        cause,
        kind="shell_executable_unlaunchable",
        debug_context=(
            r"resolved_executable=C:\Program Files\WindowsApps\PowerShell\pwsh.exe",
            r"cwd=E:\test\CSBox",
        ),
    )

    cli_module._print_safe_failure("failure", error, verbose=True)

    verbose_error = capsys.readouterr().err.replace("\n", " ")
    assert r"resolved_executable=C:\Program Files\WindowsApps\PowerShell\pwsh.exe" in verbose_error
    assert r"cwd=E:\test\CSBox" in verbose_error
