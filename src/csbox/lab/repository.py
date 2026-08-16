from __future__ import annotations

import errno
import json
import os
import platform as platform_module
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from csbox import __version__
from csbox.core.events import TerminalSize
from csbox.core.safe_paths import (
    atomic_write_bytes,
    ensure_private_directory,
    mkdir_exclusive,
    read_regular_text,
)
from csbox.lab.captures import CaptureStore
from csbox.lab.models import SessionMetadata, SessionPaths
from csbox.lab.recorder import AsciicastV3Reader, RecorderError

_MAX_SESSION_METADATA_BYTES = 4 * 1024 * 1024


def _raise_if_windows_lock_contention(error: OSError) -> None:
    if os.name == "nt" and error.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
        raise BlockingIOError from error


class SessionRepositoryError(RuntimeError):
    """A session could not be found or its metadata could not be persisted."""


@dataclass(frozen=True, slots=True)
class SessionSummary:
    paths: SessionPaths
    metadata: SessionMetadata
    capture_count: int = 0
    recording_warnings: tuple[str, ...] = ()


class _SessionOwner:
    """Hold a process-scoped lock so a live owner is distinguishable from stale metadata."""

    def __init__(self, path: Path) -> None:
        self.path = path
        try:
            self._stream = path.open("a+b")
        except OSError as exc:
            _raise_if_windows_lock_contention(exc)
            raise
        self._locked = False
        try:
            self._stream.seek(0)
            try:
                marker = self._stream.read(1)
            except OSError as exc:
                _raise_if_windows_lock_contention(exc)
                raise
            if marker != b"\0":
                self._stream.seek(0)
                self._stream.write(b"\0")
                self._stream.flush()
                self._stream.seek(0)
            self._lock()
            self._locked = True
        except BaseException:
            self._stream.close()
            raise

    def _lock(self) -> None:
        if os.name == "nt":
            import msvcrt

            try:
                self._stream.seek(0)
                msvcrt.locking(self._stream.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                _raise_if_windows_lock_contention(exc)
                raise
            return
        import fcntl

        fcntl.flock(self._stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def release(self) -> None:
        if self._stream.closed:
            return
        try:
            if self._locked:
                if os.name == "nt":
                    import msvcrt

                    self._stream.seek(0)
                    msvcrt.locking(self._stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(self._stream.fileno(), fcntl.LOCK_UN)
                self._locked = False
        finally:
            self._stream.close()


class SessionRepository:
    """Own session directories and their lifecycle metadata."""

    def __init__(self, sessions_root: Path | str) -> None:
        self.root = Path(sessions_root)
        self._owners: dict[Path, _SessionOwner] = {}
        self._owner_tokens: dict[Path, str] = {}

    @classmethod
    def from_cwd(cls, cwd: Path | str) -> SessionRepository:
        return cls(Path(cwd) / ".csbox" / "sessions")

    def create_starting(
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
        ensure_private_directory(self.root)
        mkdir_exclusive(paths.root)
        owner_token = secrets.token_hex(16)
        metadata = SessionMetadata(
            id=identifier,
            name=experiment_name,
            status="starting",
            statusReason=None,
            exitCode=None,
            ownerPid=os.getpid(),
            ownerToken=owner_token,
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
            owner = _SessionOwner(paths.owner_lock)
        except BaseException:
            paths.owner_lock.unlink(missing_ok=True)
            paths.metadata.unlink(missing_ok=True)
            paths.root.rmdir()
            raise
        try:
            _atomic_write_json(paths.metadata, metadata.model_dump(mode="json", by_alias=True))
        except BaseException:
            owner.release()
            paths.metadata.unlink(missing_ok=True)
            paths.owner_lock.unlink(missing_ok=True)
            paths.root.rmdir()
            raise
        self._owners[paths.root] = owner
        self._owner_tokens[paths.root] = owner_token
        return paths

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
        """Backward-compatible name for creating a session before spawn."""

        return self.create_starting(
            name,
            platform=platform,
            shell=shell,
            shell_version=shell_version,
            size=size,
            cwd=cwd,
            session_id=session_id,
            started_at=started_at,
        )

    def mark_running(self, paths: SessionPaths) -> SessionMetadata:
        metadata = self._read_metadata(paths)
        self._require_owner(paths, metadata)
        if metadata.status not in {"starting", "running"}:
            raise SessionRepositoryError("只能将 starting session 标记为 running。")
        updated = metadata.model_copy(update={"status": "running", "status_reason": None})
        _atomic_write_json(paths.metadata, updated.model_dump(mode="json", by_alias=True))
        return updated

    def finish(
        self,
        paths: SessionPaths,
        status: str,
        *,
        exit_code: int | None = None,
        reason: str | None = None,
        ended_at: datetime | None = None,
    ) -> SessionMetadata:
        if status not in {"completed", "interrupted", "failed"}:
            raise ValueError(f"invalid terminal session status: {status}")
        metadata = self._read_metadata(paths)
        self._require_owner(paths, metadata)
        updated = metadata.model_copy(
            update={
                "status": status,
                "status_reason": reason,
                "exit_code": exit_code,
                "ended_at": ended_at or datetime.now(UTC),
            }
        )
        _atomic_write_json(paths.metadata, updated.model_dump(mode="json", by_alias=True))
        self._release_owner(paths)
        return updated

    def recover_stale_running(self) -> tuple[SessionPaths, ...]:
        """Recover sessions whose process-scoped owner lock is no longer held."""

        recovered: list[SessionPaths] = []
        if not self.root.is_dir():
            return ()
        for directory in self.root.iterdir():
            if not directory.is_dir() or directory.is_symlink():
                continue
            paths = SessionPaths(directory)
            try:
                metadata = self._read_metadata(paths)
            except (OSError, UnicodeError, ValueError, SessionRepositoryError):
                continue
            _, was_recovered = self._recover_stale_metadata(paths, metadata)
            if was_recovered:
                recovered.append(paths)
        return tuple(recovered)

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
            metadata, _ = self._recover_stale_metadata(paths, metadata)
            if paths.cast.is_symlink():
                continue
            recording_warnings: tuple[str, ...] = ()
            if paths.cast.is_file():
                try:
                    recording_warnings = AsciicastV3Reader(paths.cast).read().warnings
                except (OSError, UnicodeError, RecorderError) as exc:
                    recording_warnings = (f"recording is not readable: {type(exc).__name__}",)
            else:
                recording_warnings = ("recording file is missing",)
            captures = CaptureStore(paths.captures).load()
            summaries.append(
                SessionSummary(
                    paths=paths,
                    metadata=metadata,
                    capture_count=len(captures.captures),
                    recording_warnings=recording_warnings,
                )
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
        return load_session_metadata(paths)

    def _recover_stale_metadata(
        self,
        paths: SessionPaths,
        metadata: SessionMetadata,
    ) -> tuple[SessionMetadata, bool]:
        if metadata.status not in {"starting", "running"}:
            return metadata, False
        if paths.root in self._owners:
            return metadata, False
        owner = self._try_acquire_owner(paths)
        if owner is None:
            return metadata, False
        try:
            metadata = self._read_metadata(paths)
        except (OSError, UnicodeError, ValueError, SessionRepositoryError):
            return metadata, False
        if metadata.status not in {"starting", "running"}:
            return metadata, False
        updated = metadata.model_copy(
            update={
                "status": "interrupted",
                "status_reason": "session owner is no longer active",
                "ended_at": datetime.now(UTC),
            }
        )
        try:
            _atomic_write_json(paths.metadata, updated.model_dump(mode="json", by_alias=True))
        except BaseException:
            return metadata, False
        finally:
            owner.release()
        return updated, True

    def _try_acquire_owner(self, paths: SessionPaths) -> _SessionOwner | None:
        if paths.owner_lock.is_symlink():
            return None
        try:
            return _SessionOwner(paths.owner_lock)
        except BlockingIOError:
            return None

    def _release_owner(self, paths: SessionPaths) -> None:
        owner = self._owners.pop(paths.root, None)
        self._owner_tokens.pop(paths.root, None)
        if owner is not None:
            owner.release()

    def release_owner(self, paths: SessionPaths) -> None:
        """Release a session owner after terminal metadata can no longer be written."""

        self._release_owner(paths)

    def _require_owner(self, paths: SessionPaths, metadata: SessionMetadata) -> None:
        owner = self._owners.get(paths.root)
        owner_token = self._owner_tokens.get(paths.root)
        if owner is None or owner_token is None or metadata.owner_token != owner_token:
            raise SessionRepositoryError("session owner is not active")


def load_session_metadata(paths: SessionPaths) -> SessionMetadata:
    """Load bounded session metadata through the Lab repository boundary."""

    try:
        return SessionMetadata.model_validate_json(
            read_regular_text(paths.metadata, max_bytes=_MAX_SESSION_METADATA_BYTES)
        )
    except (OSError, UnicodeError, ValueError, RecursionError) as exc:
        raise SessionRepositoryError("会话 metadata 无效。") from exc


def default_experiment_name(now: datetime | None = None) -> str:
    timestamp = (now or datetime.now(UTC)).astimezone().strftime("%Y%m%d-%H%M%S")
    return f"实验-{timestamp}"


def _atomic_write_json(path: Path, value: object) -> None:
    payload = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    atomic_write_bytes(path, payload)
