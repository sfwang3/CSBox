from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from typer.testing import CliRunner

from csbox.api.models import (
    ApiAssertion,
    ApiAssertionResult,
    ApiRequest,
    ApiResponse,
    ApiRun,
    ApiRunResult,
    ApiScenario,
    ApiStep,
)
from csbox.cli.main import app


@contextmanager
def _api_server() -> Iterator[tuple[str, list[str]]]:
    requests: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            requests.append(self.path)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"ok":true}')

        def log_message(self, format: str, *args: object) -> None:
            del format, args

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        yield f"http://{host}:{port}", requests
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def _write_scenario(path: Path, url: str, *, expected_status: int = 200) -> None:
    path.write_text(
        f'''name = "CLI 场景"

[[steps]]
name = "健康检查"
method = "GET"
url = "{url}"

[[steps.assertions]]
type = "status"
expected = {expected_status}
''',
        encoding="utf-8",
    )


def test_api_run_uses_cli_var_and_emits_schema_versioned_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scenario = tmp_path / "scenario.toml"
    monkeypatch.chdir(tmp_path)
    with _api_server() as (base_url, requests):
        _write_scenario(scenario, "{{base_url}}/health")

        result = CliRunner().invoke(
            app,
            ["api", "run", str(scenario), "--var", f"base_url={base_url}", "--json"],
        )

    assert result.exit_code == 0
    assert requests == ["/health"]
    payload = json.loads(result.stdout)
    assert payload["schema_version"] == 1
    assert payload["status"] == "PASS"
    assert payload["results"][0]["request"] == {"method": "GET", "url": f"{base_url}/health"}
    assert payload["results"][0]["response"]["status_code"] == 200
    listed = CliRunner().invoke(app, ["api", "list", "--json"])
    assert listed.exit_code == 0
    assert [item["id"] for item in json.loads(listed.stdout)["runs"]] == [payload["id"]]


def test_api_run_plain_output_uses_chinese_labels_and_technical_http_details(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scenario = tmp_path / "scenario.toml"
    monkeypatch.chdir(tmp_path)
    with _api_server() as (base_url, _requests):
        _write_scenario(scenario, f"{base_url}/health")

        result = CliRunner().invoke(app, ["api", "run", str(scenario), "--plain"])

    assert result.exit_code == 0
    assert "场景：CLI 场景" in result.stdout
    assert f"请求：GET {base_url}/health" in result.stdout
    assert "响应：HTTP 200" in result.stdout
    assert "状态：PASS" in result.stdout


def test_api_run_resolves_project_api_variables(tmp_path: Path, monkeypatch) -> None:
    scenario = tmp_path / "scenario.toml"
    config = tmp_path / ".csbox" / "config.toml"
    config.parent.mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "isolated-home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "isolated-xdg"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "isolated-appdata"))
    with _api_server() as (base_url, requests):
        config.write_text(f'[api.variables]\nbase_url = "{base_url}"\n', encoding="utf-8")
        _write_scenario(scenario, "{{base_url}}/from-config")

        result = CliRunner().invoke(app, ["api", "run", "scenario.toml", "--json"])

    assert result.exit_code == 0
    assert requests == ["/from-config"]


def test_api_run_uses_project_config_without_reading_user_config(
    tmp_path: Path, monkeypatch
) -> None:
    secret = "CSBOX_SECRET_SENTINEL_user_config"
    scenario = tmp_path / "scenario.toml"
    project_config = tmp_path / ".csbox" / "config.toml"
    user_config = tmp_path / "home" / ".config" / "csbox" / "config.toml"
    project_config.parent.mkdir()
    user_config.parent.mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("APPDATA", raising=False)
    user_config.write_text(
        f'''[api]
timeout_seconds = 0

[api.variables]
user_config_secret = "{secret}"
''',
        encoding="utf-8",
    )

    with _api_server() as (base_url, requests):
        project_config.write_text(f'[api.variables]\nbase_url = "{base_url}"\n', encoding="utf-8")
        _write_scenario(scenario, "{{base_url}}/from-project-config")

        result = CliRunner().invoke(app, ["api", "run", "scenario.toml", "--json"])

    assert result.exit_code == 0
    assert requests == ["/from-project-config"]
    assert secret not in result.stdout
    assert secret not in result.stderr
    assert secret not in json.dumps(json.loads(result.stdout), ensure_ascii=False)


def test_api_run_fail_fast_stops_after_first_failed_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scenario = tmp_path / "scenario.toml"
    monkeypatch.chdir(tmp_path)
    scenario.write_text(
        """name = "快速失败"

[[steps]]
name = "first"
method = "GET"
url = "{{base_url}}/first"
[[steps.assertions]]
type = "status"
expected = 201

[[steps]]
name = "second"
method = "GET"
url = "{{base_url}}/second"
[[steps.assertions]]
type = "status"
expected = 200
""",
        encoding="utf-8",
    )
    with _api_server() as (base_url, requests):
        result = CliRunner().invoke(
            app,
            [
                "api",
                "run",
                str(scenario),
                "--var",
                f"base_url={base_url}",
                "--fail-fast",
                "--json",
            ],
        )

    assert result.exit_code == 1
    assert requests == ["/first"]
    assert len(json.loads(result.stdout)["results"]) == 1


def test_api_run_returns_config_exit_code_for_invalid_scenario_without_secret_leak(
    tmp_path: Path,
) -> None:
    secret = "CSBOX_SECRET_SENTINEL_invalid_scenario"
    scenario = tmp_path / "invalid.toml"
    scenario.write_text(f'name = "{secret}"\nsteps = []\n', encoding="utf-8")

    result = CliRunner().invoke(app, ["api", "run", str(scenario), "--plain"])

    assert result.exit_code == 2
    assert "发生了什么" in result.stdout
    assert "怎么处理" in result.stdout
    assert secret not in result.stdout
    assert "Traceback" not in result.stdout


@pytest.mark.parametrize("output_flag", ["--plain", "--json"])
def test_api_run_rejects_secret_step_name_without_leaking_it(
    tmp_path: Path, output_flag: str
) -> None:
    secret = "CSBOX_SECRET_SENTINEL_step_name"
    scenario = tmp_path / "invalid-step.toml"
    scenario.write_text(
        f'''name = "安全错误"

[[steps]]
name = "{secret}"
method = "TRACE"
url = "https://example.test"
''',
        encoding="utf-8",
    )

    result = CliRunner().invoke(app, ["api", "run", str(scenario), output_flag])

    assert result.exit_code == 2
    assert "steps[0]" in result.stdout
    assert secret not in result.stdout
    assert secret not in result.stderr
    if output_flag == "--json":
        assert secret not in json.dumps(json.loads(result.stdout), ensure_ascii=False)


def test_api_run_returns_config_exit_code_for_invalid_cli_variable(tmp_path: Path) -> None:
    scenario = tmp_path / "scenario.toml"
    _write_scenario(scenario, "http://127.0.0.1:1/health")

    result = CliRunner().invoke(app, ["api", "run", str(scenario), "--var", "invalid"])

    assert result.exit_code == 2
    assert "发生了什么" in result.stdout
    assert "--var" in result.stdout


def test_api_run_returns_assertion_exit_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scenario = tmp_path / "scenario.toml"
    monkeypatch.chdir(tmp_path)
    with _api_server() as (base_url, _requests):
        _write_scenario(scenario, f"{base_url}/health", expected_status=201)

        result = CliRunner().invoke(app, ["api", "run", str(scenario), "--json"])

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["status"] == "FAIL"
    listed = CliRunner().invoke(app, ["api", "list", "--json"])
    assert [item["id"] for item in json.loads(listed.stdout)["runs"]] == [payload["id"]]


def test_api_run_returns_runtime_exit_code(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    scenario = tmp_path / "scenario.toml"
    monkeypatch.chdir(tmp_path)
    _write_scenario(scenario, "http://127.0.0.1:1/unavailable")

    result = CliRunner().invoke(app, ["api", "run", str(scenario), "--json"])

    assert result.exit_code == 3
    payload = json.loads(result.stdout)
    assert payload["status"] == "RUNTIME_ERROR"
    listed = CliRunner().invoke(app, ["api", "list", "--json"])
    assert [item["id"] for item in json.loads(listed.stdout)["runs"]] == [payload["id"]]


def test_api_run_reports_persistence_failure_without_traceback_or_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from csbox.api.errors import ApiPersistenceError
    from csbox.api.repository import ApiRunRepository

    secret = "CSBOX_SECRET_SENTINEL_persistence_failure"
    scenario = tmp_path / "scenario.toml"
    monkeypatch.chdir(tmp_path)
    _write_scenario(scenario, "http://127.0.0.1:1/{{token}}")
    monkeypatch.setattr(
        ApiRunRepository,
        "save",
        lambda _self, _run: (_ for _ in ()).throw(
            ApiPersistenceError("发生了什么：保存 API 运行记录失败。")
        ),
    )

    result = CliRunner().invoke(app, ["api", "run", "scenario.toml", "--var", f"token={secret}"])

    assert result.exit_code == 3
    assert "保存 API 运行记录失败" in result.stdout
    assert "Traceback" not in result.stdout
    assert secret not in result.stdout
    assert secret not in result.stderr


def test_api_run_verbose_writes_only_controlled_transport_cause_to_stderr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scenario = tmp_path / "scenario.toml"
    monkeypatch.chdir(tmp_path)
    _write_scenario(scenario, "http://127.0.0.1:1/unavailable")

    result = CliRunner().invoke(app, ["api", "run", str(scenario), "--verbose", "--json"])

    assert result.exit_code == 3
    assert "调试原因：httpx.ConnectError" in result.stderr
    assert "Traceback" not in result.stderr


def test_run_renderers_are_pure_and_keep_json_schema_versioned() -> None:
    from csbox.api.cli import render_run_plain, run_json_payload

    request = ApiRequest(method="GET", url="https://example.test/health")
    response = ApiResponse(status_code=200, url=request.url, elapsed_ms=12.5)
    assertion = ApiAssertion(kind="status", expected=200)
    run = ApiRun(
        id="run-123",
        scenario=ApiScenario(name="渲染", steps=(ApiStep(name="health", request=request),)),
        started_at=datetime(2026, 8, 11, tzinfo=UTC),
        ended_at=datetime(2026, 8, 11, tzinfo=UTC),
        elapsed_ms=12.5,
        results=(
            ApiRunResult(
                step_name="health",
                response=response,
                elapsed_ms=12.5,
                assertions=(
                    ApiAssertionResult(
                        assertion=assertion,
                        status="PASS",
                        message="Status = 200",
                    ),
                ),
            ),
        ),
    )

    payload = run_json_payload(run)

    assert payload["schema_version"] == 1
    assert payload["results"][0]["request"] == {"method": "GET", "url": request.url}
    assert "场景：渲染" in render_run_plain(run)
    assert "请求：GET https://example.test/health" in render_run_plain(run)
