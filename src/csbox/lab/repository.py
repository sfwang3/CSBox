from __future__ import annotations

import json
import os
import platform as platform_module
import tempfile
import uuid
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from csbox import __version__
from csbox.core.events import TerminalSize
from csbox.lab.captures import CaptureStore
from csbox.lab.models import SessionMetadata, SessionPaths


class SessionRepositoryError(RuntimeError):
    """A session could not be found or its metadata could not be persisted."""


@dataclass(frozen=True, slots=True)
class SessionSummary:
    paths: SessionPaths
    metadata: SessionMetadata
    capture_count: int = 0


class SessionRepository:
    """Own session directories and their lifecycle metadata."""

    def __init__(self, sessions_root: Path | str) -> None:
        self.root = Path(sessions_root)

    @classmethod
    def from_cwd(cls, cwd: Path | str) -> SessionRepository:
        return cls(Path(cwd) / ".csbox" / "sessions")

    def create_running(
        self,
        name: str,
        *,
        platform: str | None = None,
        shell: str,
        shell_version: str | None,
        size: TerminalSize,
        cwd: Path | str,
        session_id: str | None = None,
        started_at: datetime | None = None,
    ) -> SessionPaths:
        experiment_name = name.strip() or default_experiment_name()
        identifier = session_id or uuid.uuid4().hex
        if (
            not identifier
            or Path(identifier).name != identifier
            or any(separator in identifier for separator in ("/", "\\"))
            or identifier in {".", ".."}
        ):
            raise ValueError("session id must be a single safe path component")
        paths = SessionPaths(self.root / identifier)
        paths.root.mkdir(parents=True, exist_ok=False)
        metadata = SessionMetadata(
            id=identifier,
            name=experiment_name,
            status="running",
            startedAt=started_at or datetime.now(UTC),
            endedAt=None,
            platform=platform or platform_module.system().lower(),
            shell=shell,
            shellVersion=shell_version,
            initialRows=size.rows,
            initialColumns=size.columns,
            cwd=Path(cwd),
            csboxVersion=__version__,
        )
        try:
            _atomic_write_json(paths.metadata, metadata.model_dump(mode="json", by_alias=True))
        except BaseException:
            paths.root.rmdir()
            raise
        return paths

    def finish(
        self,
        paths: SessionPaths,
        status: str,
        *,
        exit_code: int | None = None,
        ended_at: datetime | None = None,
    ) -> SessionMetadata:
        if status not in {"completed", "interrupted", "failed"}:
            raise ValueError(f"invalid terminal session status: {status}")
        metadata = self._read_metadata(paths)
        updated = metadata.model_copy(
            update={"status": status, "ended_at": ended_at or datetime.now(UTC)}
        )
        document = updated.model_dump(mode="json", by_alias=True)
        if exit_code is not None:
            document["exitCode"] = exit_code
        _atomic_write_json(paths.metadata, document)
        return updated

    def list_sessions(self) -> tuple[SessionSummary, ...]:
        if not self.root.is_dir():
            return ()
        summaries: list[SessionSummary] = []
        for directory in self.root.iterdir():
            if not directory.is_dir() or directory.is_symlink():
                continue
            paths = SessionPaths(directory)
            try:
                metadata = self._read_metadata(paths)
            except (OSError, UnicodeError, ValueError, SessionRepositoryError):
                continue
            captures = CaptureStore(paths.captures).load()
            summaries.append(
                SessionSummary(paths=paths, metadata=metadata, capture_count=len(captures.captures))
            )
        summaries.sort(key=lambda item: item.metadata.started_at, reverse=True)
        return tuple(summaries)

    def latest(self) -> SessionSummary | None:
        sessions = self.list_sessions()
        return sessions[0] if sessions else None

    def resolve(self, identifier: str) -> SessionPaths:
        normalized = identifier.strip()
        if not normalized:
            raise SessionRepositoryError("会话标识不能为空。")
        sessions = self.list_sessions()
        exact_matches = [
            summary.paths for summary in sessions if summary.paths.root.name == normalized
        ]
        if exact_matches:
            return exact_matches[0]
        matches = [
            summary.paths for summary in sessions if summary.paths.root.name.startswith(normalized)
        ]
        if not matches:
            raise SessionRepositoryError(f"未找到会话：{identifier}")
        if len(matches) > 1:
            names = "、".join(path.root.name for path in matches[:3])
            raise SessionRepositoryError(f"会话前缀不唯一：{identifier}（{names}）")
        return matches[0]

    def _read_metadata(self, paths: SessionPaths) -> SessionMetadata:
        try:
            return SessionMetadata.model_validate_json(paths.metadata.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError) as exc:
            raise SessionRepositoryError(f"会话 metadata 无效：{paths.metadata}") from exc


def default_experiment_name(now: datetime | None = None) -> str:
    timestamp = (now or datetime.now(UTC)).astimezone().strftime("%Y%m%d-%H%M%S")
    return f"实验-{timestamp}"


def _atomic_write_json(path: Path, value: object) -> None:
    payload = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        with suppress(OSError):
            temporary.unlink(missing_ok=True)
        raise
