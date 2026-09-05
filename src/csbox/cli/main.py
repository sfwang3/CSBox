from __future__ import annotations

import json
import re
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from typer.core import TyperCommand, TyperGroup

from csbox import __version__
from csbox.api.cli import api_app
from csbox.check.messages import public_finding_message
from csbox.check.service import create_check_service
from csbox.core.display_width import display_width, truncate_cells
from csbox.core.environment import detect_environment
from csbox.core.safe_paths import safe_relative_path
from csbox.core.schema import with_schema_version
from csbox.evidence.repository import EvidenceSetRepository
from csbox.evidence.service import create_report_handoff_service
from csbox.lab.service import create_lab_service
from csbox.locales import Translator, load_locale
from csbox.pack.service import create_pack_service
from csbox.report.repository import ReportProfileRepository
from csbox.report.service import default_report_profile

_locale = load_locale()


class _LocalizedHelpMixin:
    def get_help_option(self, ctx: object):
        option = super().get_help_option(ctx)  # type: ignore[misc]
        if option is not None:
            option.help = _locale("cli.help.option")
        return option


class _LocalizedHelpCommand(_LocalizedHelpMixin, TyperCommand):
    pass


class _LocalizedHelpGroup(_LocalizedHelpMixin, TyperGroup):
    pass


app = typer.Typer(
    add_completion=False,
    add_help_option=False,
    cls=_LocalizedHelpGroup,
    help=_locale("cli.help"),
    invoke_without_command=True,
    name="csbox",
    no_args_is_help=False,
)
lab_app = typer.Typer(
    cls=_LocalizedHelpGroup,
    help="实验记录、回看和导出实验材料。",
    no_args_is_help=True,
)
report_app = typer.Typer(
    cls=_LocalizedHelpGroup,
    help="配置并导出用户提供的报告材料。",
    no_args_is_help=True,
)
app.add_typer(lab_app, name="lab")
app.add_typer(api_app, name="api")
app.add_typer(report_app, name="report")


def _yes_no(translator: Translator, value: bool) -> str:
    return translator("doctor.yes" if value else "doctor.no")


def _print_safe_failure(message: str, error: Exception, *, verbose: bool) -> None:
    Console(markup=False).print(message)
    if verbose:
        Console(markup=False, stderr=True).print(f"调试类型：{type(error).__name__}")
        Console(markup=False, stderr=True).print(f"调试异常链：{_format_exception_chain(error)}")


def _format_exception_chain(error: BaseException) -> str:
    parts: list[str] = []
    current: BaseException | None = error
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        keep_detail = isinstance(current, OSError) or any(
            hasattr(current, attribute) for attribute in ("cause", "debug_context")
        )
        detail = str(current).strip() if keep_detail else ""
        detail = _redact_debug_detail(detail)
        native_code = getattr(current, "winerror", None)
        if native_code is None:
            native_code = getattr(current, "errno", None)
        if native_code is not None:
            detail = (
                f"{detail} (native_code={native_code})" if detail else f"native_code={native_code}"
            )
        debug_context = getattr(current, "debug_context", ())
        if debug_context:
            safe_context = "; ".join(_redact_debug_detail(str(item)) for item in debug_context)
            detail = f"{detail} [{safe_context}]"
        parts.append(f"{type(current).__name__}: {detail}" if detail else type(current).__name__)

        linked = getattr(current, "cause", None)
        if not isinstance(linked, BaseException):
            linked = current.__cause__ or current.__context__
        current = linked
    return " -> ".join(parts)


_DEBUG_INPUT_VALUE = re.compile(r"input_value=[^\r\n]*")
_DEBUG_SECRET_ASSIGNMENT = re.compile(
    r"(?i)(?<![a-z0-9_-])"
    r"(?P<key>[a-z][a-z0-9_-]*(?:api[_-]?key|access[_-]?key|token|secret|password|passwd))"
    r"\s*[:=]\s*(?:\"[^\r\n\"]*\"|'[^\r\n']*'|[^\s,;]+)"
)


def _redact_debug_detail(value: str) -> str:
    value = _DEBUG_INPUT_VALUE.sub("input_value=<redacted>", value)
    return _DEBUG_SECRET_ASSIGNMENT.sub(r"\g<key>=<redacted>", value)


def _version_callback(value: bool) -> bool:
    if value:
        typer.echo(__version__)
        raise typer.Exit()
    return value


def _stream_is_tty(stream: object) -> bool:
    isatty = getattr(stream, "isatty", None)
    if not callable(isatty):
        return False
    try:
        return bool(isatty())
    except (OSError, ValueError):
        return False


def _require_interactive_stdio() -> None:
    if _stream_is_tty(sys.stdin) and _stream_is_tty(sys.stdout):
        return
    print(_locale("cli.non_interactive"), file=sys.stderr)
    raise typer.Exit(code=1)


@app.callback()
def _run_default(
    ctx: typer.Context,
    version: bool = typer.Option(
        False,
        "--version",
        callback=_version_callback,
        is_eager=True,
        help="显示 CSBox 版本。",
    ),
    show_help: Annotated[
        bool,
        typer.Option(
            "--help",
            is_eager=True,
            help=_locale("cli.help.option"),
        ),
    ] = False,
) -> None:
    del version
    if show_help:
        typer.echo(ctx.get_help())
        raise typer.Exit()
    if ctx.invoked_subcommand is not None:
        return
    _require_interactive_stdio()
    _run_tui_workflow(Path.cwd())


def _run_tui_workflow(
    project_dir: Path,
    *,
    environment: object | None = None,
    service: object | None = None,
    app_factory: Callable[..., object] | None = None,
) -> None:
    """Run Textual outside the terminal-owning Lab service boundary."""

    from csbox.tui.app import CSBoxApp, real_home_data_source
    from csbox.tui.lab_workflow import (
        ActiveLabSession,
        HomeNotice,
        LabStartRequest,
        ShellOption,
        lab_start_failure_notice,
        lab_status_notice,
        no_available_shell_message,
    )

    selected_environment = environment or detect_environment()
    selected_service = service or create_lab_service(project_dir)
    selected_app_factory = app_factory or CSBoxApp
    data_source = real_home_data_source(project_dir)
    notice: HomeNotice | None = None
    active_session: ActiveLabSession | None = None

    try:
        selected_service.repository.recover_stale_running()
    except (OSError, UnicodeError, ValueError):
        notice = HomeNotice("部分旧实验状态暂时无法恢复；仍可开始新的实验。", "warning")

    profiles = selected_service.available_shells()
    shell_options = tuple(ShellOption.from_profile(profile) for profile in profiles)
    shell_error = (
        None
        if shell_options
        else no_available_shell_message(getattr(selected_environment, "os_name", ""))
    )

    while True:
        tui = selected_app_factory(
            data_source=data_source,
            environment=selected_environment,
            locale=_locale,
            shell_options=shell_options,
            shell_error=shell_error,
            home_notice=notice,
            active_session=active_session,
            export_action=getattr(selected_service, "export", None),
        )
        request = tui.run()
        notice = getattr(tui, "home_notice", notice)
        active_session = getattr(tui, "active_session", active_session)
        if request is None:
            return
        if not isinstance(request, LabStartRequest):
            return
        active_session = None
        try:
            result = selected_service.start(
                request.experiment_name,
                shell=request.shell,
            )
        except Exception as error:
            notice = lab_start_failure_notice(error)
            continue
        notice = lab_status_notice(request.experiment_name, result.status)
        if result.status == "running":
            active_session = ActiveLabSession(
                paths=result.session,
                experiment_name=request.experiment_name,
            )


@app.command(cls=_LocalizedHelpCommand, help=_locale("cli.doctor.help"))
def doctor() -> None:
    translator = _locale
    environment = detect_environment()
    console = Console(markup=False)
    console.print(translator("doctor.title"))
    console.print(f"{translator('doctor.os')}: {environment.os_name} {environment.os_version}")
    console.print(f"{translator('doctor.python')}: {environment.python_version}")
    shell_name = environment.shell or translator("doctor.host_unknown")
    console.print(f"{translator('doctor.host_shell')}: {shell_name}")
    console.print(
        f"{translator('doctor.powershell51')}: "
        f"{_yes_no(translator, environment.powershell_51_available)}"
    )
    console.print(
        f"{translator('doctor.powershell7')}: "
        f"{_yes_no(translator, environment.powershell_7_available)}"
    )
    console.print(f"{translator('doctor.wsl')}: {_yes_no(translator, environment.is_wsl)}")
    console.print(
        f"{translator('doctor.terminal')}: "
        f"{environment.terminal_columns}×{environment.terminal_rows}"
    )
    available_shells, default_shell, can_start_lab, lab_detection_failed = (
        _doctor_lab_shell_status()
    )
    console.print(
        f"{translator('doctor.available_shells')}: "
        f"{'、'.join(available_shells) if available_shells else translator('doctor.none')}"
    )
    console.print(
        f"{translator('doctor.default_shell')}: {default_shell or translator('doctor.none')}"
    )
    console.print(f"{translator('doctor.lab')}: {_yes_no(translator, can_start_lab)}")
    if lab_detection_failed:
        console.print(translator("doctor.next.config"))
    elif can_start_lab:
        console.print(translator("doctor.next.start"))
    elif environment.os_name == "Windows":
        console.print(translator("doctor.next.windows"))
    else:
        console.print(translator("doctor.next.unix"))


def _doctor_lab_shell_status() -> tuple[tuple[str, ...], str | None, bool, bool]:
    """Use Lab's launchability boundary for Doctor's shell claims."""

    try:
        profiles = tuple(create_lab_service().available_shells())
    except Exception:
        return (), None, False, True
    available = tuple(
        display_name
        for profile in profiles
        if (display_name := str(getattr(profile, "display_name", "")))
    )
    return available, (available[0] if available else None), bool(available), False


@app.command(
    "check",
    cls=_LocalizedHelpCommand,
    help="交作业前检查项目结构、敏感文件、状态和可选构建。",
)
def check_project(
    root: Path | None = typer.Argument(None, help="待检查的项目目录。"),  # noqa: B008
    plain: bool = typer.Option(False, "--plain", help="输出纯文本结果。"),
    json_output: bool = typer.Option(False, "--json", help="输出稳定 JSON。"),
    build: bool = typer.Option(False, "--build", help="执行检测到的项目构建。"),
    deep: bool = typer.Option(False, "--deep", help="使用 gitleaks 执行深度 secret 扫描。"),
    verbose: bool = typer.Option(False, "--verbose", help="显示受控调试类型。"),
) -> None:
    if plain and json_output:
        raise typer.BadParameter("--plain 与 --json 不能同时使用。")
    try:
        project_root = root or Path(".")
        run_options: dict[str, bool] = {"build": build}
        if deep:
            run_options["deep"] = True
        report = create_check_service(project_root).run(project_root, **run_options)
    except Exception as error:
        _print_safe_failure(
            "发生了什么：检查项目失败。在哪里：项目目录或检查规则。"
            "怎么处理：检查路径、权限和配置后重试。",
            error,
            verbose=verbose,
        )
        raise typer.Exit(code=1) from error

    if json_output:
        payload = _check_json_payload(report)
        payload["status"] = report.status.value
        payload["exitCode"] = report.exit_code
        Console(markup=False, soft_wrap=True).print(_strict_json(with_schema_version(payload)))
    elif plain:
        _print_check_plain(report)
    else:
        from csbox.locales import load_locale
        from csbox.tui.app import CheckApp

        CheckApp(report=report, locale=load_locale()).run()
    if report.exit_code:
        raise typer.Exit(code=report.exit_code)


def _print_check_plain(report) -> None:
    console = Console(markup=False)
    console.print("项目：.")
    console.print(f"状态：{report.status.value}")
    deep_scan = getattr(report, "deep_scan", None)
    findings = [*report.findings]
    if deep_scan is not None:
        findings.append(deep_scan)
    for finding in (*findings, *report.builds):
        if finding is deep_scan:
            console.print(
                f"Deep secret scan    {finding.status.value} {public_finding_message(finding)}"
            )
            continue
        location = ""
        path = getattr(finding, "path", None)
        if path is not None:
            location = _safe_relative_location(report.root, path) or ""
            line = getattr(finding, "line", None)
            if line is not None and location:
                location += f":{line}"
        finding_category = getattr(finding, "category", None)
        category = f" [{finding_category}]" if finding_category else ""
        identifier = getattr(finding, "rule_id", getattr(finding, "adapter_id", "build"))
        sensitive_category = (
            finding_category
            if finding_category in {"env", "private-key", "hard-coded-secret"}
            else {
                "env-file": "env",
                "private-key": "private-key",
                "hard-coded-secret": "hard-coded-secret",
            }.get(identifier)
        )
        if (
            sensitive_category in {"env", "private-key", "hard-coded-secret"}
            and finding.status.value == "FAIL"
        ):
            console.print(f"{finding.status.value} {sensitive_category} {location}".rstrip())
            for message_line in public_finding_message(finding).splitlines()[1:]:
                console.print(f"  {message_line}")
            continue
        console.print(
            f"{finding.status.value} {identifier}{category} {location} {finding.message}".rstrip()
        )


def _strict_json(payload: dict[str, object]) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    )


def _safe_relative_location(root: Path, value: Path | str | None) -> str | None:
    if value is None:
        return None
    path = Path(value)
    resolved_root = Path(root).resolve(strict=False)
    if path.is_absolute():
        try:
            relative = path.resolve(strict=False).relative_to(resolved_root)
        except ValueError:
            return None
    else:
        try:
            relative = Path(safe_relative_path(path.as_posix()))
        except ValueError:
            return None
    return relative.as_posix() or "."


def _safe_finding_payload(
    finding: dict[str, object],
    *,
    root: Path | None = None,
) -> dict[str, object]:
    finding = dict(finding)
    if root is not None and finding.get("path") is not None:
        safe_path = _safe_relative_location(root, finding.get("path"))
        finding["path"] = safe_path
        if safe_path is None:
            finding["line"] = None
    category = finding.get("category")
    if category in {"env", "private-key", "hard-coded-secret"} and finding.get("status") == "FAIL":
        return {
            "path": finding.get("path"),
            "line": finding.get("line"),
            "category": category,
        }
    return finding


def _safe_command_payload(command: object, root: Path) -> list[object]:
    if not isinstance(command, list | tuple):
        return []
    result: list[object] = []
    for item in command:
        if not isinstance(item, str):
            result.append(item)
            continue
        safe_item = _safe_relative_location(root, item) if Path(item).is_absolute() else item
        result.append(safe_item if safe_item is not None else Path(item).name)
    return result


def _check_json_payload(report) -> dict[str, object]:
    payload = report.model_dump(mode="json")
    root = Path(getattr(report, "root", payload.get("root", ".")))
    payload["root"] = "."

    raw_findings = payload.get("findings", [])
    report_findings = tuple(getattr(report, "findings", ()))
    payload["findings"] = (
        [
            _safe_finding_payload(
                item,
                root=root,
            )
            for item in raw_findings
        ]
        if isinstance(raw_findings, list)
        else []
    )
    for index, _finding in enumerate(report_findings):
        if index < len(payload["findings"]):
            payload["findings"][index] = _safe_finding_payload(
                payload["findings"][index], root=root
            )

    raw_projects = payload.get("projects", [])
    projects = tuple(getattr(report, "projects", ()))
    if isinstance(raw_projects, list):
        for index, project in enumerate(projects):
            if index >= len(raw_projects):
                break
            raw_projects[index]["root"] = _safe_relative_location(root, project.root)

    raw_builds = payload.get("builds", [])
    builds = tuple(getattr(report, "builds", ()))
    if isinstance(raw_builds, list):
        for index, build in enumerate(builds):
            if index >= len(raw_builds):
                break
            raw_builds[index]["command"] = _safe_command_payload(build.command, root)
            project = raw_builds[index].get("project")
            if isinstance(project, dict):
                project["root"] = _safe_relative_location(root, build.project.root)

    deep_scan = getattr(report, "deep_scan", None)
    if deep_scan is None:
        payload["deep_scan"] = None
    elif isinstance(payload.get("deep_scan"), dict):
        payload["deep_scan"] = _safe_finding_payload(payload["deep_scan"], root=root)

    stats = getattr(report, "text_scan_stats", {})
    payload["text_scan_stats"] = dict(stats) if isinstance(stats, dict) else {}
    return payload


@lab_app.command(
    "list",
    cls=_LocalizedHelpCommand,
    help="列出本项目保存过的实验记录。",
)
def lab_list(
    json_output: bool = typer.Option(False, "--json", help="输出稳定 JSON。"),
) -> None:
    try:
        service = create_lab_service()
        sessions = service.list()
        payload = [
            {
                "id": summary.metadata.session_id,
                "name": summary.metadata.experiment_name,
                "status": summary.metadata.status,
                "startedAt": summary.metadata.started_at.isoformat(),
                "captures": summary.capture_count,
                "platform": summary.metadata.platform,
                "shell": summary.metadata.shell,
                "cwd": str(summary.metadata.cwd),
            }
            for summary in sessions
        ]
    except Exception as error:
        _print_safe_failure(
            "发生了什么：无法读取实验记录。在哪里：本地实验仓库。"
            "怎么处理：检查 .csbox 目录和权限后重试。",
            error,
            verbose=False,
        )
        raise typer.Exit(code=1) from error
    if json_output:
        console = Console(markup=False, soft_wrap=True)
        console.print(
            json.dumps(
                with_schema_version({"sessions": payload}),
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )
        return
    console = Console(markup=False)
    if not payload:
        console.print("暂无实验记录。")
        return
    for item in payload:
        console.print(_format_lab_list_line(item, console.width))


def _format_lab_list_line(item: dict[str, object], width: int) -> str:
    prefix = f"{truncate_cells(str(item['id']), 12, ellipsis='…')}  "
    summary = f"{item['status']}  关键画面：{item['captures']}  {item['shell']}"
    fixed_width = display_width(prefix) + 2 + display_width(summary) + 2
    available = max(0, width - fixed_width)
    name_width = available // 2
    cwd_width = available - name_width
    name = truncate_cells(str(item["name"]), name_width, ellipsis="…")
    cwd = truncate_cells(str(item["cwd"]), cwd_width, ellipsis="…")
    return f"{prefix}{name}  {summary}  {cwd}"


@lab_app.command(
    "start",
    cls=_LocalizedHelpCommand,
    help="开始一次终端实验并记录过程。",
)
def lab_start(
    name: str | None = typer.Argument(None, help="实验名称。"),
    shell: str | None = typer.Option(None, "--shell", help="powershell、pwsh、bash 或 zsh。"),
    verbose: bool = typer.Option(False, "--verbose", help="显示受控调试类型。"),
) -> None:
    console = Console(markup=False)
    advisory_printed = False
    try:
        service = create_lab_service()
        capture_advisory = getattr(service, "capture_advisory", None)
        if callable(capture_advisory):
            console.print(capture_advisory().message)
            advisory_printed = True
        result = service.start(name, shell=shell)
    except Exception as error:
        _print_safe_failure(
            _lab_start_failure_message(error),
            error,
            verbose=verbose,
        )
        raise typer.Exit(code=1) from error
    if not advisory_printed:
        console.print(result.advisory)
    status_label = {
        "completed": "完成",
        "running": "启动",
        "interrupted": "中断",
        "failed": "失败",
    }.get(result.status, result.status)
    console.print(f"实验已{status_label}：{result.session.root}")


def _lab_start_failure_message(error: BaseException) -> str:
    kind = getattr(error, "kind", None)
    if kind == "windows_terminal_unavailable":
        return (
            "发生了什么：未找到 Windows Terminal。在哪里：专用终端窗口发现阶段。"
            "怎么处理：安装或修复 Windows Terminal 后重试。"
        )
    if kind == "windows_terminal_launch_failure":
        return (
            "发生了什么：Windows Terminal 无法启动。在哪里：专用终端窗口启动阶段。"
            "怎么处理：运行 wt.exe 检查 Windows Terminal 后重试。"
        )
    if kind == "shell_executable_unlaunchable":
        return (
            "发生了什么：Shell 可执行文件无法启动。在哪里：PowerShell 启动阶段。"
            "怎么处理：重新安装 PowerShell，或选择另一个已安装的 Shell 后重试。"
        )
    if kind == "shell_executable_unavailable":
        return (
            "发生了什么：未找到 Shell 可执行文件。在哪里：Shell 发现阶段。"
            "怎么处理：安装 PowerShell，或使用 --shell 选择已安装的 Shell。"
        )
    if kind == "conpty_initialization_failure":
        return (
            "发生了什么：Windows ConPTY 无法初始化。在哪里：终端后端启动阶段。"
            "怎么处理：确认 Windows 版本与终端能力后重试。"
        )
    if kind == "runtime_backend_failure":
        return (
            "发生了什么：终端后端运行失败。在哪里：实验 Shell 运行阶段。"
            "怎么处理：重新启动实验；若仍失败，使用 --verbose 查看诊断。"
        )
    return (
        "发生了什么：实验无法启动。在哪里：当前 Shell 或实验目录。"
        "怎么处理：运行 csbox doctor，检查 Shell 配置和目录权限后重试。"
    )


@lab_app.command("_host", cls=_LocalizedHelpCommand, hidden=True)
def lab_host(
    intent: Annotated[Path, typer.Option("--intent", help="内部 dedicated-host intent。")],
    token: Annotated[str, typer.Option("--token", help="内部 dedicated-host token。")],
) -> None:
    """Run the private Windows Terminal host without writing diagnostics to the body."""

    from csbox.lab.windows_host import run_dedicated_lab_host

    try:
        return_code = run_dedicated_lab_host(intent, token)
    except Exception:
        return_code = 1
    raise typer.Exit(code=return_code)


@lab_app.command(
    "export",
    cls=_LocalizedHelpCommand,
    help="导出实验记录中的关键画面和实验材料。",
)
def lab_export(
    session: str = typer.Argument(..., help="会话 ID 或唯一前缀。"),
    output: Annotated[Path | None, typer.Option("--output", help="导出目录。")] = None,
    theme: Annotated[str, typer.Option("--theme", help="dark 或 light。")] = "dark",
    force: Annotated[bool, typer.Option("--force", help="允许刷新已存在导出目录。")] = False,
    verbose: Annotated[bool, typer.Option("--verbose", help="显示受控调试类型。")] = False,
) -> None:
    if theme not in {"dark", "light"}:
        raise typer.BadParameter("主题只能是 dark 或 light。", param_hint="--theme")
    console = Console(markup=False)
    try:
        result = create_lab_service().export(session, output, theme=theme, force=force)
    except Exception as error:
        _print_safe_failure(
            "发生了什么：实验导出失败。在哪里：PNG、Markdown 或会话文件。"
            "怎么处理：检查 session、字体和输出目录后重试；"
            "并发冲突文件会保留为同目录 .csbox-recovery-*.bak。",
            error,
            verbose=verbose,
        )
        raise typer.Exit(code=1) from error
    console.print(f"实验证据已导出：{result.destination}")


@report_app.command(
    "export",
    cls=_LocalizedHelpCommand,
    help="导出证据集和用户提供的报告材料。",
)
def report_export(
    evidence_set_id: str = typer.Argument(..., help="证据集 ID。"),
    output: Annotated[Path | None, typer.Option("--output", help="报告输出目录。")] = None,
    force: Annotated[bool, typer.Option("--force", help="允许刷新已存在报告目录。")] = False,
    verbose: Annotated[bool, typer.Option("--verbose", help="显示受控调试类型。")] = False,
) -> None:
    project_dir = Path.cwd()
    destination = (
        output
        if output is not None and output.is_absolute()
        else project_dir / (output or f"{evidence_set_id}-report")
    )
    try:
        evidence_repository = EvidenceSetRepository.from_cwd(project_dir)
        evidence_set = evidence_repository.load(evidence_set_id)
        profile = ReportProfileRepository.from_cwd(project_dir).load_or_default(
            evidence_set.evidence_set_id,
            default=default_report_profile(project_dir),
        )
        result = create_report_handoff_service(project_dir).export(
            evidence_set,
            destination,
            force=force,
            report_profile=profile,
        )
    except Exception as error:
        _print_safe_failure(
            "发生了什么：课程报告导出失败。在哪里：报告结构、证据来源或输出目录。"
            "怎么处理：检查证据集、报告配置和输出目录后重试。",
            error,
            verbose=verbose,
        )
        raise typer.Exit(code=1) from error
    console = Console(markup=False)
    console.print(f"课程报告已导出：{result.destination}")
    console.print(f"Markdown：{result.markdown}")
    console.print(f"DOCX：{result.docx}")
    console.print(f"图片：{len(result.images)}")


@lab_app.command(
    "review",
    cls=_LocalizedHelpCommand,
    help="回看之前的终端实验并补充关键画面。",
)
def lab_review(
    session: str | None = typer.Argument(None, help="会话 ID 或唯一前缀；省略则打开最近会话。"),
) -> None:
    from csbox.tui.app import ReviewApp
    from csbox.tui.screens.review import ReviewController

    try:
        service = create_lab_service()
        if session:
            paths = service.repository.resolve(session)
        else:
            latest = service.repository.latest()
            if latest is None:
                raise ValueError("暂无可回看的 session，请先运行 csbox lab start。")
            paths = latest.paths
        ReviewApp(controller=ReviewController.from_session(paths), locale=_locale).run()
    except Exception as error:
        _print_safe_failure(
            "发生了什么：无法打开实验回看。在哪里：实验会话或回看数据。"
            "怎么处理：检查 session 是否存在且完整后重试。",
            error,
            verbose=False,
        )
        raise typer.Exit(code=1) from error


@app.command(
    "pack",
    cls=_LocalizedHelpCommand,
    help="检查项目并安全打包提交文件。",
)
def pack_project(
    root: Path | None = typer.Argument(None, help="待打包的项目目录。"),  # noqa: B008
    output: Annotated[
        Path | None,
        typer.Option(
            "--output",
            help="输出路径：已存在目录使用默认 ZIP 文件名，其他路径视为最终 ZIP 文件。",
        ),
    ] = None,
    dry_run: bool = typer.Option(False, "--dry-run", help="只显示打包计划，不创建 ZIP。"),
    manifest: bool = typer.Option(False, "--manifest", help="在 ZIP 中写入 manifest.json。"),
    verify: bool = typer.Option(False, "--verify", help="重新打开 ZIP 校验条目。"),
    force: bool = typer.Option(False, "--force", help="允许覆盖已有 ZIP。"),
    plain: bool = typer.Option(False, "--plain", help="输出稳定纯文本结果。"),
    json_output: bool = typer.Option(False, "--json", help="输出稳定 JSON。"),
    verbose: bool = typer.Option(False, "--verbose", help="显示受控调试类型。"),
) -> None:
    if plain and json_output:
        raise typer.BadParameter("--plain 与 --json 不能同时使用。")
    project_root = root or Path(".")
    try:
        service = create_pack_service(project_root)
        if dry_run:
            plan = service.plan(
                project_root,
                destination=output,
                verify=verify,
                force=force,
                include_manifest=manifest,
            )
        else:
            report = service.pack(
                project_root,
                destination=output,
                verify=verify,
                force=force,
                include_manifest=manifest,
            )
    except Exception as error:
        _print_safe_failure(
            _pack_failure_message(error, project_root),
            error,
            verbose=verbose,
        )
        raise typer.Exit(code=1) from error
    if dry_run:
        if json_output:
            Console(markup=False, soft_wrap=True).print(
                _strict_json(with_schema_version(_pack_plan_payload(plan, project_root)))
            )
        else:
            _print_pack_plan(plan, project_root)
        if not _pack_plan_can_publish(plan):
            raise typer.Exit(code=1)
        return
    if json_output:
        Console(markup=False, soft_wrap=True).print(
            _strict_json(with_schema_version(_pack_report_payload(report, project_root)))
        )
        return
    console = Console(markup=False)
    console.print(f"打包完成：{_safe_pack_location(project_root, report.destination)}")
    console.print(
        f"文件数：{len(report.entries)}  源文件大小：{report.source_bytes}  "
        f"ZIP 大小：{report.archive_bytes}"
    )
    console.print(f"排除项：{len(report.excluded)}  已校验：{'是' if report.verified else '否'}")


def _pack_plan_payload(plan: object, root: Path) -> dict[str, object]:
    payload = plan.model_dump(mode="json")
    payload["mode"] = "dry-run"
    payload["source_root"] = "."
    payload["destination"] = _safe_pack_location(root, Path(payload["destination"]))
    payload["can_publish"] = _pack_plan_can_publish(plan)
    payload["blockers"] = _pack_plan_blockers(plan, root)
    return payload


def _pack_report_payload(report: object, root: Path) -> dict[str, object]:
    payload = report.model_dump(mode="json")
    payload["source_root"] = "."
    payload["destination"] = _safe_pack_location(root, Path(payload["destination"]))
    return payload


def _safe_pack_location(root: Path, destination: Path) -> str:
    destination = Path(destination)
    root_path = Path(root).resolve(strict=False)
    destination_path = destination.resolve(strict=False)
    try:
        return destination_path.relative_to(root_path).as_posix()
    except ValueError:
        try:
            return destination_path.relative_to(Path.cwd().resolve(strict=False)).as_posix()
        except ValueError:
            return destination.name or "."


def _print_pack_plan(plan: object, root: Path) -> None:
    console = Console(markup=False)
    warnings = tuple(getattr(plan, "warnings", ()))
    console.print("打包预览")
    console.print(f"项目：.  类型：{plan.project_type}")
    console.print(f"输出：{_safe_pack_location(root, plan.destination)}")
    console.print(
        f"included：{len(plan.included)}  excluded：{len(plan.excluded)}  "
        f"rejected：{len(plan.rejected)}  warnings：{len(warnings)}  "
        f"源文件大小：{plan.source_bytes}"
    )
    if _pack_plan_can_publish(plan):
        status = "状态：可以直接打包"
        if warnings:
            status += f"（有 {len(warnings)} 项警告；不阻塞打包）"
        if bool(getattr(plan, "output_exists", False)):
            status += "（将覆盖已有 ZIP）"
        console.print(status)
    else:
        console.print("状态：暂时不能打包")
        for blocker in _pack_plan_blockers(plan, root):
            console.print(f"阻塞项：{blocker}")
    console.print("included:")
    for item in plan.included:
        console.print(f"  + {item}")
    console.print("excluded:")
    for item in plan.excluded:
        console.print(f"  - {item}")
    console.print("rejected:")
    for item in plan.rejected:
        console.print(f"  ! {item}")
    console.print("warnings:")
    for item in warnings:
        console.print(f"  ? {item}")


def _pack_plan_can_publish(plan: object) -> bool:
    can_publish = getattr(plan, "can_publish", None)
    if isinstance(can_publish, bool):
        return can_publish
    rejected = tuple(getattr(plan, "rejected", ()))
    return not rejected and not (
        bool(getattr(plan, "output_exists", False)) and not bool(getattr(plan, "force", False))
    )


def _pack_plan_blockers(plan: object, root: Path) -> list[str]:
    blockers = [_safe_pack_blocker(root, item) for item in getattr(plan, "rejected", ())]
    if bool(getattr(plan, "output_exists", False)) and not bool(getattr(plan, "force", False)):
        blockers.append(f"目标文件已存在：{_safe_pack_location(root, Path(plan.destination))}")
    return blockers


_SAFE_PACK_RULES = frozenset(
    {
        "env",
        "private-key",
        "hard-coded-secret",
        "unsafe-path",
        "path-unsafe",
        "path-conflict",
        "check-failed",
    }
)
_SAFE_PACK_RULE_LABELS = {
    "env": "真实 .env",
    "private-key": "私钥",
    "hard-coded-secret": "硬编码 secret",
    "unsafe-path": "不安全路径",
    "path-unsafe": "不安全路径",
    "path-conflict": "路径冲突",
    "check-failed": "检查项目",
}


def _pack_failure_message(error: Exception, root: Path) -> str:
    kind = getattr(error, "kind", None)
    if kind in {None, "pack_failed"}:
        if isinstance(error, FileExistsError):
            kind = "destination_exists"
        elif isinstance(error, PermissionError):
            kind = "destination_permission"
        detail = str(error)
        if kind == "pack_failed" and "目标文件已存在" in detail:
            kind = "destination_exists"
        elif kind == "pack_failed" and (
            "敏感或不安全" in detail
            or "ZIP 条目路径不安全" in detail
            or "ZIP 条目存在路径冲突" in detail
        ):
            kind = "content_rejected"
        elif kind == "pack_failed" and (
            "ZIP 校验" in detail or "ZIP 条目" in detail or "manifest" in detail
        ):
            kind = "verify_failed"
        elif kind == "pack_failed" and "输出路径不可安全使用" in detail:
            kind = "destination_unsafe"
        elif kind == "pack_failed" and "输出路径不可用" in detail:
            kind = "destination_unavailable"
        elif kind == "pack_failed" and "输出文件无法写入" in detail:
            kind = "destination_permission"
        elif kind == "pack_failed" and "打包计划已变化" in detail:
            kind = "plan_changed"
        elif kind == "pack_failed" and ("源文件清单" in detail or "源文件在复制" in detail):
            kind = "source_changed"
        elif kind == "pack_failed" and "无法安全建立打包计划" in detail:
            kind = "plan_failed"
        elif kind == "pack_failed" and "pack 文件名模板无效" in detail:
            kind = "config_invalid"
        elif kind == "pack_failed" and (
            "打包失败，无法写入 ZIP" in detail or "未发布输出文件" in detail
        ):
            kind = "publish_failed"

    if kind == "destination_exists":
        path = _pack_error_location(root, getattr(error, "path", None))
        return (
            "发生了什么：目标文件已存在。\n"
            "具体原因：默认不会覆盖已有 ZIP。\n"
            f"影响位置：{path}\n"
            "下一步：使用 --force 覆盖，或修改 --output 后重试。"
        )
    if kind == "content_rejected":
        details = tuple(getattr(error, "details", ()))
        lines = [
            f"发生了什么：无法打包：发现 {len(details) or 1} 个阻塞项。",
            "具体原因：安全检查拒绝了敏感或不安全内容。",
            "影响位置：",
        ]
        if details:
            lines.extend(f"- {_safe_pack_blocker(root, item)}" for item in details[:8])
        else:
            lines.append("- 项目内安全检查")
        lines.append("下一步：先处理这些检查项，再重新预览并打包。")
        return "\n".join(lines)
    if kind == "verify_failed":
        return (
            "发生了什么：ZIP 已生成但验证未通过。\n"
            "具体原因：ZIP 内容与打包计划不一致。\n"
            "影响位置：临时交付文件未发布。\n"
            "下一步：检查项目内容后重新预览并打包。"
        )
    if kind == "destination_unavailable":
        path = _pack_error_location(root, getattr(error, "path", None))
        return (
            "发生了什么：输出路径不可用。\n"
            "具体原因：无法访问输出目录或检查目标文件。\n"
            f"影响位置：{path}\n"
            "下一步：检查目录是否存在、权限是否足够，或换一个输出路径后重试。"
        )
    if kind == "destination_unsafe":
        path = _pack_error_location(root, getattr(error, "path", None))
        return (
            "发生了什么：输出目标不能安全使用。\n"
            "具体原因：目标是符号链接、特殊文件或经过了不安全的路径。\n"
            f"影响位置：{path}\n"
            "下一步：换一个普通 ZIP 文件路径后重试。"
        )
    if kind == "destination_permission":
        path = _pack_error_location(root, getattr(error, "path", None))
        return (
            "发生了什么：无法写入 ZIP。\n"
            "具体原因：输出目录或目标文件权限不足。\n"
            f"影响位置：{path}\n"
            "下一步：检查输出目录权限，或换一个可写路径后重试。"
        )
    if kind == "plan_changed":
        details = tuple(getattr(error, "details", ()))
        changed = tuple(
            location
            for item in details
            if (location := _safe_relative_location(root, str(item))) is not None
        )
        if changed:
            affected = ", ".join(changed[:8])
            if len(changed) > 8:
                affected += f" 等 {len(changed)} 项"
        else:
            affected = _pack_error_location(root, getattr(error, "path", None))
        return (
            "发生了什么：打包计划已变化。\n"
            "具体原因：预览后项目文件或输出设置发生了变化。\n"
            f"影响位置：{affected}\n"
            "下一步：重新运行打包预览，确认计划后再发布。"
        )
    if kind == "source_changed":
        path = _pack_error_location(root, getattr(error, "path", None))
        return (
            "发生了什么：源文件在打包时发生变化。\n"
            "具体原因：文件清单或文件内容与预览时不一致。\n"
            f"影响位置：{path}\n"
            "下一步：停止正在修改项目的程序，重新预览并打包。"
        )
    if kind == "plan_failed":
        path = _pack_error_location(root, getattr(error, "path", root))
        return (
            "发生了什么：无法建立安全的打包计划。\n"
            "具体原因：项目目录或打包规则检查未完成。\n"
            f"影响位置：{path}\n"
            "下一步：检查项目路径、权限和配置后重新运行 --dry-run。"
        )
    if kind == "config_invalid":
        return (
            "发生了什么：输出文件名配置无效。\n"
            "具体原因：文件名模板无法生成安全的 ZIP 文件名。\n"
            "影响位置：打包配置。\n"
            "下一步：修正配置中的 pack 文件名模板后重试。"
        )
    if kind == "publish_failed":
        path = _pack_error_location(root, getattr(error, "path", None))
        return (
            "发生了什么：打包内容已准备，但 ZIP 未能发布。\n"
            "具体原因：写入或替换输出文件时发生系统错误。\n"
            f"影响位置：{path}\n"
            "下一步：检查输出目录权限和磁盘空间，或换一个输出路径后重试。"
        )
    return (
        "发生了什么：项目打包失败，未完成。\n"
        "具体原因：未能生成并发布 ZIP。\n"
        "影响位置（在哪里）：输出文件未发布。\n"
        "下一步（怎么处理）：检查项目内容、输出目录权限和磁盘空间后重试。"
    )


def _pack_error_location(root: Path, value: object) -> str:
    if value is None:
        return "输出文件"
    return _safe_pack_location(root, Path(str(value)))


def _safe_pack_blocker(root: Path, value: object) -> str:
    text = str(value)
    path_text, separator, rule = text.rpartition(":")
    if not separator or rule not in _SAFE_PACK_RULES:
        return "项目内安全检查阻塞项"
    if path_text == "project":
        location = "项目范围"
    else:
        location = _safe_relative_location(root, Path(path_text)) or "项目内路径"
    return f"{location}（{_SAFE_PACK_RULE_LABELS.get(rule, f'规则：{rule}')}）"


def main() -> None:
    _configure_utf8_stdio()
    app()


def _configure_utf8_stdio() -> None:
    """Keep localized CLI output writable on Windows legacy code pages."""

    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if not callable(reconfigure):
            continue
        encoding = (getattr(stream, "encoding", "") or "").lower().replace("-", "")
        if encoding == "utf8":
            continue
        try:
            reconfigure(encoding="utf-8")
        except (OSError, ValueError):
            continue
