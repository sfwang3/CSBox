from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from csbox import __version__
from csbox.api.cli import api_app
from csbox.check.service import create_check_service
from csbox.core.display_width import display_width, truncate_cells
from csbox.core.environment import detect_environment
from csbox.core.schema import with_schema_version
from csbox.lab.service import create_lab_service
from csbox.locales import Translator, load_locale
from csbox.pack.service import PackServiceError, create_pack_service

_locale = load_locale()
app = typer.Typer(
    add_completion=False,
    help=_locale("cli.help"),
    invoke_without_command=True,
    name="csbox",
    no_args_is_help=False,
)
lab_app = typer.Typer(help="实验录制、回放和证据导出。", no_args_is_help=True)
app.add_typer(lab_app, name="lab")
app.add_typer(api_app, name="api")


def _yes_no(translator: Translator, value: bool) -> str:
    return translator("doctor.yes" if value else "doctor.no")


def _version_callback(value: bool) -> bool:
    if value:
        typer.echo(__version__)
        raise typer.Exit()
    return value


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
) -> None:
    del version
    if ctx.invoked_subcommand is not None:
        return
    from csbox.tui.app import CSBoxApp, real_home_data_source

    environment = detect_environment()
    data_source = real_home_data_source(Path.cwd())
    CSBoxApp(data_source=data_source, environment=environment, locale=_locale).run()


@app.command(help=_locale("cli.doctor.help"))
def doctor() -> None:
    translator = _locale
    environment = detect_environment()
    console = Console(markup=False)
    console.print(translator("doctor.title"))
    console.print(f"{translator('doctor.os')}: {environment.os_name} {environment.os_version}")
    console.print(f"{translator('doctor.python')}: {environment.python_version}")
    shell_name = environment.shell or translator("doctor.unknown")
    console.print(f"{translator('doctor.shell')}: {shell_name}")
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


@app.command("check", help="检查项目结构、敏感文件、Git 状态和可选构建。")
def check_project(
    root: Path | None = typer.Argument(None, help="待检查的项目目录。"),  # noqa: B008
    plain: bool = typer.Option(False, "--plain", help="输出纯文本结果。"),
    json_output: bool = typer.Option(False, "--json", help="输出稳定 JSON。"),
    build: bool = typer.Option(False, "--build", help="执行检测到的项目构建。"),
) -> None:
    if plain and json_output:
        raise typer.BadParameter("--plain 与 --json 不能同时使用。")
    try:
        project_root = root or Path(".")
        report = create_check_service(project_root).run(project_root, build=build)
    except Exception as error:
        Console(markup=False).print(f"项目检查失败：{error}")
        raise typer.Exit(code=1) from error

    if json_output:
        payload = report.model_dump(mode="json")
        payload["findings"] = [_safe_finding_payload(item) for item in payload["findings"]]
        payload["status"] = report.status.value
        payload["exitCode"] = report.exit_code
        Console(markup=False, soft_wrap=True).print(
            json.dumps(with_schema_version(payload), ensure_ascii=False, separators=(",", ":"))
        )
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
    console.print(f"项目：{report.root}")
    console.print(f"状态：{report.status.value}")
    for finding in (*report.findings, *report.builds):
        location = ""
        path = getattr(finding, "path", None)
        if path is not None:
            location = str(path)
            line = getattr(finding, "line", None)
            if line is not None:
                location += f":{line}"
        finding_category = getattr(finding, "category", None)
        category = f" [{finding_category}]" if finding_category else ""
        identifier = getattr(finding, "rule_id", getattr(finding, "adapter_id", "build"))
        if (
            finding_category in {"env", "private-key", "hard-coded-secret"}
            and finding.status.value == "FAIL"
        ):
            console.print(f"{location} {finding_category}")
            continue
        console.print(f"{finding.status.value} {identifier}{category} {location} {finding.message}")


def _safe_finding_payload(finding: dict[str, object]) -> dict[str, object]:
    category = finding.get("category")
    if category in {"env", "private-key", "hard-coded-secret"} and finding.get("status") == "FAIL":
        return {
            "path": finding.get("path"),
            "line": finding.get("line"),
            "category": category,
        }
    return finding


@lab_app.command("list")
def lab_list(
    json_output: bool = typer.Option(False, "--json", help="输出稳定 JSON。"),
) -> None:
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
    summary = f"{item['status']}  Capture: {item['captures']}  {item['shell']}"
    fixed_width = display_width(prefix) + 2 + display_width(summary) + 2
    available = max(0, width - fixed_width)
    name_width = available // 2
    cwd_width = available - name_width
    name = truncate_cells(str(item["name"]), name_width, ellipsis="…")
    cwd = truncate_cells(str(item["cwd"]), cwd_width, ellipsis="…")
    return f"{prefix}{name}  {summary}  {cwd}"


@lab_app.command("start")
def lab_start(
    name: str | None = typer.Argument(None, help="实验名称。"),
    shell: str | None = typer.Option(None, "--shell", help="powershell、pwsh、bash 或 zsh。"),
    verbose: bool = typer.Option(False, "--verbose", help="显示底层错误。"),
) -> None:
    service = create_lab_service()
    console = Console(markup=False)
    advisory_printed = False
    try:
        capture_advisory = getattr(service, "capture_advisory", None)
        if callable(capture_advisory):
            console.print(capture_advisory().message)
            advisory_printed = True
        result = service.start(name, shell=shell)
    except Exception as error:
        console.print(f"实验启动失败：{error}")
        if verbose and error.__cause__ is not None:
            console.print(f"底层错误：{error.__cause__}")
        raise typer.Exit(code=1) from error
    if not advisory_printed:
        console.print(result.advisory)
    status_label = "完成" if result.status == "completed" else result.status
    console.print(f"实验已{status_label}：{result.session.root}")


@lab_app.command("export")
def lab_export(
    session: str = typer.Argument(..., help="会话 ID 或唯一前缀。"),
    output: Annotated[Path | None, typer.Option("--output", help="导出目录。")] = None,
    theme: Annotated[str, typer.Option("--theme", help="dark 或 light。")] = "dark",
    force: Annotated[bool, typer.Option("--force", help="允许刷新已存在导出目录。")] = False,
) -> None:
    if theme not in {"dark", "light"}:
        raise typer.BadParameter("主题只能是 dark 或 light。", param_hint="--theme")
    console = Console(markup=False)
    try:
        result = create_lab_service().export(session, output, theme=theme, force=force)
    except Exception as error:
        console.print(f"实验导出失败：{error}")
        raise typer.Exit(code=1) from error
    console.print(f"实验证据已导出：{result.destination}")


@lab_app.command("review")
def lab_review(
    session: str | None = typer.Argument(None, help="会话 ID 或唯一前缀；省略则打开最近会话。"),
) -> None:
    from csbox.tui.app import ReviewApp
    from csbox.tui.screens.review import ReviewController

    service = create_lab_service()
    console = Console(markup=False)
    try:
        if session:
            paths = service.repository.resolve(session)
        else:
            latest = service.repository.latest()
            if latest is None:
                raise ValueError("暂无可回看的 session，请先运行 csbox lab start。")
            paths = latest.paths
        ReviewApp(controller=ReviewController.from_session(paths), locale=_locale).run()
    except Exception as error:
        console.print(f"打开 Review 失败：{error}")
        raise typer.Exit(code=1) from error


@app.command("pack", help="安全检查并打包项目文件。")
def pack_project(
    root: Path | None = typer.Argument(None, help="待打包的项目目录。"),  # noqa: B008
    output: Annotated[Path | None, typer.Option("--output", help="ZIP 文件或输出目录。")] = None,
    verify: bool = typer.Option(False, "--verify", help="重新打开 ZIP 校验条目。"),
    force: bool = typer.Option(False, "--force", help="允许覆盖已有 ZIP。"),
    json_output: bool = typer.Option(False, "--json", help="输出稳定 JSON。"),
) -> None:
    project_root = root or Path(".")
    try:
        report = create_pack_service(project_root).pack(
            project_root,
            destination=output,
            verify=verify,
            force=force,
        )
    except (OSError, PackServiceError) as error:
        Console(markup=False).print(f"项目打包失败：{error}")
        raise typer.Exit(code=1) from error
    if json_output:
        Console(markup=False, soft_wrap=True).print(
            json.dumps(
                with_schema_version(report.model_dump(mode="json")),
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )
        return
    console = Console(markup=False)
    console.print(f"打包完成：{report.destination}")
    console.print(
        f"文件数：{len(report.entries)}  源文件大小：{report.source_bytes}  "
        f"ZIP 大小：{report.archive_bytes}"
    )
    console.print(f"排除项：{len(report.excluded)}  已校验：{'是' if report.verified else '否'}")


def main() -> None:
    app()
