from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from csbox.core.environment import detect_environment
from csbox.lab.service import create_lab_service
from csbox.locales import Translator, load_locale

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


def _yes_no(translator: Translator, value: bool) -> str:
    return translator("doctor.yes" if value else "doctor.no")


@app.callback()
def _run_default(ctx: typer.Context) -> None:
    if ctx.invoked_subcommand is not None:
        return
    from csbox.lab.fake_data import HOME_DATA_SOURCES
    from csbox.tui.app import CSBoxApp

    environment = detect_environment()
    data_source = HOME_DATA_SOURCES.get("fake")
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
    console = Console(markup=False)
    if json_output:
        console.print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
        return
    if not payload:
        console.print("暂无实验记录。")
        return
    for item in payload:
        console.print(
            f"{item['id']}  {item['name']}  {item['status']}  "
            f"Capture: {item['captures']}  {item['shell']}  {item['cwd']}"
        )


@lab_app.command("start")
def lab_start(
    name: str | None = typer.Argument(None, help="实验名称。"),
    shell: str | None = typer.Option(None, "--shell", help="powershell、pwsh、bash 或 zsh。"),
    verbose: bool = typer.Option(False, "--verbose", help="显示底层错误。"),
) -> None:
    service = create_lab_service()
    console = Console(markup=False)
    try:
        result = service.start(name, shell=shell)
    except Exception as error:
        console.print(f"实验启动失败：{error}")
        if verbose and error.__cause__ is not None:
            console.print(f"底层错误：{error.__cause__}")
        raise typer.Exit(code=1) from error
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


def main() -> None:
    app()
