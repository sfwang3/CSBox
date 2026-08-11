"""Safe, schema-versioned persistence for already-redacted API runs."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from csbox import __version__
from csbox.api.errors import ApiPersistenceError
from csbox.api.models import (
    ApiAssertion,
    ApiAssertionResult,
    ApiRequest,
    ApiRun,
    ApiRunResult,
    ApiScenario,
    ApiStep,
)
from csbox.api.redaction import Redactor
from csbox.config import ConfigPaths
from csbox.core.safe_paths import atomic_write_text, mkdir_exclusive, safe_relative_path
from csbox.core.schema import SCHEMA_VERSION

_STATUSES = ("PASS", "FAIL", "CONFIG_ERROR", "RUNTIME_ERROR")
_METADATA_KEYS = frozenset(
    {
        "schema_version",
        "id",
        "scenario",
        "status",
        "started_at",
        "ended_at",
        "elapsed_ms",
        "counts",
        "csbox_version",
    }
)
_RESULT_KEYS = frozenset({"schema_version", "scenario", "results"})
_SCENARIO_KEYS = frozenset({"name", "source"})
_RESULT_SCENARIO_KEYS = frozenset({"name", "source", "steps"})


@dataclass(frozen=True, slots=True)
class ApiRunPaths:
    """Paths for one validated run identifier beneath a repository root."""

    root: Path

    @property
    def metadata(self) -> Path:
        return self.root / "metadata.json"

    @property
    def result(self) -> Path:
        return self.root / "result.json"


@dataclass(frozen=True, slots=True)
class ApiRunSummary:
    """Small metadata-only view suitable for a deterministic run list."""

    paths: ApiRunPaths
    id: str
    scenario_name: str
    scenario_source: str | None
    status: str
    started_at: datetime
    ended_at: datetime | None
    elapsed_ms: float
    counts: dict[str, int]
    csbox_version: str


class ApiRunRepository:
    """Persist public, redacted API runs below ``.csbox/api/runs`` only."""

    def __init__(self, runs_root: Path | str) -> None:
        self.root = Path(runs_root)

    @classmethod
    def from_cwd(cls, cwd: Path | str) -> ApiRunRepository:
        return cls(ConfigPaths.project_only(Path(cwd)).api_runs)

    def paths_for(self, run_id: str) -> ApiRunPaths:
        identifier = _validated_run_id(run_id)
        self._assert_root_is_not_symlinked()
        paths = ApiRunPaths(self.root / identifier)
        if paths.root.is_symlink():
            raise _persistence_error("运行目录不能是符号链接。")
        return paths

    def save(self, run: ApiRun) -> ApiRunPaths:
        paths = self.paths_for(run.id)
        safe_run = _redacted_run_for_storage(run, project_root=self.root.parents[2])
        metadata = _metadata_document(safe_run)
        result = _result_document(safe_run)
        metadata_text = _json_document(metadata)
        result_text = _json_document(result)
        try:
            mkdir_exclusive(paths.root)
        except FileExistsError as error:
            raise _persistence_error("运行记录已存在，不能覆盖。") from error
        except (OSError, ValueError) as error:
            raise _persistence_error("保存 API 运行记录失败。") from error

        try:
            # Writing the derived document first keeps incomplete writes invisible to list().
            atomic_write_text(paths.result, result_text)
            atomic_write_text(paths.metadata, metadata_text)
        except (OSError, UnicodeError, ValueError, TypeError) as error:
            raise _persistence_error("保存 API 运行记录失败。") from error
        return paths

    def load(self, run_id_or_prefix: str) -> ApiRun:
        paths = self._resolve(run_id_or_prefix)
        try:
            metadata = self._read_metadata(paths)
            result = self._read_result(paths)
            payload = {
                "id": metadata["id"],
                "scenario": result["scenario"],
                "started_at": metadata["started_at"],
                "status": metadata["status"],
                "results": result["results"],
                "ended_at": metadata["ended_at"],
                "elapsed_ms": metadata["elapsed_ms"],
            }
            loaded = ApiRun.model_validate_json(
                json.dumps(payload, ensure_ascii=False, allow_nan=False)
            )
            _validate_loaded_integrity(metadata, loaded)
            return loaded
        except ApiPersistenceError:
            raise
        except (TypeError, ValueError, ValidationError, json.JSONDecodeError) as error:
            raise _persistence_error("运行记录不可用，结果文件无效。") from error

    def list(self) -> tuple[ApiRunSummary, ...]:
        if not self.root.exists():
            return ()
        self._assert_root_is_not_symlinked()
        if not self.root.is_dir():
            raise _persistence_error("API 运行目录不可用。")

        summaries: list[ApiRunSummary] = []
        for directory in self.root.iterdir():
            if not directory.is_dir() or directory.is_symlink():
                continue
            try:
                paths = self.paths_for(directory.name)
                metadata = self._read_metadata(paths)
                summaries.append(_summary_from_metadata(paths, metadata))
            except (OSError, UnicodeError, ValueError, ApiPersistenceError):
                continue
        summaries.sort(key=lambda summary: (-summary.started_at.timestamp(), summary.id))
        return tuple(summaries)

    def _resolve(self, run_id_or_prefix: str) -> ApiRunPaths:
        identifier = _validated_run_id(run_id_or_prefix)
        requested_paths = self.paths_for(identifier)
        if requested_paths.root.exists():
            return requested_paths

        matches = [summary.paths for summary in self.list() if summary.id.startswith(identifier)]
        if not matches:
            raise _persistence_error("未找到 API 运行记录。")
        if len(matches) > 1:
            raise _persistence_error("运行前缀不唯一，请提供更长的运行 ID。")
        return matches[0]

    def _read_metadata(self, paths: ApiRunPaths) -> dict[str, Any]:
        document = self._read_document(paths.metadata)
        if set(document) != _METADATA_KEYS or document.get("schema_version") != SCHEMA_VERSION:
            raise _persistence_error("运行记录 metadata 无效。")
        if document.get("id") != paths.root.name:
            raise _persistence_error("运行记录 metadata 无效。")
        _validated_run_id(document["id"])
        _validate_scenario(document.get("scenario"), _SCENARIO_KEYS)
        _validate_status(document.get("status"))
        started_at = _utc_datetime(document.get("started_at"))
        ended_at = document.get("ended_at")
        if ended_at is not None and _utc_datetime(ended_at) < started_at:
            raise _persistence_error("运行记录 metadata 无效。")
        _nonnegative_number(document.get("elapsed_ms"))
        _validate_counts(document.get("counts"))
        if _status_from_counts(document["counts"]) != document["status"]:
            raise _persistence_error("运行记录 metadata 无效。")
        if not isinstance(document.get("csbox_version"), str) or not document["csbox_version"]:
            raise _persistence_error("运行记录 metadata 无效。")
        return document

    def _read_result(self, paths: ApiRunPaths) -> dict[str, Any]:
        try:
            document = self._read_document(paths.result)
            if set(document) != _RESULT_KEYS or document.get("schema_version") != SCHEMA_VERSION:
                raise ValueError("invalid result schema")
            _validate_scenario(document.get("scenario"), _RESULT_SCENARIO_KEYS)
            if not isinstance(document.get("results"), list):
                raise ValueError("invalid results")
            return document
        except (
            ApiPersistenceError,
            OSError,
            UnicodeError,
            ValueError,
            json.JSONDecodeError,
        ) as error:
            raise _persistence_error("运行记录不可用，结果文件无效。") from error

    def _read_document(self, path: Path) -> dict[str, Any]:
        if path.is_symlink():
            raise _persistence_error("运行文件不能是符号链接。")
        try:
            document = json.loads(
                path.read_text(encoding="utf-8"), parse_constant=_reject_json_constant
            )
        except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as error:
            raise _persistence_error("运行记录文件无效。") from error
        if not isinstance(document, dict):
            raise _persistence_error("运行记录文件无效。")
        return document

    def _assert_root_is_not_symlinked(self) -> None:
        absolute = self.root.absolute()
        current = Path(absolute.anchor)
        for component in absolute.parts[1:]:
            current /= component
            if current.is_symlink():
                raise _persistence_error("API 运行目录不能经过符号链接。")


def _redacted_run_for_storage(run: ApiRun, *, project_root: Path) -> ApiRun:
    redactor = Redactor.with_configured_values(
        value for value in run.scenario.variables.values() if isinstance(value, str) and value
    )
    safe_steps = tuple(_redacted_step(step, redactor) for step in run.scenario.steps)
    safe_results = tuple(_redacted_result(result, redactor) for result in run.results)
    return run.model_copy(
        update={
            "scenario": ApiScenario(
                name=redactor.text(run.scenario.name),
                source=_relative_source(run.scenario.source, project_root),
                steps=safe_steps,
            ),
            "results": safe_results,
        },
        deep=True,
    )


def _redacted_step(step: ApiStep, redactor: Redactor) -> ApiStep:
    request = step.request
    return ApiStep(
        name=redactor.text(step.name),
        request=ApiRequest(
            method=request.method,
            url=redactor.url(request.url),
            headers=redactor.headers(request.headers),
            query=_redacted_mapping(request.query, redactor),
            json_body=redactor.json_value(request.json_body),
            body=None,
            form=_redacted_mapping(request.form, redactor),
            multipart=tuple(
                {"name": redactor.text(part.name), "value": redactor.text(part.value)}
                for part in request.multipart
            ),
            timeout_seconds=request.timeout_seconds,
            follow_redirects=request.follow_redirects,
            verify_tls=request.verify_tls,
        ),
        assertions=tuple(_redacted_assertion(item, redactor) for item in step.assertions),
    )


def _redacted_result(result: ApiRunResult, redactor: Redactor) -> ApiRunResult:
    return ApiRunResult(
        step_name=redactor.text(result.step_name),
        status=result.status,
        response=None if result.response is None else result.response.redacted_copy(redactor),
        assertions=tuple(
            ApiAssertionResult(
                assertion=_redacted_assertion(item.assertion, redactor),
                status=item.status,
                message=redactor.text(item.message),
                actual=redactor.json_value(item.actual),
            )
            for item in result.assertions
        ),
        error=None if result.error is None else redactor.text(result.error),
        elapsed_ms=result.elapsed_ms,
    )


def _redacted_assertion(assertion: ApiAssertion, redactor: Redactor) -> ApiAssertion:
    return ApiAssertion(
        kind=redactor.text(assertion.kind),
        expected=redactor.json_value(assertion.expected),
        location=None if assertion.location is None else redactor.text(assertion.location),
        operator=None if assertion.operator is None else redactor.text(assertion.operator),
    )


def _redacted_mapping(values: dict[str, str], redactor: Redactor) -> dict[str, str]:
    redacted = redactor.json_value(values)
    if not isinstance(redacted, dict):
        return {}
    return {str(name): str(value) for name, value in redacted.items()}


def _relative_source(source: str | None, project_root: Path) -> str | None:
    if source is None:
        return None
    path = Path(source)
    if path.is_absolute():
        try:
            path = path.relative_to(project_root)
        except ValueError:
            return None
    try:
        relative = safe_relative_path(path.as_posix())
    except ValueError:
        return None
    if str(relative) == ".":
        return None
    return relative.as_posix()


def _metadata_document(run: ApiRun) -> dict[str, object]:
    started_at = _utc_iso(run.started_at)
    ended_at = None if run.ended_at is None else _utc_iso(run.ended_at)
    if ended_at is not None and _utc_datetime(ended_at) < _utc_datetime(started_at):
        raise _persistence_error("运行结束时间不能早于开始时间。")
    counts = _counts(run)
    if _status_from_counts(counts) != run.status:
        raise _persistence_error("运行记录状态与结果不一致。")
    return {
        "schema_version": SCHEMA_VERSION,
        "id": run.id,
        "scenario": {"name": run.scenario.name, "source": run.scenario.source},
        "status": run.status,
        "started_at": started_at,
        "ended_at": ended_at,
        "elapsed_ms": run.elapsed_ms,
        "counts": counts,
        "csbox_version": __version__,
    }


def _result_document(run: ApiRun) -> dict[str, object]:
    return {
        "schema_version": SCHEMA_VERSION,
        "scenario": {
            "name": run.scenario.name,
            "source": run.scenario.source,
            "steps": [step.model_dump(mode="json") for step in run.scenario.steps],
        },
        "results": [result.model_dump(mode="json") for result in run.results],
    }


def _counts(run: ApiRun) -> dict[str, int]:
    return {status: sum(result.status == status for result in run.results) for status in _STATUSES}


def _status_from_counts(counts: dict[str, int]) -> str:
    for status in ("RUNTIME_ERROR", "CONFIG_ERROR", "FAIL"):
        if counts[status]:
            return status
    return "PASS"


def _validate_loaded_integrity(metadata: dict[str, Any], loaded: ApiRun) -> None:
    counts = _counts(loaded)
    if (
        metadata["status"] != loaded.status
        or metadata["counts"] != counts
        or _status_from_counts(counts) != loaded.status
    ):
        raise _persistence_error("运行记录不可用，结果文件无效。")


def _summary_from_metadata(paths: ApiRunPaths, document: dict[str, Any]) -> ApiRunSummary:
    scenario = document["scenario"]
    assert isinstance(scenario, dict)
    return ApiRunSummary(
        paths=paths,
        id=document["id"],
        scenario_name=scenario["name"],
        scenario_source=scenario["source"],
        status=document["status"],
        started_at=_utc_datetime(document["started_at"]),
        ended_at=None if document["ended_at"] is None else _utc_datetime(document["ended_at"]),
        elapsed_ms=float(document["elapsed_ms"]),
        counts=dict(document["counts"]),
        csbox_version=document["csbox_version"],
    )


def _validate_scenario(value: object, keys: frozenset[str]) -> None:
    if not isinstance(value, dict) or set(value) != keys:
        raise _persistence_error("运行记录文件无效。")
    if not isinstance(value.get("name"), str) or not value["name"]:
        raise _persistence_error("运行记录文件无效。")
    if value.get("source") is not None and not isinstance(value["source"], str):
        raise _persistence_error("运行记录文件无效。")
    if "steps" in keys and not isinstance(value.get("steps"), list):
        raise _persistence_error("运行记录文件无效。")


def _validate_status(value: object) -> None:
    if value not in _STATUSES:
        raise _persistence_error("运行记录 metadata 无效。")


def _validate_counts(value: object) -> None:
    if not isinstance(value, dict) or set(value) != set(_STATUSES):
        raise _persistence_error("运行记录 metadata 无效。")
    for count in value.values():
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise _persistence_error("运行记录 metadata 无效。")


def _nonnegative_number(value: object) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or (isinstance(value, float) and not math.isfinite(value))
        or value < 0
    ):
        raise _persistence_error("运行记录 metadata 无效。")


def _utc_datetime(value: object) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise _persistence_error("运行记录 metadata 无效。")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise _persistence_error("运行记录 metadata 无效。") from error
    if parsed.tzinfo is None or parsed.utcoffset() != UTC.utcoffset(parsed):
        raise _persistence_error("运行记录 metadata 无效。")
    return parsed.astimezone(UTC)


def _utc_iso(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise _persistence_error("运行时间必须使用 UTC 时区。")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _validated_run_id(value: object) -> str:
    if not isinstance(value, str):
        raise _persistence_error("运行标识无效。")
    identifier = value.strip()
    if (
        not identifier
        or identifier != value
        or identifier in {".", ".."}
        or Path(identifier).name != identifier
        or "/" in identifier
        or "\\" in identifier
        or "\x00" in identifier
    ):
        raise _persistence_error("运行标识无效。")
    return identifier


def _json_document(value: object) -> str:
    return (
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
        + "\n"
    )


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-standard JSON constant: {value}")


def _persistence_error(message: str) -> ApiPersistenceError:
    return ApiPersistenceError(
        f"发生了什么：{message}在哪里：API 运行记录。怎么处理：检查运行 ID 和本地文件后重试。"
    )


__all__ = ["ApiRunPaths", "ApiRunRepository", "ApiRunSummary"]
