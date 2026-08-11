"""Typer adapter for running redacted API scenarios from the command line."""

from __future__ import annotations

import asyncio
import json
import os
import re
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Annotated, Any

import typer

from csbox.api.errors import ApiConfigError, ApiDomainError, ApiPersistenceError, ApiTransportError
from csbox.api.httpx_transport import HttpxTransport
from csbox.api.models import ApiRequest, ApiResponse, ApiRun, ApiRunResult
from csbox.api.openapi import OpenApiImporter, write_scenario_templates
from csbox.api.redaction import Redactor
from csbox.api.repository import ApiRunRepository, ApiRunSummary
from csbox.api.runner import ApiRunner
from csbox.api.scenario import ScenarioLoader
from csbox.api.transport import ApiTransport
from csbox.api.variables import resolve_variables
from csbox.config import ConfigPaths, ConfigurationError, load_config
from csbox.config.models import ApiConfig
from csbox.core.display_width import truncate_cells
from csbox.core.schema import with_schema_version

api_app = typer.Typer(
    help="接口测试场景、运行记录和证据导出。",
    no_args_is_help=True,
)

_PLACEHOLDER_RE = re.compile(r"{{([A-Za-z_][A-Za-z0-9_]*)}}")
_VARIABLE_ASSIGNMENT_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", re.DOTALL)
_CONTROLLED_DEBUG_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,120}$")
_EXIT_CODES = {
    "PASS": 0,
    "FAIL": 1,
    "CONFIG_ERROR": 2,
    "RUNTIME_ERROR": 3,
}


@api_app.command("list", help="列出当前项目已保存的接口测试运行记录。")
def list_runs(
    plain: Annotated[bool, typer.Option("--plain", help="输出中文纯文本结果。")] = False,
    json_output: Annotated[bool, typer.Option("--json", help="输出稳定 JSON 结果。")] = False,
) -> None:
    """List metadata-only persisted runs without opening result bodies."""

    try:
        if plain and json_output:
            raise ApiConfigError(
                "发生了什么：--plain 与 --json 不能同时使用。"
                "在哪里：命令行输出选项。"
                "怎么处理：仅选择一种输出格式后重试。"
            )
        summaries = ApiRunRepository.from_cwd(Path.cwd()).list()
    except ApiConfigError as error:
        _emit_error(error, code=2, json_output=json_output, verbose=False)
        raise typer.Exit(code=2) from None
    except ApiPersistenceError as error:
        _emit_error(error, code=3, json_output=json_output, verbose=False)
        raise typer.Exit(code=3) from None

    if json_output:
        typer.echo(
            json.dumps(
                api_list_json_payload(summaries),
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            )
        )
    else:
        typer.echo(render_list_plain(summaries))


def render_list_plain(summaries: Iterable[ApiRunSummary]) -> str:
    """Render deterministic metadata-only rows without exposing run details."""

    rows = tuple(summaries)
    if not rows:
        return "暂无 API 运行记录。"
    return "\n".join(
        "  ".join(
            (
                truncate_cells(summary.id, 12, ellipsis="…"),
                summary.status,
                summary.scenario_name,
                _format_elapsed(summary.elapsed_ms),
            )
        )
        for summary in rows
    )


def api_list_json_payload(summaries: Iterable[ApiRunSummary]) -> dict[str, object]:
    """Return the stable public schema for ``csbox api list --json``."""

    return with_schema_version(
        {
            "runs": [
                {
                    "id": summary.id,
                    "scenario": {
                        "name": summary.scenario_name,
                        "source": summary.scenario_source,
                    },
                    "status": summary.status,
                    "started_at": summary.started_at.isoformat(),
                    "ended_at": None if summary.ended_at is None else summary.ended_at.isoformat(),
                    "elapsed_ms": summary.elapsed_ms,
                    "counts": summary.counts,
                    "csbox_version": summary.csbox_version,
                }
                for summary in summaries
            ]
        }
    )


@api_app.command("import", help="从 OpenAPI 3.0/3.1 生成静态 TOML 场景模板。")
def import_openapi(
    openapi_path: Annotated[Path, typer.Argument(help="OpenAPI JSON 或 YAML 文件。")],
    output: Annotated[Path, typer.Option("--output", help="生成场景模板的目录。")] = Path(
        ".csbox/api/scenarios"
    ),
    force: Annotated[bool, typer.Option("--force", help="覆盖已有的普通 TOML 模板文件。")] = False,
    plain: Annotated[bool, typer.Option("--plain", help="输出中文纯文本结果。")] = False,
    json_output: Annotated[bool, typer.Option("--json", help="输出稳定 JSON 结果。")] = False,
) -> None:
    """Import static request structure without executing or resolving source content."""

    try:
        if plain and json_output:
            raise ApiConfigError(
                "发生了什么：--plain 与 --json 不能同时使用。"
                "在哪里：命令行输出选项。"
                "怎么处理：仅选择一种输出格式后重试。"
            )
        importer = OpenApiImporter()
        scenario = importer.to_scenario(importer.load(openapi_path), openapi_path.stem)
        written = write_scenario_templates(scenario, output, force=force)
    except ApiConfigError as error:
        _emit_error(error, code=2, json_output=json_output, verbose=False)
        raise typer.Exit(code=2) from None
    except FileExistsError:
        _emit_error(
            ApiConfigError(
                "发生了什么：目标场景模板已存在。"
                "在哪里：OpenAPI 导出目录。"
                "怎么处理：更换 --output，或确认后使用 --force 覆盖普通文件。"
            ),
            code=2,
            json_output=json_output,
            verbose=False,
        )
        raise typer.Exit(code=2) from None
    except (OSError, ValueError):
        _emit_error(
            ApiConfigError(
                "发生了什么：无法安全写入 OpenAPI 场景模板。"
                "在哪里：OpenAPI 导出目录。"
                "怎么处理：检查目录、符号链接和写入权限后重试。"
            ),
            code=2,
            json_output=json_output,
            verbose=False,
        )
        raise typer.Exit(code=2) from None

    if json_output:
        typer.echo(
            json.dumps(
                api_import_json_payload(written),
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            )
        )
    else:
        typer.echo(render_import_plain(written))


def render_import_plain(files: Iterable[Path]) -> str:
    """Render imported template paths without exposing source document values."""

    paths = tuple(files)
    lines = [f"已导入 {len(paths)} 个 OpenAPI 操作："]
    lines.extend(f"- {path.as_posix()}" for path in paths)
    return "\n".join(lines)


def api_import_json_payload(files: Iterable[Path]) -> dict[str, object]:
    """Return the stable public schema for ``csbox api import --json``."""

    return with_schema_version({"files": [path.as_posix() for path in files]})


@api_app.command("run", help="运行一个 TOML 接口测试场景。")
def run_scenario(
    scenario_path: Annotated[Path, typer.Argument(help="待运行的场景 TOML 文件。")],
    variables: Annotated[
        list[str] | None,
        typer.Option("--var", help="覆盖变量，格式为 key=value；可重复使用。"),
    ] = None,
    plain: Annotated[bool, typer.Option("--plain", help="输出中文纯文本结果。")] = False,
    json_output: Annotated[bool, typer.Option("--json", help="输出稳定 JSON 结果。")] = False,
    fail_fast: Annotated[
        bool, typer.Option("--fail-fast", help="首个失败步骤后停止执行。")
    ] = False,
    verbose: Annotated[bool, typer.Option("--verbose", help="显示受控调试原因。")] = False,
) -> None:
    """Run a scenario without exposing the domain implementation to Typer."""

    try:
        if plain and json_output:
            raise ApiConfigError(
                "发生了什么：--plain 与 --json 不能同时使用。"
                "在哪里：命令行输出选项。"
                "怎么处理：仅选择一种输出格式后重试。"
            )
        cli_variables = _parse_cli_variables(variables or ())
        scenario = ScenarioLoader().load(scenario_path)
        config = _load_api_config()
        resolution = resolve_variables(
            _referenced_variable_names(scenario.steps),
            config.variables,
            scenario.variables,
            os.environ,
            cli_variables,
        )
        if resolution.missing_names:
            raise ApiConfigError(resolution.user_message)

        transport = _DebugTransport(
            HttpxTransport(
                response_max_bytes=config.response_max_bytes,
                timeout_seconds=config.timeout_seconds,
            )
        )
        run = asyncio.run(
            ApiRunner(transport, Redactor.with_configured_values([])).run(
                scenario,
                resolution.values,
                fail_fast=fail_fast,
            )
        )
        ApiRunRepository.from_cwd(Path.cwd()).save(run)
    except ApiConfigError as error:
        _emit_error(error, code=2, json_output=json_output, verbose=verbose)
        raise typer.Exit(code=2) from None
    except ApiTransportError as error:
        _emit_error(error, code=3, json_output=json_output, verbose=verbose)
        raise typer.Exit(code=3) from None
    except ApiPersistenceError as error:
        _emit_error(error, code=3, json_output=json_output, verbose=verbose)
        raise typer.Exit(code=3) from None

    _emit_run(run, json_output=json_output)
    if verbose:
        _emit_debug_causes(transport.debug_causes)
    raise typer.Exit(code=_EXIT_CODES[run.status])


def render_run_plain(run: ApiRun) -> str:
    """Render an already-redacted run using Chinese labels and technical HTTP details."""

    lines = [
        f"运行 ID：{run.id}",
        f"场景：{run.scenario.name}",
        f"状态：{run.status}",
        f"开始时间：{run.started_at.isoformat()}",
        f"结束时间：{run.ended_at.isoformat() if run.ended_at is not None else '—'}",
        f"耗时：{_format_elapsed(run.elapsed_ms)}",
        "步骤：",
    ]
    for index, result in enumerate(run.results):
        step = run.scenario.steps[index] if index < len(run.scenario.steps) else None
        lines.extend(_render_result_plain(result, step.request if step is not None else None))
    return "\n".join(lines)


def run_json_payload(run: ApiRun) -> dict[str, object]:
    """Return a schema-versioned JSON-safe payload without mutating ``run``."""

    results: list[dict[str, object]] = []
    for index, result in enumerate(run.results):
        step = run.scenario.steps[index] if index < len(run.scenario.steps) else None
        results.append(_result_json_payload(result, step.request if step is not None else None))
    return with_schema_version(
        {
            "id": run.id,
            "scenario": {
                "name": run.scenario.name,
                "source": run.scenario.source,
            },
            "status": run.status,
            "started_at": run.started_at.isoformat(),
            "ended_at": None if run.ended_at is None else run.ended_at.isoformat(),
            "elapsed_ms": run.elapsed_ms,
            "results": results,
        }
    )


class _DebugTransport:
    """Preserve intentionally safe transport diagnostics for explicit verbose output."""

    def __init__(self, transport: ApiTransport) -> None:
        self._transport = transport
        self.debug_causes: list[str] = []

    async def send(self, request: ApiRequest) -> ApiResponse:
        try:
            return await self._transport.send(request)
        except ApiTransportError as error:
            if error.debug_message is not None and _CONTROLLED_DEBUG_RE.fullmatch(
                error.debug_message
            ):
                self.debug_causes.append(error.debug_message)
            raise


def _load_api_config() -> ApiConfig:
    cwd = Path.cwd()
    try:
        return load_config(cwd, paths=ConfigPaths.project_only(cwd)).api
    except ConfigurationError as error:
        raise ApiConfigError(
            "发生了什么：API 配置无效。"
            "在哪里：项目配置。"
            "怎么处理：检查 [api] 与 [api.variables] 配置后重试。"
        ) from error


def _parse_cli_variables(values: Iterable[str]) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for value in values:
        match = _VARIABLE_ASSIGNMENT_RE.fullmatch(value)
        if match is None:
            raise ApiConfigError(
                "发生了什么：--var 必须使用 key=value 格式。"
                "在哪里：命令行变量。"
                "怎么处理：使用安全的变量名和明确值，例如 --var base_url=https://example.test。"
            )
        name, replacement = match.groups()
        if name in parsed:
            raise ApiConfigError(
                "发生了什么：同一变量不能重复指定。"
                "在哪里：命令行变量。"
                "怎么处理：每个 --var key=value 仅保留一次后重试。"
            )
        parsed[name] = replacement
    return parsed


def _referenced_variable_names(steps: Iterable[Any]) -> set[str]:
    names: set[str] = set()
    for step in steps:
        _collect_placeholder_names(step.request.model_dump(mode="python"), names)
    return names


def _collect_placeholder_names(value: Any, names: set[str]) -> None:
    if isinstance(value, str):
        names.update(_PLACEHOLDER_RE.findall(value))
    elif isinstance(value, Mapping):
        for item in value.values():
            _collect_placeholder_names(item, names)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _collect_placeholder_names(item, names)


def _emit_run(run: ApiRun, *, json_output: bool) -> None:
    if json_output:
        typer.echo(json.dumps(run_json_payload(run), ensure_ascii=False, separators=(",", ":")))
    else:
        typer.echo(render_run_plain(run))


def _emit_error(error: ApiDomainError, *, code: int, json_output: bool, verbose: bool) -> None:
    if json_output:
        typer.echo(
            json.dumps(
                with_schema_version(
                    {
                        "status": "CONFIG_ERROR" if code == 2 else "RUNTIME_ERROR",
                        "exit_code": code,
                        "error": error.user_message,
                    }
                ),
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )
    else:
        typer.echo(error.user_message)
    if verbose and error.debug_message is not None:
        _emit_debug_causes((error.debug_message,))


def _emit_debug_causes(causes: Iterable[str]) -> None:
    for cause in dict.fromkeys(causes):
        if _CONTROLLED_DEBUG_RE.fullmatch(cause):
            typer.echo(f"调试原因：{cause}", err=True)


def _render_result_plain(result: ApiRunResult, request: ApiRequest | None) -> list[str]:
    lines = [f"- 步骤：{result.step_name}", f"  状态：{result.status}"]
    if request is not None:
        lines.append(f"  请求：{request.method} {request.url}")
    if result.response is not None:
        lines.append(
            f"  响应：HTTP {result.response.status_code}  "
            f"{_format_elapsed(result.response.elapsed_ms)}"
        )
    if result.error is not None:
        lines.append(f"  错误：{result.error}")
    for assertion in result.assertions:
        lines.append(f"  断言：{assertion.status} {assertion.message}")
    return lines


def _result_json_payload(
    result: ApiRunResult,
    request: ApiRequest | None,
) -> dict[str, object]:
    response: dict[str, object] | None = None
    if result.response is not None:
        response = result.response.model_dump(mode="json")
    return {
        "step_name": result.step_name,
        "status": result.status,
        "request": None if request is None else {"method": request.method, "url": request.url},
        "response": response,
        "assertions": [item.model_dump(mode="json") for item in result.assertions],
        "error": result.error,
        "elapsed_ms": result.elapsed_ms,
    }


def _format_elapsed(elapsed_ms: float) -> str:
    return f"{elapsed_ms:.1f} ms"


__all__ = [
    "api_app",
    "api_list_json_payload",
    "render_list_plain",
    "render_run_plain",
    "run_json_payload",
]
