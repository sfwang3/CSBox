from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from threading import Barrier

import pytest
from typer.testing import CliRunner

import csbox.api.repository as repository_module
from csbox.api.errors import ApiPersistenceError
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

SECRET = "CSBOX_SECRET_SENTINEL_repository"


def _run(
    identifier: str,
    *,
    started_at: datetime = datetime(2026, 8, 11, 8, 0, tzinfo=UTC),
    status: str = "PASS",
    source: str | None = "scenarios/health.toml",
) -> ApiRun:
    request = ApiRequest(
        method="POST",
        url="https://example.test/health",
        headers={"Authorization": "Bearer ••••••••"},
        json_body={"password": "••••••••"},
        body=SECRET,
    )
    assertion = ApiAssertion(kind="status", expected=200)
    response = ApiResponse(
        status_code=200,
        headers={"Set-Cookie": "••••••••"},
        body='{"token":"••••••••","ok":true}',
        url=request.url,
        content_type="application/json",
        elapsed_ms=3.5,
    )
    return ApiRun(
        id=identifier,
        scenario=ApiScenario(
            name="健康检查",
            source=source,
            variables={"token": SECRET},
            steps=(ApiStep(name="请求健康检查", request=request, assertions=(assertion,)),),
        ),
        started_at=started_at,
        ended_at=started_at,
        status=status,  # type: ignore[arg-type]
        elapsed_ms=3.5,
        results=(
            ApiRunResult(
                step_name="请求健康检查",
                status=status,  # type: ignore[arg-type]
                response=response,
                elapsed_ms=3.5,
                assertions=(
                    ApiAssertionResult(
                        assertion=assertion,
                        status="PASS",
                        message="状态码符合预期",
                    ),
                ),
            ),
        ),
    )


def _repository(tmp_path: Path):
    from csbox.api.repository import ApiRunRepository

    return ApiRunRepository.from_cwd(tmp_path)


def test_repository_saves_loads_and_lists_redacted_schema_versioned_runs(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    run = _run("20260811T080000-abcdef123456", source=str(tmp_path / "scenarios/health.toml"))

    paths = repository.save(run)

    assert paths.root == repository.root / run.id
    assert paths.metadata == paths.root / "metadata.json"
    assert paths.result == paths.root / "result.json"
    metadata = json.loads(paths.metadata.read_text(encoding="utf-8"))
    result = json.loads(paths.result.read_text(encoding="utf-8"))
    assert metadata == {
        "schema_version": 1,
        "id": run.id,
        "scenario": {"name": "健康检查", "source": "scenarios/health.toml"},
        "status": "PASS",
        "started_at": "2026-08-11T08:00:00Z",
        "ended_at": "2026-08-11T08:00:00Z",
        "elapsed_ms": 3.5,
        "counts": {"PASS": 1, "FAIL": 0, "CONFIG_ERROR": 0, "RUNTIME_ERROR": 0},
        "csbox_version": metadata["csbox_version"],
    }
    assert result["schema_version"] == 1
    assert result["scenario"] == {
        "name": "健康检查",
        "source": "scenarios/health.toml",
        "steps": result["scenario"]["steps"],
    }
    assert "variables" not in result["scenario"]
    assert result["scenario"]["steps"][0]["request"]["body"] is None
    serialized = paths.metadata.read_text(encoding="utf-8") + paths.result.read_text(
        encoding="utf-8"
    )
    assert SECRET not in serialized
    assert "_raw_assertion_view" not in serialized

    loaded = repository.load("20260811T080000-abc")

    assert loaded == run.model_copy(
        update={
            "scenario": run.scenario.model_copy(
                update={
                    "variables": {},
                    "source": "scenarios/health.toml",
                    "steps": (
                        run.scenario.steps[0].model_copy(
                            update={
                                "request": run.scenario.steps[0].request.model_copy(
                                    update={"body": None}
                                )
                            }
                        ),
                    ),
                }
            ),
            "results": run.results,
        }
    )
    summaries = repository.list()
    assert len(summaries) == 1
    assert summaries[0].id == run.id
    assert summaries[0].counts == {"PASS": 1, "FAIL": 0, "CONFIG_ERROR": 0, "RUNTIME_ERROR": 0}


def test_repository_rejects_ambiguous_prefix_and_absent_run_with_stable_messages(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    repository.save(_run("20260811T080000-aaaaaaaaaaaa"))
    repository.save(_run("20260811T080001-bbbbbbbbbbbb"))

    with pytest.raises(ApiPersistenceError, match="运行前缀不唯一"):
        repository.load("20260811T08000")
    with pytest.raises(ApiPersistenceError, match="未找到 API 运行记录"):
        repository.load("missing")


def test_repository_refuses_symlink_run_directory_and_traversal_identifier(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    redirected = tmp_path / "redirected"
    redirected.mkdir()
    run = _run("20260811T080000-abcdef123456")
    (repository.root / run.id).parent.mkdir(parents=True)
    (repository.root / run.id).symlink_to(redirected, target_is_directory=True)

    with pytest.raises(ApiPersistenceError, match="符号链接"):
        repository.save(run)
    with pytest.raises(ApiPersistenceError, match="符号链接"):
        repository.load(run.id)
    with pytest.raises(ApiPersistenceError, match="运行标识"):
        repository.load("../outside")
    assert list(redirected.iterdir()) == []


def test_repository_keeps_incomplete_run_when_result_write_fails_and_list_ignores_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import csbox.api.repository as repository_module

    repository = _repository(tmp_path)
    run = _run("20260811T080000-abcdef123456")
    original_write = repository_module.atomic_write_text

    def fail_result(path: Path, text: str) -> None:
        if path.name == "result.json":
            raise OSError("simulated interrupted write")
        original_write(path, text)

    monkeypatch.setattr(repository_module, "atomic_write_text", fail_result)

    with pytest.raises(ApiPersistenceError, match="保存 API 运行记录失败"):
        repository.save(run)

    paths = repository.paths_for(run.id)
    assert not paths.result.exists()
    assert not paths.metadata.exists()
    assert paths.root.is_dir()
    assert not list(paths.root.glob(".*.tmp"))
    assert repository.list() == ()


def test_repository_keeps_partial_result_when_metadata_write_fails_and_list_ignores_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import csbox.api.repository as repository_module

    repository = _repository(tmp_path)
    run = _run("20260811T080000-abcdef123456")
    original_write = repository_module.atomic_write_text

    def fail_metadata(path: Path, text: str) -> None:
        if path.name == "metadata.json":
            raise OSError("simulated interrupted metadata write")
        original_write(path, text)

    monkeypatch.setattr(repository_module, "atomic_write_text", fail_metadata)

    with pytest.raises(ApiPersistenceError, match="保存 API 运行记录失败"):
        repository.save(run)

    paths = repository.paths_for(run.id)
    assert paths.result.is_file()
    assert not paths.metadata.exists()
    assert paths.root.is_dir()
    assert repository.list() == ()


def test_repository_save_exclusively_claims_run_directory_under_concurrent_saves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = _repository(tmp_path)
    run = _run("20260811T080000-abcdef123456")
    barrier = Barrier(2)
    synchronized_checks = 0
    synchronize_save_checks = True
    original_exists = Path.exists

    def synchronized_exists(path: Path) -> bool:
        nonlocal synchronized_checks
        if synchronize_save_checks and path == repository.root / run.id:
            synchronized_checks += 1
            if synchronized_checks <= 2:
                barrier.wait(timeout=2)
        return original_exists(path)

    monkeypatch.setattr(Path, "exists", synchronized_exists)
    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(lambda _: _save_outcome(repository, run), range(2)))
    synchronize_save_checks = False

    assert outcomes.count("saved") == 1
    assert outcomes.count("运行记录已存在，不能覆盖") == 1
    assert repository.load(run.id).id == run.id


def test_repository_failed_save_does_not_touch_a_concurrently_replaced_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import csbox.api.repository as repository_module

    repository = _repository(tmp_path)
    run = _run("20260811T080000-abcdef123456")
    original_write = repository_module.atomic_write_text
    replacement_content = "concurrently-owned replacement"

    def replace_result_then_fail_metadata(path: Path, text: str) -> None:
        if path.name == "metadata.json":
            replacement = path.with_name("concurrent-result.json")
            replacement.write_text(replacement_content, "utf-8")
            os.replace(replacement, repository.root / run.id / "result.json")
            raise OSError("simulated interrupted metadata write")
        original_write(path, text)

    monkeypatch.setattr(repository_module, "atomic_write_text", replace_result_then_fail_metadata)

    with pytest.raises(ApiPersistenceError, match="保存 API 运行记录失败"):
        repository.save(run)

    paths = repository.paths_for(run.id)
    assert paths.result.read_text(encoding="utf-8") == replacement_content
    assert not paths.metadata.exists()
    assert paths.root.is_dir()
    assert repository.list() == ()


@pytest.mark.parametrize(
    "run",
    [
        _run("20260811T080000-abcdef123456").model_copy(update={"status": "FAIL"}),
        _run("20260811T080000-abcdef123456").model_copy(
            update={"ended_at": datetime(2026, 8, 11, 7, 59, 59, tzinfo=UTC)}
        ),
    ],
)
def test_repository_rejects_inconsistent_run_before_creating_its_directory(
    tmp_path: Path, run: ApiRun
) -> None:
    repository = _repository(tmp_path)

    with pytest.raises(ApiPersistenceError):
        repository.save(run)

    assert not (repository.root / run.id).exists()


def test_repository_defensively_redacts_all_scenario_variable_values_before_storage(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    raw_secret = "CSBOX_SECRET_SENTINEL_raw_scenario_variable"
    run = _run("20260811T080000-abcdef123456")
    request = run.scenario.steps[0].request.model_copy(
        update={
            "headers": {"X-Trace": raw_secret},
            "query": {"trace": raw_secret},
            "json_body": {"echo": raw_secret},
            "body": raw_secret,
        }
    )
    response = run.results[0].response
    assert response is not None
    run = run.model_copy(
        update={
            "scenario": run.scenario.model_copy(
                update={
                    "variables": {"known_value": raw_secret},
                    "steps": (run.scenario.steps[0].model_copy(update={"request": request}),),
                }
            ),
            "results": (
                run.results[0].model_copy(
                    update={
                        "response": response.model_copy(
                            update={
                                "headers": {"X-Trace": raw_secret},
                                "body": json.dumps({"echo": raw_secret}),
                                "url": f"https://example.test/health?trace={raw_secret}",
                            }
                        ),
                        "assertions": (
                            run.results[0]
                            .assertions[0]
                            .model_copy(update={"message": f"response contains {raw_secret}"}),
                        ),
                        "error": f"request failed with {raw_secret}",
                    }
                ),
            ),
        }
    )

    paths = repository.save(run)

    stored = paths.metadata.read_text(encoding="utf-8") + paths.result.read_text(encoding="utf-8")
    result = json.loads(paths.result.read_text(encoding="utf-8"))
    request_payload = result["scenario"]["steps"][0]["request"]
    response_payload = result["results"][0]["response"]
    assert raw_secret not in stored
    assert "variables" not in result["scenario"]
    assert request_payload["json_body"]["echo"] == "••••••••"
    assert response_payload["body"] == '{"echo":"••••••••"}'
    assert result["results"][0]["assertions"][0]["message"] == "response contains ••••••••"
    assert result["results"][0]["error"] == "request failed with ••••••••"


def test_repository_rejects_corrupt_metadata_nan_and_list_json_stays_standard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = _repository(tmp_path)
    paths = repository.save(_run("20260811T080000-abcdef123456"))
    metadata = json.loads(paths.metadata.read_text(encoding="utf-8"))
    metadata["elapsed_ms"] = float("nan")
    paths.metadata.write_text(json.dumps(metadata), encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    with pytest.raises(ApiPersistenceError, match="运行记录"):
        repository.load(paths.root.name)

    result = CliRunner().invoke(app, ["api", "list", "--json"])

    assert result.exit_code == 0
    assert json.loads(result.stdout)["runs"] == []
    assert "NaN" not in result.stdout


def test_repository_reports_corrupt_result_as_unavailable_without_harming_neighbor(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    corrupt = _run("20260811T080000-aaaaaaaaaaaa")
    healthy = _run(
        "20260811T080001-bbbbbbbbbbbb",
        started_at=datetime(2026, 8, 11, 8, 1, tzinfo=UTC),
    )
    corrupt_paths = repository.save(corrupt)
    repository.save(healthy)
    corrupt_paths.result.write_text("{", encoding="utf-8")

    assert [summary.id for summary in repository.list()] == [healthy.id, corrupt.id]
    with pytest.raises(ApiPersistenceError, match="运行记录不可用"):
        repository.load(corrupt.id)
    loaded_healthy = repository.load(healthy.id)
    assert loaded_healthy.id == healthy.id
    assert loaded_healthy.scenario.variables == {}
    assert loaded_healthy.scenario.steps[0].request.body is None
    assert corrupt_paths.result.read_text(encoding="utf-8") == "{"


def test_repository_maps_pathologically_deep_json_to_a_safe_persistence_error(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    paths = repository.save(_run("20260811T080000-deepjsonsafe"))
    paths.result.write_text("[" * 10_000 + "0" + "]" * 10_000, encoding="utf-8")

    with pytest.raises(ApiPersistenceError, match="运行记录") as caught:
        repository.load(paths.root.name)

    assert "Recursion" not in str(caught.value)


def test_repository_maps_json_recursion_to_a_safe_persistence_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _repository(tmp_path)
    paths = repository.save(_run("20260811T080000-recursionsafe"))
    real_loads = repository_module.json.loads

    def recurse_on_result(value: str, **kwargs: object) -> object:
        if '"results"' in value:
            raise RecursionError("too deep")
        return real_loads(value, **kwargs)

    monkeypatch.setattr(repository_module.json, "loads", recurse_on_result)

    with pytest.raises(ApiPersistenceError, match="运行记录") as caught:
        repository.load(paths.root.name)

    assert "Recursion" not in str(caught.value)


def test_repository_reads_documents_through_the_bounded_regular_file_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _repository(tmp_path)
    paths = repository.save(_run("20260811T080000-boundedreader"))
    observed: list[tuple[Path, int]] = []
    from csbox.core.safe_paths import read_regular_text as real_read_regular_text

    def observed_read(path: Path, *, max_bytes: int, encoding: str = "utf-8") -> str:
        observed.append((Path(path), max_bytes))
        return real_read_regular_text(path, max_bytes=max_bytes, encoding=encoding)

    monkeypatch.setattr(repository_module, "read_regular_text", observed_read, raising=False)

    repository.load(paths.root.name)

    assert {path.name for path, _limit in observed} == {"metadata.json", "result.json"}
    assert all(limit > 0 for _path, limit in observed)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("status", "FAIL"),
        ("counts", {"PASS": 0, "FAIL": 1, "CONFIG_ERROR": 0, "RUNTIME_ERROR": 0}),
    ],
)
def test_repository_rejects_metadata_status_or_counts_not_internally_consistent(
    tmp_path: Path, field: str, value: object
) -> None:
    repository = _repository(tmp_path)
    corrupt_paths = repository.save(_run("20260811T080000-aaaaaaaaaaaa"))
    healthy = _run(
        "20260811T080001-bbbbbbbbbbbb",
        started_at=datetime(2026, 8, 11, 8, 1, tzinfo=UTC),
    )
    repository.save(healthy)
    metadata = json.loads(corrupt_paths.metadata.read_text(encoding="utf-8"))
    metadata[field] = value
    corrupt_paths.metadata.write_text(json.dumps(metadata), encoding="utf-8")

    assert [summary.id for summary in repository.list()] == [healthy.id]
    with pytest.raises(ApiPersistenceError, match="运行记录 metadata 无效"):
        repository.load(corrupt_paths.root.name)
    assert repository.load(healthy.id).id == healthy.id


def test_repository_rejects_metadata_with_end_before_start_in_load_and_list(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    corrupt_paths = repository.save(_run("20260811T080000-aaaaaaaaaaaa"))
    healthy = _run(
        "20260811T080001-bbbbbbbbbbbb",
        started_at=datetime(2026, 8, 11, 8, 1, tzinfo=UTC),
    )
    repository.save(healthy)
    metadata = json.loads(corrupt_paths.metadata.read_text(encoding="utf-8"))
    metadata["ended_at"] = "2026-08-11T07:59:59Z"
    corrupt_paths.metadata.write_text(json.dumps(metadata), encoding="utf-8")

    assert [summary.id for summary in repository.list()] == [healthy.id]
    with pytest.raises(ApiPersistenceError, match="运行记录 metadata 无效"):
        repository.load(corrupt_paths.root.name)
    assert repository.load(healthy.id).id == healthy.id


def test_repository_load_rejects_result_status_counts_mismatch_without_repairing_result(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    corrupt_paths = repository.save(_run("20260811T080000-aaaaaaaaaaaa"))
    healthy = _run(
        "20260811T080001-bbbbbbbbbbbb",
        started_at=datetime(2026, 8, 11, 8, 1, tzinfo=UTC),
    )
    repository.save(healthy)
    result = json.loads(corrupt_paths.result.read_text(encoding="utf-8"))
    result["results"][0]["status"] = "FAIL"
    corrupt_paths.result.write_text(json.dumps(result), encoding="utf-8")

    assert [summary.id for summary in repository.list()] == [healthy.id, corrupt_paths.root.name]
    with pytest.raises(ApiPersistenceError, match="运行记录不可用，结果文件无效"):
        repository.load(corrupt_paths.root.name)
    assert corrupt_paths.result.read_text(encoding="utf-8") == json.dumps(result)
    assert repository.load(healthy.id).id == healthy.id


def test_repository_lists_runs_in_deterministic_newest_then_id_order(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    started_at = datetime(2026, 8, 11, 8, 0, tzinfo=UTC)
    repository.save(_run("20260811T080000-bbbbbbbbbbbb", started_at=started_at))
    repository.save(_run("20260811T080000-aaaaaaaaaaaa", started_at=started_at))
    repository.save(
        _run("20260811T080001-cccccccccccc", started_at=datetime(2026, 8, 11, 8, 1, tzinfo=UTC))
    )

    assert [summary.id for summary in repository.list()] == [
        "20260811T080001-cccccccccccc",
        "20260811T080000-aaaaaaaaaaaa",
        "20260811T080000-bbbbbbbbbbbb",
    ]


def test_api_list_emits_schema_versioned_secret_free_json_and_truncated_plain_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = _repository(tmp_path)
    repository.save(
        _run(
            "20260811T080001-cccccccccccc",
            started_at=datetime(2026, 8, 11, 8, 1, tzinfo=UTC),
        )
    )
    repository.save(_run("20260811T080000-aaaaaaaaaaaa"))
    monkeypatch.chdir(tmp_path)

    json_result = CliRunner().invoke(app, ["api", "list", "--json"])
    plain_result = CliRunner().invoke(app, ["api", "list", "--plain"])

    assert json_result.exit_code == 0
    payload = json.loads(json_result.stdout)
    assert payload["schema_version"] == 1
    assert [item["id"] for item in payload["runs"]] == [
        "20260811T080001-cccccccccccc",
        "20260811T080000-aaaaaaaaaaaa",
    ]
    assert SECRET not in json_result.stdout
    assert SECRET not in plain_result.stdout
    assert "20260811T08…" in plain_result.stdout


def _save_outcome(repository: object, run: ApiRun) -> str:
    try:
        repository.save(run)  # type: ignore[union-attr]
    except ApiPersistenceError as error:
        return error.user_message.split("。", maxsplit=1)[0].removeprefix("发生了什么：")
    return "saved"
