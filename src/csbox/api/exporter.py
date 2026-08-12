"""Safe Markdown, JSON, and PNG export for persisted API evidence."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

from csbox.api.errors import ApiConfigError, ApiDomainError, ApiPersistenceError
from csbox.api.evidence import ApiEvidenceBuilder, evidence_filename, redact_evidence
from csbox.api.models import ApiEvidence, ApiRequest, ApiResponse, ApiRun
from csbox.api.redaction import Redactor
from csbox.api.renderer import ApiEvidenceRenderer, ThemeName
from csbox.core.safe_paths import (
    atomic_write_text,
    mkdir_exclusive,
    open_regular_binary,
    read_regular_text,
    restore_backup_or_preserve,
    safe_relative_path,
    safe_rename,
)
from csbox.core.schema import with_schema_version
from csbox.core.text_layout import wrap_cells

_MARKDOWN_WIDTH = 100
_GENERATED_MANIFEST = ".csbox-generated-api-evidence.json"
# A persisted run may occupy up to 64 MiB.  Evidence repeats each response in
# the top-level view and its result, then adds pretty-print whitespace.
_MAX_EXPORT_JSON_BYTES = 144 * 1024 * 1024
_MAX_EXPORT_MANIFEST_BYTES = 4 * 1024 * 1024
_STATUS_LABELS = {
    "PASS": "通过",
    "FAIL": "失败",
    "SKIP": "跳过",
    "CONFIG_ERROR": "配置错误",
    "RUNTIME_ERROR": "运行错误",
}


@dataclass(frozen=True, slots=True)
class ApiExportResult:
    destination: Path
    evidence: tuple[Path, ...]
    markdown: Path
    results: Path

    @property
    def json(self) -> Path:
        """Compatibility alias for callers that call the JSON artifact ``json``."""

        return self.results

    @property
    def files(self) -> tuple[Path, ...]:
        return (*self.evidence, self.markdown, self.results)


class ApiMarkdownRenderer:
    """Render safe evidence items as stable, human-readable Markdown."""

    def render(self, evidence_items: Iterable[ApiEvidence]) -> str:
        items = tuple(redact_evidence(item) for item in evidence_items)
        lines = ["# CSBox API Evidence", "", "> 事实记录：内容来自已保存的 API 运行结果。", ""]
        if not items:
            lines.append("暂无已执行的步骤证据。")
            return "\n".join(lines) + "\n"

        first = items[0]
        lines.append(f"## 场景：{_escape_markdown(first.scenario_name or '—')}")
        if first.scenario_source:
            lines.append(f"来源：`{_escape_inline(first.scenario_source)}`")
        if first.run_id:
            lines.append(f"运行 ID：`{_escape_inline(first.run_id)}`")
        if first.run_status:
            lines.append(
                f"运行状态：{_STATUS_LABELS.get(first.run_status, '未知状态')} ({first.run_status})"
            )
        if first.started_at is not None:
            lines.append(f"开始时间：`{first.started_at.isoformat()}`")
        lines.append("")

        for index, item in enumerate(items, 1):
            lines.extend(self._render_item(index, item))
        return "\n".join(lines).rstrip() + "\n"

    def _render_item(self, index: int, evidence: ApiEvidence) -> list[str]:
        lines = [
            f"## 步骤 {index}：{_escape_markdown(evidence.title)}",
            "",
            f"![API 证据](evidence/{evidence_filename(evidence.title, index)})",
            "",
            "### 请求",
            "",
        ]
        lines.extend(_fenced_block(_request_lines(evidence.request), "text"))
        lines.extend(("", "### 响应", ""))
        if evidence.response is None:
            lines.extend(_fenced_block(("未收到响应",), "text"))
        else:
            lines.extend(_fenced_block(_response_lines(evidence.response), "text"))
        lines.extend(("", "### 断言", ""))
        if evidence.result is None or not evidence.result.assertions:
            lines.append("未记录断言结果。")
        else:
            for assertion in evidence.result.assertions:
                status = _STATUS_LABELS.get(assertion.status, "未知状态")
                location = assertion.assertion.location or assertion.assertion.kind
                expected = _inline_json(assertion.assertion.expected)
                actual = _inline_json(assertion.actual)
                assertion_line = (
                    f"- {status} ({assertion.status}) `{_escape_inline(location)}` "
                    f"expected={_escape_inline(expected)} actual={_escape_inline(actual)}"
                )
                lines.extend(_wrapped_markdown(assertion_line))
                lines.extend(_wrapped_markdown(f"  说明：{_escape_markdown(assertion.message)}"))
        if evidence.result is not None:
            lines.extend(
                _wrapped_markdown(
                    f"步骤状态：{_STATUS_LABELS.get(evidence.result.status, '未知状态')} "
                    f"({evidence.result.status})；耗时：{evidence.result.elapsed_ms:.1f} ms"
                )
            )
            if evidence.result.error:
                lines.extend(_wrapped_markdown(f"错误：{_escape_markdown(evidence.result.error)}"))
        lines.append("")
        return lines


class ApiEvidenceExporter:
    """Stage and publish all API evidence artifacts with rollback semantics."""

    def __init__(
        self,
        renderer: ApiEvidenceRenderer | None = None,
        markdown_renderer: ApiMarkdownRenderer | None = None,
    ) -> None:
        self.renderer = renderer or ApiEvidenceRenderer()
        self.markdown_renderer = markdown_renderer or ApiMarkdownRenderer()

    def export(
        self,
        run: ApiRun,
        destination: Path | str,
        theme: ThemeName = "dark",
        force: bool = False,
    ) -> ApiExportResult:
        if theme not in {"dark", "light"}:
            raise ApiConfigError(
                "发生了什么：证据主题无效。在哪里：--theme。怎么处理：使用 dark 或 light 后重试。"
            )
        output = Path(destination)
        try:
            evidence_items = tuple(ApiEvidenceBuilder.from_run(run))
            filenames = tuple(
                evidence_filename(item.title, index) for index, item in enumerate(evidence_items, 1)
            )
            existing = _preflight_output(output, filenames, force=force)
            previous_generated = (
                _previous_generated_evidence(output) if output.exists() and force else ()
            )
            staging = _create_staging_directory(output)
        except ApiDomainError:
            raise
        except Exception as error:
            raise _export_error() from error

        try:
            staged_evidence = staging / "evidence"
            mkdir_exclusive(staged_evidence)
            staged_images: list[Path] = []
            safe_items = tuple(redact_evidence(item) for item in evidence_items)
            for _index, (item, filename) in enumerate(zip(safe_items, filenames, strict=True), 1):
                target = staged_evidence / filename
                self.renderer.render(item, target, theme=theme)
                staged_images.append(target)

            markdown_text = self.markdown_renderer.render(safe_items)
            markdown_path = staging / "api-evidence.md"
            results_path = staging / "results.json"
            manifest_path = staging / _GENERATED_MANIFEST
            atomic_write_text(markdown_path, markdown_text)
            atomic_write_text(results_path, _json_text(_results_payload(run, safe_items)))
            atomic_write_text(
                manifest_path,
                _json_text(
                    {
                        "version": 1,
                        "files": [
                            {
                                "path": f"evidence/{filename}",
                                "sha256": _file_sha256(image),
                            }
                            for filename, image in zip(filenames, staged_images, strict=True)
                        ],
                    }
                ),
            )
            _validate_staged_files(staged_images, markdown_path, results_path, manifest_path)
        except Exception as error:
            _cleanup_staging(staging)
            if isinstance(error, ApiDomainError):
                raise
            raise _export_error() from error

        generated = (
            tuple(
                (staging / "evidence" / filename, output / "evidence" / filename)
                for filename in filenames
            )
            + ((staging / "api-evidence.md", output / "api-evidence.md"),)
            + ((staging / "results.json", output / "results.json"),)
            + ((staging / _GENERATED_MANIFEST, output / _GENERATED_MANIFEST),)
        )
        generated_destinations = {destination for _source, destination in generated}
        obsolete = tuple(path for path in previous_generated if path not in generated_destinations)
        try:
            if output.exists():
                _publish_to_existing_directory(
                    staging,
                    output,
                    generated,
                    existing,
                    obsolete,
                )
            else:
                safe_rename(staging, output, replace_existing=False)
        except Exception as error:
            _cleanup_staging(staging)
            if isinstance(error, ApiDomainError):
                raise
            raise _export_error() from error

        return ApiExportResult(
            destination=output,
            evidence=tuple(output / "evidence" / filename for filename in filenames),
            markdown=output / "api-evidence.md",
            results=output / "results.json",
        )


def _results_payload(run: ApiRun, evidence_items: tuple[ApiEvidence, ...]) -> dict[str, object]:
    safe_items = tuple(redact_evidence(item) for item in evidence_items)
    redactor = Redactor.with_configured_values(
        value for value in run.scenario.variables.values() if isinstance(value, str) and value
    )
    run_id = safe_items[0].run_id if safe_items else redactor.text(run.id)
    scenario_name = safe_items[0].scenario_name if safe_items else redactor.text(run.scenario.name)
    source = (
        safe_items[0].scenario_source
        if safe_items
        else (None if run.scenario.source is None else redactor.text(run.scenario.source))
    )
    return with_schema_version(
        {
            "id": run_id,
            "scenario": {"name": scenario_name, "source": source},
            "status": run.status,
            "started_at": run.started_at.isoformat(),
            "ended_at": None if run.ended_at is None else run.ended_at.isoformat(),
            "elapsed_ms": run.elapsed_ms,
            "evidence": [item.model_dump(mode="json") for item in safe_items],
        }
    )


def _json_text(payload: object) -> str:
    return (
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            allow_nan=False,
        )
        + "\n"
    )


def _preflight_output(output: Path, filenames: tuple[str, ...], *, force: bool) -> dict[Path, bool]:
    if output.is_symlink():
        raise _unsafe_output_error("输出目录不能是符号链接。")
    _assert_safe_output_parent(output)
    if output.exists() and not output.is_dir():
        raise _unsafe_output_error("输出路径必须是普通目录。")
    if output.exists() and not force:
        raise ApiPersistenceError(
            "发生了什么：导出目录已存在。"
            "在哪里：API Evidence 输出。"
            "怎么处理：更换 --output 或使用 --force 后重试。"
        )

    existing: dict[Path, bool] = {}
    if not output.exists():
        return existing
    evidence_directory = output / "evidence"
    if evidence_directory.is_symlink():
        raise _unsafe_output_error("evidence 目录不能是符号链接。")
    if evidence_directory.exists() and not evidence_directory.is_dir():
        raise _unsafe_output_error("evidence 路径必须是普通目录。")
    for filename in (*filenames, "api-evidence.md", "results.json", _GENERATED_MANIFEST):
        relative = "evidence/" + filename if filename in filenames else filename
        safe_relative_path(relative)
        path = output / relative
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            existing[path] = False
            continue
        if stat.S_ISLNK(metadata.st_mode):
            raise _unsafe_output_error("生成目标不能是符号链接。")
        if not stat.S_ISREG(metadata.st_mode):
            raise _unsafe_output_error("生成目标必须是普通文件。")
        existing[path] = True
    return existing


def _create_staging_directory(output: Path) -> Path:
    # Do not derive temporary names from a caller-controlled output name.  The
    # output directory may be named after data that must never appear in a
    # temporary artifact path.
    prefix = ".csbox-api-evidence-"
    for _ in range(8):
        staging = output.parent / f"{prefix}{os.urandom(16).hex()}.partial"
        try:
            mkdir_exclusive(staging)
        except FileExistsError:
            continue
        return staging
    raise OSError("could not allocate API evidence staging directory")


def _assert_safe_output_parent(output: Path) -> None:
    """Reject an output path whose existing parent chain contains a symlink."""

    absolute = output.absolute()
    current = Path(absolute.anchor)
    for component in absolute.parts[1:-1]:
        current /= component
        if current.is_symlink():
            raise _unsafe_output_error("输出目录的父路径不能包含符号链接。")


def _validate_staged_files(
    images: list[Path],
    markdown: Path,
    results: Path,
    manifest: Path,
) -> None:
    for image in images:
        if image.is_symlink() or not image.is_file():
            raise OSError("staged evidence image is not a regular file")
        with Image.open(image) as opened:
            opened.verify()
    if markdown.is_symlink() or not markdown.is_file():
        raise OSError("staged Markdown is not a regular file")
    if results.is_symlink() or not results.is_file():
        raise OSError("staged JSON is not a regular file")
    json.loads(
        read_regular_text(results, max_bytes=_MAX_EXPORT_JSON_BYTES),
        parse_constant=_reject_json_constant,
    )
    document = json.loads(
        read_regular_text(manifest, max_bytes=_MAX_EXPORT_MANIFEST_BYTES),
        parse_constant=_reject_json_constant,
    )
    if not isinstance(document, dict) or document.get("version") != 1:
        raise OSError("staged API evidence manifest is invalid")


def _previous_generated_evidence(output: Path) -> tuple[Path, ...]:
    manifest = output / _GENERATED_MANIFEST
    try:
        document = json.loads(
            read_regular_text(manifest, max_bytes=_MAX_EXPORT_MANIFEST_BYTES),
            parse_constant=_reject_json_constant,
        )
    except FileNotFoundError:
        return ()
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError, RecursionError):
        return ()
    if not isinstance(document, dict) or document.get("version") != 1:
        return ()
    files = document.get("files")
    if not isinstance(files, list) or any(not isinstance(item, dict) for item in files):
        return ()
    generated: list[Path] = []
    for item in files:
        relative_text = item.get("path")
        expected_digest = item.get("sha256")
        if (
            not isinstance(relative_text, str)
            or not isinstance(expected_digest, str)
            or not re.fullmatch(r"[0-9a-f]{64}", expected_digest)
        ):
            return ()
        try:
            relative = safe_relative_path(relative_text)
        except ValueError:
            return ()
        if len(relative.parts) != 2 or relative.parts[0] != "evidence":
            return ()
        candidate = output.joinpath(*relative.parts)
        if candidate.is_symlink():
            raise _unsafe_output_error("历史 API evidence 不能是符号链接。")
        try:
            current_digest = _file_sha256(candidate)
        except FileNotFoundError:
            continue
        except (OSError, ValueError):
            return ()
        if current_digest == expected_digest:
            generated.append(candidate)
    return tuple(generated)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open_regular_binary(path) as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _publish_to_existing_directory(
    staging: Path,
    output: Path,
    generated: tuple[tuple[Path, Path], ...],
    existing: dict[Path, bool],
    obsolete: tuple[Path, ...],
) -> None:
    if output.is_symlink() or not output.is_dir():
        raise OSError("output directory changed during publication")
    evidence_directory = output / "evidence"
    created_evidence_directory = False
    if not evidence_directory.exists():
        mkdir_exclusive(evidence_directory)
        created_evidence_directory = True

    backups: dict[Path, Path] = {}
    try:
        operations: tuple[tuple[Path | None, Path], ...] = (
            *generated,
            *((None, destination) for destination in obsolete),
        )
        for index, (source, destination) in enumerate(operations, 1):
            should_backup = existing.get(destination, False) or source is None
            if should_backup and destination.exists():
                if destination.is_symlink() or not destination.is_file():
                    raise OSError("generated destination is not a regular file")
                backup = staging / f".backup-{index}"
                safe_rename(destination, backup, replace_existing=False)
                backups[destination] = backup
            if source is not None:
                safe_rename(source, destination, replace_existing=False)
    except BaseException:
        # Never replace a destination during rollback: another writer can take
        # the name after its old content was moved to staging.  If a name is
        # occupied, preserve the occupant under a private recovery name before
        # restoring the old canonical artifact.
        for destination, backup in reversed(tuple(backups.items())):
            if backup.exists():
                restore_backup_or_preserve(backup, destination)
        if (
            created_evidence_directory
            and evidence_directory.is_dir()
            and not any(evidence_directory.iterdir())
        ):
            evidence_directory.rmdir()
        raise
    for backup in backups.values():
        backup.unlink()
    _cleanup_staging(staging)


def _cleanup_staging(staging: Path) -> None:
    if staging.is_symlink() or not staging.is_dir():
        return
    if any(staging.glob(".backup-*")):
        return
    shutil.rmtree(staging, ignore_errors=True)


def _request_lines(request: ApiRequest) -> tuple[str, ...]:
    lines = [f"{request.method} {request.url}"]
    if request.query:
        lines.append("Query:")
        lines.extend(f"  {name}: {value}" for name, value in sorted(request.query.items()))
    if request.headers:
        lines.append("Headers:")
        lines.extend(f"  {name}: {value}" for name, value in sorted(request.headers.items()))
    if request.json_body is not None:
        lines.append("JSON body:")
        lines.extend(f"  {line}" for line in _json_lines(request.json_body))
    if request.body is not None:
        lines.append("Body:")
        lines.extend(f"  {line}" for line in _display_lines(request.body))
    if request.form:
        lines.append("Form:")
        lines.extend(f"  {name}: {value}" for name, value in sorted(request.form.items()))
    if request.multipart:
        lines.append("Multipart:")
        lines.extend(f"  {part.name}: {part.value}" for part in request.multipart)
    if len(lines) == 1:
        lines.append("Body: not recorded")
    return tuple(lines)


def _response_lines(response: ApiResponse) -> tuple[str, ...]:
    content_type = response.content_type or "—"
    size_suffix = "" if response.response_size_exact else " (lower bound)"
    lines = [
        f"HTTP {response.status_code}  {response.elapsed_ms:.1f} ms  "
        f"{content_type}  {response.response_size} bytes{size_suffix}",
    ]
    if response.headers:
        lines.append("Headers:")
        lines.extend(f"  {name}: {value}" for name, value in sorted(response.headers.items()))
    lines.append("Body:")
    lines.extend(f"  {line}" for line in _body_lines(response.body, response.content_type))
    return tuple(lines)


def _body_lines(body: str, content_type: str | None) -> tuple[str, ...]:
    if content_type and (
        content_type.partition(";")[0].strip().casefold() == "application/json"
        or content_type.partition(";")[0].strip().casefold().endswith("+json")
    ):
        try:
            parsed = json.loads(body, parse_constant=_reject_json_constant)
            return _json_lines(parsed)
        except (TypeError, ValueError, json.JSONDecodeError, RecursionError):
            pass
    return _display_lines(body)


def _json_lines(value: object) -> tuple[str, ...]:
    try:
        rendered = json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2, sort_keys=True)
    except (TypeError, ValueError, RecursionError):
        rendered = "null"
    return tuple(rendered.split("\n"))


def _display_lines(value: str) -> tuple[str, ...]:
    result: list[str] = []
    for line in value.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        characters: list[str] = []
        for character in line.replace("\t", "    "):
            if unicodedata.category(character) in {"Cc", "Cf"}:
                characters.append(f"\\u{ord(character):04x}")
            else:
                characters.append(character)
        result.append("".join(characters))
    return tuple(result or ("",))


def _fenced_block(lines: Iterable[str], language: str) -> list[str]:
    content = tuple(wrapped for line in lines for wrapped in wrap_cells(line, _MARKDOWN_WIDTH))
    longest = 0
    current = 0
    for line in content:
        for character in line:
            if character == "`":
                current += 1
                longest = max(longest, current)
            else:
                current = 0
    fence = "`" * max(3, longest + 1)
    return [f"{fence}{language}", *content, fence]


def _wrapped_markdown(text: str) -> list[str]:
    return list(wrap_cells(text, _MARKDOWN_WIDTH))


def _inline_json(value: object) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    except (TypeError, ValueError, RecursionError):
        return "null"


def _escape_inline(value: str) -> str:
    return _single_line(value).replace("`", "\\`")


def _escape_markdown(value: str) -> str:
    return re.sub(r"([\\`*_{}\[\]()#+|%<>!~])", r"\\\1", _single_line(value))


def _single_line(value: str) -> str:
    characters: list[str] = []
    for character in value.replace("\r\n", "\n").replace("\r", "\n"):
        if character == "\n":
            characters.append(" ")
        elif unicodedata.category(character).startswith("C"):
            characters.append(f"\\u{ord(character):04x}")
        else:
            characters.append(character)
    return "".join(characters)


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-standard JSON constant: {value}")


def _export_error() -> ApiPersistenceError:
    return ApiPersistenceError(
        "发生了什么：API Evidence 导出失败。在哪里：PNG、Markdown 或 JSON 输出。"
        "怎么处理：检查输出目录、字体、符号链接和写入权限后重试；"
        "并发冲突文件会保留为同目录 .csbox-recovery-*.bak。"
    )


def _unsafe_output_error(reason: str) -> ApiPersistenceError:
    return ApiPersistenceError(
        f"发生了什么：{reason}在哪里：API Evidence 输出。怎么处理：选择普通目录和普通文件后重试。"
    )


__all__ = ["ApiEvidenceExporter", "ApiExportResult", "ApiMarkdownRenderer"]
