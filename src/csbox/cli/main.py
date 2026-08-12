from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from csbox import __version__
from csbox.api.cli import api_app
from csbox.check.service import create_check_service
from csbox.core.display_width import display_width, truncate_cells
from csbox.core.environment import detect_environment
from csbox.core.safe_paths import safe_relative_path
from csbox.core.schema import with_schema_version
from csbox.lab.service import create_lab_service
from csbox.locales import Translator, load_locale
from csbox.pack.service import create_pack_service

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


def _print_safe_failure(message: str, error: Exception, *, verbose: bool) -> None:
    Console(markup=False).print(message)
    if verbose:
        Console(markup=False, stderr=True).print(f"调试类型：{type(error).__name__}")


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
            "发生了什么：项目检查失败。在哪里：项目目录或检查规则。"
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
            console.print(f"Deep secret scan    {finding.status.value} {finding.message}")
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
        if (
            finding_category in {"env", "private-key", "hard-coded-secret"}
            and finding.status.value == "FAIL"
        ):
            console.print(f"{finding.status.value} {finding_category} {location}".rstrip())
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


@lab_app.command("list")
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
            "发生了什么：实验无法启动。在哪里：当前 Shell 或实验目录。"
            "怎么处理：运行 csbox doctor，检查 Shell 配置和目录权限后重试。",
            error,
            verbose=verbose,
        )
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


@lab_app.command("review")
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


@app.command("pack", help="安全检查并打包项目文件。")
def pack_project(
    root: Path | None = typer.Argument(None, help="待打包的项目目录。"),  # noqa: B008
    output: Annotated[Path | None, typer.Option("--output", help="ZIP 文件或输出目录。")] = None,
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
            "发生了什么：项目打包失败。在哪里：打包计划或输出文件。"
            "怎么处理：先处理项目检查失败项，再检查输出路径和权限后重试。",
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
        if plan.rejected or (plan.output_exists and not plan.force):
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
    console.print("打包预览")
    console.print(f"项目：.  类型：{plan.project_type}")
    console.print(f"输出：{_safe_pack_location(root, plan.destination)}")
    console.print(
        f"included：{len(plan.included)}  excluded：{len(plan.excluded)}  "
        f"rejected：{len(plan.rejected)}  源文件大小：{plan.source_bytes}"
    )
    console.print("included:")
    for item in plan.included:
        console.print(f"  + {item}")
    console.print("excluded:")
    for item in plan.excluded:
        console.print(f"  - {item}")
    console.print("rejected:")
    for item in plan.rejected:
        console.print(f"  ! {item}")


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
