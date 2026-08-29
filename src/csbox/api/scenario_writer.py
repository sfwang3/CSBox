"""Small, file-backed TOML writer shared by API scenario producers."""

from __future__ import annotations

import json
import math
import re
import secrets
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from csbox.api.models import ApiScenario
from csbox.api.scenario import ScenarioLoader
from csbox.core.safe_paths import (
    atomic_write_text,
    ensure_private_directory,
    mkdir_exclusive,
    restore_backup_or_preserve,
    safe_relative_path,
    safe_rename,
)

_FILENAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


@dataclass(frozen=True, slots=True)
class ScenarioFile:
    """One validated scenario source to publish as a TOML file."""

    filename_stem: str
    scenario: ApiScenario
    comments: tuple[str, ...] = ()


def write_scenario_files(
    files: tuple[ScenarioFile, ...],
    destination: Path,
    *,
    force: bool = False,
) -> tuple[Path, ...]:
    """Stage, validate, and safely publish a set of scenario TOML files."""

    if not files:
        return ()
    output_directory = Path(destination)
    if output_directory.is_symlink():
        raise ValueError("refusing to write scenario files through a symlink directory")
    ensure_private_directory(output_directory.parent)
    filenames = _filenames(files)
    paths = tuple(output_directory / filename for filename in filenames)
    existing = _preflight_paths(output_directory, paths, force)
    rendered = tuple(_render(file.scenario, file.comments) for file in files)
    staging = _create_staging_directory(output_directory)
    try:
        for filename, text in zip(filenames, rendered, strict=True):
            atomic_write_text(staging / filename, text)
        _validate_staged(staging, filenames)
        if output_directory.exists():
            _publish_existing(staging, output_directory, filenames, existing, rendered)
        else:
            safe_rename(staging, output_directory, replace_existing=False)
    except BaseException:
        _cleanup_unpublished_staging(staging)
        raise
    return paths


def render_scenario_toml(scenario: ApiScenario, comments: tuple[str, ...] = ()) -> str:
    """Render a model through the same TOML boundary used by file publication."""

    return _render(scenario, comments)


def _filenames(files: tuple[ScenarioFile, ...]) -> tuple[str, ...]:
    used: set[str] = set()
    filenames: list[str] = []
    for index, file in enumerate(files, start=1):
        stem = _safe_filename_stem(file.filename_stem, index)
        candidate = stem
        suffix = 2
        while candidate.casefold() in used:
            candidate = f"{stem}-{suffix}"
            suffix += 1
        used.add(candidate.casefold())
        filenames.append(f"{candidate}.toml")
    return tuple(filenames)


def scenario_filename(filename_stem: str, *, index: int = 1) -> str:
    """Return the deterministic first filename for a scenario stem."""

    return f"{_safe_filename_stem(filename_stem, index)}.toml"


def _safe_filename_stem(filename_stem: str, index: int) -> str:
    stem = _FILENAME_RE.sub("-", filename_stem).strip(".-")[:80]
    return stem or f"scenario-{index}"


def _render(scenario: ApiScenario, comments: tuple[str, ...]) -> str:
    lines = list(comments)
    lines.append(f"name = {_toml_value(scenario.name)}")
    if scenario.variables:
        lines.append(f"variables = {_toml_value(dict(sorted(scenario.variables.items())))}")
    for step in scenario.steps:
        request = step.request
        lines.extend(
            (
                "",
                "[[steps]]",
                f"name = {_toml_value(step.name)}",
                f"method = {_toml_value(request.method)}",
                f"url = {_toml_value(request.url)}",
            )
        )
        if request.headers:
            lines.append(f"headers = {_toml_value(dict(sorted(request.headers.items())))}")
        if request.query:
            lines.append(f"query = {_toml_value(dict(sorted(request.query.items())))}")
        if request.json_body is not None:
            lines.append(f"json = {_toml_value(request.json_body)}")
        elif request.form:
            lines.append(f"form = {_toml_value(dict(sorted(request.form.items())))}")
        elif request.multipart:
            for part in request.multipart:
                lines.extend(
                    (
                        "",
                        "[[steps.multipart]]",
                        f"name = {_toml_value(part.name)}",
                        f"value = {_toml_value(part.value)}",
                    )
                )
        if request.timeout_seconds is not None:
            lines.append(f"timeout = {_toml_value(request.timeout_seconds)}")
        if request.follow_redirects:
            lines.append("follow_redirects = true")
        if not request.verify_tls:
            lines.append("verify_tls = false")
        if not step.assertions:
            lines.append("assertions = []")
        else:
            for assertion in step.assertions:
                lines.extend(
                    (
                        "",
                        "[[steps.assertions]]",
                        f"type = {_toml_value(assertion.kind)}",
                        f"expected = {_toml_value(assertion.expected)}",
                    )
                )
                if assertion.location is not None:
                    lines.append(f"path = {_toml_value(assertion.location)}")
                if assertion.operator is not None:
                    lines.append(f"operator = {_toml_value(assertion.operator)}")
    return "\n".join(lines) + "\n"


def _toml_value(value: Any) -> str:
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("scenario values must be finite")
        return repr(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    if isinstance(value, tuple):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    if isinstance(value, dict):
        if not all(isinstance(key, str) for key in value):
            raise ValueError("scenario object keys must be strings")
        return (
            "{ "
            + ", ".join(
                f"{_toml_value(key)} = {_toml_value(item)}" for key, item in sorted(value.items())
            )
            + " }"
        )
    raise ValueError("scenario values must be TOML-compatible")


def _preflight_paths(
    output_directory: Path,
    paths: tuple[Path, ...],
    force: bool,
) -> dict[Path, bool]:
    if output_directory.exists() and not output_directory.is_dir():
        raise ValueError("scenario destination must be a directory")
    existing: dict[Path, bool] = {}
    for path in paths:
        safe_relative_path(path.name)
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            existing[path] = False
            continue
        if stat.S_ISLNK(metadata.st_mode):
            raise ValueError("refusing to overwrite a scenario symlink")
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError("refusing to overwrite a non-regular scenario")
        if not force:
            raise FileExistsError(f"scenario already exists: {path.name}")
        existing[path] = True
    return existing


def _create_staging_directory(output_directory: Path) -> Path:
    prefix = f".{output_directory.name or 'scenarios'}-"
    for _ in range(8):
        staging = output_directory.parent / f"{prefix}{secrets.token_hex(16)}.partial"
        try:
            mkdir_exclusive(staging)
        except FileExistsError:
            continue
        return staging
    raise OSError("could not allocate scenario staging directory")


def _validate_staged(staging: Path, filenames: tuple[str, ...]) -> None:
    loader = ScenarioLoader()
    for filename in filenames:
        loader.load(staging / filename)


def _publish_existing(
    staging: Path,
    output_directory: Path,
    filenames: tuple[str, ...],
    existing: dict[Path, bool],
    rendered: tuple[str, ...],
) -> None:
    backups: dict[Path, Path] = {}
    published_new: list[tuple[Path, str, int]] = []
    try:
        for index, filename in enumerate(filenames, start=1):
            destination = output_directory / filename
            if existing[destination]:
                backup = staging / f".backup-{index}"
                safe_rename(destination, backup, replace_existing=False)
                backups[destination] = backup
            safe_rename(staging / filename, destination, replace_existing=False)
            if not existing[destination]:
                published_new.append((destination, rendered[index - 1], index))
    except BaseException:
        for destination, expected, index in reversed(published_new):
            _rollback_new_publication(
                destination,
                output_directory,
                staging,
                expected,
                index,
            )
        for destination, backup in reversed(tuple(backups.items())):
            if backup.exists():
                restore_backup_or_preserve(backup, destination)
        raise
    shutil.rmtree(staging, ignore_errors=True)


def _rollback_new_publication(
    destination: Path,
    output_directory: Path,
    staging: Path,
    expected: str,
    index: int,
) -> None:
    """Remove our new file or preserve a concurrent occupant as recovery."""

    rollback = staging / f".published-{index}"
    try:
        safe_rename(destination, rollback, replace_existing=False)
    except FileNotFoundError:
        return
    except (OSError, ValueError):
        return
    try:
        actual = rollback.read_bytes()
        if actual == expected.encode("utf-8"):
            try:
                rollback.unlink()
            except OSError:
                pass
            else:
                return
        recovery = output_directory / f".csbox-recovery-{secrets.token_hex(16)}.bak"
        safe_rename(rollback, recovery, replace_existing=False)
    except (OSError, ValueError):
        return


def _cleanup_unpublished_staging(staging: Path) -> None:
    if staging.is_symlink() or not staging.is_dir():
        return
    if any(staging.glob(".backup-*")) or any(staging.glob(".published-*")):
        return
    shutil.rmtree(staging, ignore_errors=True)


__all__ = ["ScenarioFile", "render_scenario_toml", "scenario_filename", "write_scenario_files"]
