from __future__ import annotations

import json
import os
import secrets
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from csbox.core.events import TerminalSize
from csbox.core.safe_paths import (
    atomic_write_bytes,
    ensure_private_directory,
    mkdir_exclusive,
    read_regular_text,
)
from csbox.core.shell import ShellUnavailableError
from csbox.core.terminal import TerminalBackendError

_MAX_LAUNCH_FILE_BYTES = 128 * 1024
_MAX_EXCEPTION_CHAIN_CHARS = 8 * 1024
_DEFAULT_READY_TIMEOUT_SECONDS = 15.0
_DEFAULT_POLL_INTERVAL_SECONDS = 0.05


class LaunchProtocolError(RuntimeError):
    """A dedicated-host intent or status file is invalid."""


class LaunchClaimError(RuntimeError):
    """Another host already owns a launch intent."""


class LaunchCancelledError(RuntimeError):
    """A launcher timed out or cancelled an intent before the host was ready."""


class LabLaunchError(RuntimeError):
    """The parent launcher could not establish a dedicated Lab host."""

    def __init__(self, kind: str, message: str, cause: BaseException | None = None) -> None:
        super().__init__(message)
        self.kind = kind
        self.cause = cause


@dataclass(frozen=True, slots=True)
class LaunchIntent:
    token: str
    name: str
    shell: str
    command: tuple[str, ...]
    cwd: Path
    size: TerminalSize
    request_path: Path

    @property
    def directory(self) -> Path:
        return self.request_path.parent

    @property
    def claim_path(self) -> Path:
        return self.directory / "claim"

    @property
    def cancelled_path(self) -> Path:
        return self.directory / "cancelled"

    @property
    def status_path(self) -> Path:
        return self.directory / "status.json"


@dataclass(frozen=True, slots=True)
class LaunchStatus:
    state: str
    session_id: str | None = None
    session_status: str | None = None
    exit_code: int | None = None
    error_kind: str | None = None
    error_type: str | None = None
    error_message: str | None = None
    exception_chain: str = ""


@dataclass(frozen=True, slots=True)
class LabLaunchResult:
    session_id: str
    status: str
    intent: LaunchIntent


class LaunchIntentStore:
    """Persist a bounded, private launcher/host handshake."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)

    def reserve(
        self,
        *,
        name: str,
        shell: str,
        command: Sequence[str],
        cwd: Path | str,
        size: TerminalSize,
    ) -> LaunchIntent:
        ensure_private_directory(self.root)
        for _ in range(8):
            token = secrets.token_hex(16)
            directory = self.root / token
            try:
                mkdir_exclusive(directory)
            except FileExistsError:
                continue
            request_path = directory / "request.json"
            intent = LaunchIntent(
                token=token,
                name=name.strip() or "实验",
                shell=shell,
                command=_validate_command(command),
                cwd=Path(cwd).absolute(),
                size=size,
                request_path=request_path,
            )
            payload = {
                "version": 1,
                "token": intent.token,
                "name": intent.name,
                "shell": intent.shell,
                "command": list(intent.command),
                "cwd": str(intent.cwd),
                "size": {"columns": size.columns, "rows": size.rows},
            }
            atomic_write_bytes(
                request_path,
                (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
            )
            self._write_status(intent, LaunchStatus("reserved"))
            return intent
        raise OSError("could not reserve a unique dedicated Lab launch intent")

    @staticmethod
    def load(request_path: Path | str) -> LaunchIntent:
        path = Path(request_path)
        try:
            payload = json.loads(read_regular_text(path, max_bytes=_MAX_LAUNCH_FILE_BYTES))
        except (OSError, UnicodeError, ValueError, RecursionError) as exc:
            raise LaunchProtocolError("dedicated Lab launch intent is unreadable") from exc
        if not isinstance(payload, dict):
            raise LaunchProtocolError("dedicated Lab launch intent must be an object")
        token = _required_string(payload, "token")
        name = _required_string(payload, "name")
        shell = _required_string(payload, "shell")
        command_value = payload.get("command")
        if not isinstance(command_value, list):
            raise LaunchProtocolError("dedicated Lab launch command is invalid")
        command = _validate_command(command_value)
        cwd_value = _required_string(payload, "cwd")
        cwd = Path(cwd_value)
        if not cwd.is_absolute():
            raise LaunchProtocolError("dedicated Lab launch cwd must be absolute")
        size_value = payload.get("size")
        if not isinstance(size_value, dict):
            raise LaunchProtocolError("dedicated Lab launch size is invalid")
        try:
            size = TerminalSize(
                _required_int(size_value, "columns"),
                _required_int(size_value, "rows"),
            )
        except (TypeError, ValueError) as exc:
            raise LaunchProtocolError("dedicated Lab launch size is invalid") from exc
        if payload.get("version") != 1:
            raise LaunchProtocolError("unsupported dedicated Lab launch intent version")
        _validate_token(token)
        return LaunchIntent(token, name, shell, command, cwd, size, path)

    @staticmethod
    def load_status(intent: LaunchIntent) -> LaunchStatus:
        try:
            payload = json.loads(
                read_regular_text(intent.status_path, max_bytes=_MAX_LAUNCH_FILE_BYTES)
            )
        except (OSError, UnicodeError, ValueError, RecursionError) as exc:
            raise LaunchProtocolError("dedicated Lab launch status is unreadable") from exc
        if not isinstance(payload, dict):
            raise LaunchProtocolError("dedicated Lab launch status must be an object")
        state = payload.get("state")
        if not isinstance(state, str) or state not in {
            "reserved",
            "claimed",
            "ready",
            "failed",
            "cancelled",
            "finished",
        }:
            raise LaunchProtocolError("dedicated Lab launch state is invalid")
        return LaunchStatus(
            state=state,
            session_id=_optional_string(payload, "sessionId"),
            session_status=_optional_string(payload, "sessionStatus"),
            exit_code=_optional_int(payload, "exitCode"),
            error_kind=_optional_string(payload, "errorKind"),
            error_type=_optional_string(payload, "errorType"),
            error_message=_optional_string(payload, "errorMessage"),
            exception_chain=str(payload.get("exceptionChain") or ""),
        )

    def claim(self, intent: LaunchIntent) -> None:
        _require_store(self, intent)
        with self._transition_lock(intent):
            if intent.cancelled_path.exists():
                raise LaunchCancelledError("dedicated Lab launch was cancelled before host claim")
            status = self.load_status(intent)
            if status.state != "reserved":
                raise LaunchClaimError("dedicated Lab launch is no longer claimable")
            try:
                mkdir_exclusive(intent.claim_path)
            except FileExistsError as exc:
                raise LaunchClaimError("dedicated Lab launch already has a host owner") from exc
            if intent.cancelled_path.exists():
                raise LaunchCancelledError("dedicated Lab launch was cancelled during host claim")
            self._write_status(intent, LaunchStatus("claimed"))

    def mark_ready(self, intent: LaunchIntent, *, session_id: str) -> None:
        _require_store(self, intent)
        _validate_session_id(session_id)
        with self._transition_lock(intent):
            if intent.cancelled_path.exists():
                raise LaunchCancelledError("dedicated Lab launch was cancelled before READY")
            status = self.load_status(intent)
            if status.state == "ready" and status.session_id == session_id:
                return
            if status.state != "claimed":
                raise LaunchProtocolError("dedicated Lab host reported READY from an invalid state")
            self._write_status(intent, LaunchStatus("ready", session_id=session_id))

    def mark_failed(
        self,
        intent: LaunchIntent,
        *,
        error_kind: str,
        error: BaseException | None = None,
        message: str | None = None,
    ) -> None:
        _require_store(self, intent)
        with self._transition_lock(intent):
            current = self.load_status(intent)
            if current.state in {"ready", "finished"}:
                return
            if current.state == "cancelled":
                return
            self._write_status(
                intent,
                LaunchStatus(
                    "failed",
                    error_kind=error_kind,
                    error_type=type(error).__name__ if error is not None else None,
                    error_message=message or (str(error).strip() if error is not None else None),
                    exception_chain=_exception_chain(error) if error is not None else "",
                ),
            )

    def cancel(self, intent: LaunchIntent) -> LaunchStatus:
        _require_store(self, intent)
        with self._transition_lock(intent):
            current = self.load_status(intent)
            if current.state in {"ready", "finished", "failed", "cancelled"}:
                return current
            with suppress(FileExistsError):
                mkdir_exclusive(intent.cancelled_path)
            current = self.load_status(intent)
            if current.state in {"ready", "finished", "failed"}:
                return current
            self._write_status(intent, LaunchStatus("cancelled"))
            return self.load_status(intent)

    def mark_finished(
        self,
        intent: LaunchIntent,
        *,
        session_id: str,
        session_status: str,
        exit_code: int | None = None,
        error_kind: str | None = None,
        error: BaseException | None = None,
    ) -> None:
        _require_store(self, intent)
        _validate_session_id(session_id)
        with self._transition_lock(intent):
            self._write_status(
                intent,
                LaunchStatus(
                    "finished",
                    session_id=session_id,
                    session_status=session_status,
                    exit_code=exit_code,
                    error_kind=error_kind,
                    error_type=type(error).__name__ if error is not None else None,
                    error_message=str(error).strip() if error is not None else None,
                    exception_chain=_exception_chain(error) if error is not None else "",
                ),
            )

    @contextmanager
    def _transition_lock(self, intent: LaunchIntent):
        with _LaunchTransitionLock(intent.directory / "transition.lock"):
            yield

    def _write_status(self, intent: LaunchIntent, status: LaunchStatus) -> None:
        payload = {
            "state": status.state,
            "sessionId": status.session_id,
            "sessionStatus": status.session_status,
            "exitCode": status.exit_code,
            "errorKind": status.error_kind,
            "errorType": status.error_type,
            "errorMessage": status.error_message,
            "exceptionChain": status.exception_chain[:_MAX_EXCEPTION_CHAIN_CHARS],
        }
        atomic_write_bytes(
            intent.status_path,
            (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
        )


class _LaunchTransitionLock:
    """Hold a short cross-process lock while changing the launch state."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.stream: Any | None = None
        self.locked = False

    def __enter__(self) -> _LaunchTransitionLock:
        if self.path.is_symlink():
            raise LaunchProtocolError("dedicated Lab transition lock is a symlink")
        stream = self.path.open("a+b")
        try:
            stream.seek(0)
            if stream.read(1) != b"\0":
                stream.seek(0)
                stream.write(b"\0")
                stream.flush()
                stream.seek(0)
            if os.name == "nt":
                import msvcrt

                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            self.stream = stream
            self.locked = True
            return self
        except BaseException:
            stream.close()
            raise

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        del exc_type, exc, traceback
        stream = self.stream
        if stream is None:
            return
        try:
            if self.locked:
                if os.name == "nt":
                    import msvcrt

                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
                self.locked = False
        finally:
            stream.close()


class WindowsTerminalLabLauncher:
    """Start the dedicated WT surface and wait for the host's bounded READY state."""

    def __init__(
        self,
        *,
        launch_root: Path | str | None = None,
        wt_executable: str | None = None,
        wt_locator: Callable[[], str | None] | None = None,
        process_factory: Callable[..., Any] | None = None,
        timeout_seconds: float = _DEFAULT_READY_TIMEOUT_SECONDS,
        poll_interval_seconds: float = _DEFAULT_POLL_INTERVAL_SECONDS,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.launch_root = Path(launch_root or Path.cwd() / ".csbox" / "launches")
        self.wt_executable = wt_executable
        self.wt_locator = wt_locator
        self.process_factory = process_factory or _spawn_wt_client
        self.timeout_seconds = timeout_seconds
        self.poll_interval_seconds = poll_interval_seconds
        self.monotonic = monotonic
        self.sleep = sleep
        self.last_intent: LaunchIntent | None = None
        self.last_intent_status: LaunchStatus | None = None

    def start(
        self,
        *,
        name: str,
        shell: str,
        command: Sequence[str],
        cwd: Path | str,
        size: TerminalSize,
    ) -> LabLaunchResult:
        store = LaunchIntentStore(self.launch_root)
        intent = store.reserve(
            name=name,
            shell=shell,
            command=command,
            cwd=cwd,
            size=size,
        )
        self.last_intent = intent
        executable = self._find_wt()
        if executable is None:
            error = FileNotFoundError("wt.exe")
            store.mark_failed(
                intent,
                error_kind="wt_launch_failure",
                error=error,
                message="Windows Terminal executable was not found",
            )
            self.last_intent_status = store.load_status(intent)
            raise LabLaunchError(
                "wt_launch_failure",
                "Windows Terminal could not be started.",
                error,
            ) from error

        argv = self._build_command(executable, intent)
        try:
            process = self.process_factory(
                argv,
                cwd=str(intent.cwd),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except BaseException as exc:
            store.mark_failed(
                intent,
                error_kind="wt_launch_failure",
                error=exc,
                message="Windows Terminal launch client failed",
            )
            self.last_intent_status = store.load_status(intent)
            raise LabLaunchError(
                "wt_launch_failure", "Windows Terminal could not be started.", exc
            ) from exc

        deadline = self.monotonic() + self.timeout_seconds
        while True:
            status = store.load_status(intent)
            self.last_intent_status = status
            if status.state == "ready" and status.session_id is not None:
                return LabLaunchResult(status.session_id, "running", intent)
            if status.state == "finished" and status.session_id is not None:
                if status.session_status == "failed":
                    self._raise_for_status(status)
                return LabLaunchResult(
                    status.session_id, status.session_status or "completed", intent
                )
            if status.state == "failed":
                self._raise_for_status(status)
            if status.state == "cancelled":
                raise LabLaunchError(
                    "launcher_timeout",
                    "Dedicated Lab host launch was cancelled before READY.",
                )
            returncode = process.poll()
            if returncode is not None and returncode != 0:
                error = RuntimeError(f"wt.exe exited before READY with code {returncode}")
                store.mark_failed(
                    intent,
                    error_kind="wt_launch_failure",
                    error=error,
                    message="Windows Terminal launch client exited before READY",
                )
                self.last_intent_status = store.load_status(intent)
                raise LabLaunchError(
                    "wt_launch_failure", "Windows Terminal could not be started.", error
                ) from error
            if self.monotonic() >= deadline:
                status = store.cancel(intent)
                self.last_intent_status = status
                raise LabLaunchError(
                    "launcher_timeout",
                    "Dedicated Lab host did not report READY before the bounded timeout.",
                )
            self.sleep(min(self.poll_interval_seconds, max(0.0, deadline - self.monotonic())))

    def _find_wt(self) -> str | None:
        if self.wt_executable is not None:
            return self.wt_executable
        if self.wt_locator is not None:
            return self.wt_locator()
        return shutil.which("wt.exe") or shutil.which("wt")

    @staticmethod
    def _build_command(executable: str, intent: LaunchIntent) -> tuple[str, ...]:
        return (
            executable,
            "--window",
            "new",
            "new-tab",
            "--title",
            "CSBox Lab",
            "--startingDirectory",
            str(intent.cwd),
            "--inheritEnvironment",
            sys.executable,
            "-m",
            "csbox",
            "lab",
            "_host",
            "--intent",
            str(intent.request_path),
            "--token",
            intent.token,
        )

    @staticmethod
    def _raise_for_status(status: LaunchStatus) -> None:
        cause = RemoteLaunchCause(status.exception_chain or status.error_message or "host failed")
        kind = status.error_kind or "host_startup_failure"
        message = status.error_message or "Dedicated Lab host failed before READY."
        error = LabLaunchError(kind, message, cause)
        raise error from cause


class RemoteLaunchCause(RuntimeError):
    """Bounded diagnostics transported from the hidden host to the launcher."""


def run_dedicated_lab_host(
    request_path: Path | str,
    token: str,
    *,
    service_factory: Callable[..., object] | None = None,
) -> int:
    """Run the hidden host process without writing launcher diagnostics to the terminal body."""

    intent = LaunchIntentStore.load(request_path)
    if token != intent.token:
        return _record_host_failure(
            intent,
            error_kind="host_startup_failure",
            error=LaunchProtocolError("dedicated Lab launch token mismatch"),
        )
    store = LaunchIntentStore(intent.directory.parent)
    try:
        store.claim(intent)
    except (LaunchClaimError, LaunchCancelledError):
        return 1
    ready = False
    ready_session_id: str | None = None
    try:
        if service_factory is None:
            from csbox.lab.service import create_lab_service

            service_factory = create_lab_service
        service = service_factory(intent.cwd, dedicated_host=True)

        def mark_ready(paths: object) -> None:
            nonlocal ready, ready_session_id
            session_root = getattr(getattr(paths, "root", None), "name", None)
            if not isinstance(session_root, str):
                raise LaunchProtocolError("dedicated Lab service did not return a session id")
            store.mark_ready(intent, session_id=session_root)
            ready_session_id = session_root
            ready = True

        result = service.start(  # type: ignore[attr-defined]
            intent.name,
            shell=intent.shell,
            command=intent.command,
            size=intent.size,
            dedicated_host=True,
            ready_callback=mark_ready,
        )
    except KeyboardInterrupt as exc:
        if ready:
            _finish_from_result(store, intent, None, ready_session_id, "interrupted", None, exc)
            return 0
        return _record_host_failure(intent, error_kind="host_startup_failure", error=exc)
    except BaseException as exc:
        if isinstance(exc, LaunchCancelledError):
            return 1
        if ready:
            _finish_from_result(store, intent, None, ready_session_id, "failed", None, exc)
            return 1
        error_kind = _host_error_kind(exc)
        return _record_host_failure(intent, error_kind=error_kind, error=exc)

    if not ready or ready_session_id is None:
        error = LaunchProtocolError("dedicated Lab host returned before reporting READY")
        return _record_host_failure(intent, error_kind="host_startup_failure", error=error)
    session = getattr(result, "session", None)
    session_root = getattr(getattr(session, "root", None), "name", None)
    if not isinstance(session_root, str):
        error = LaunchProtocolError("dedicated Lab service returned no session id")
        store.mark_finished(
            intent,
            session_id=ready_session_id,
            session_status="failed",
            error_kind="host_startup_failure",
            error=error,
        )
        return 1
    if session_root != ready_session_id:
        error = LaunchProtocolError("dedicated Lab service changed session identity after READY")
        store.mark_finished(
            intent,
            session_id=ready_session_id,
            session_status="failed",
            error_kind="host_startup_failure",
            error=error,
        )
        return 1
    session_status = str(getattr(result, "status", "completed"))
    exit_code = getattr(result, "exit_code", None)
    store.mark_finished(
        intent,
        session_id=session_root,
        session_status=session_status,
        exit_code=exit_code,
    )
    return 0 if session_status != "failed" else 1


def _finish_from_result(
    store: LaunchIntentStore,
    intent: LaunchIntent,
    result: object | None,
    session_id: str | None,
    session_status: str,
    exit_code: int | None,
    error: BaseException,
) -> None:
    session = getattr(result, "session", None) if result is not None else None
    session_root = session_id or getattr(getattr(session, "root", None), "name", None)
    if isinstance(session_root, str):
        store.mark_finished(
            intent,
            session_id=session_root,
            session_status=session_status,
            exit_code=exit_code,
            error_kind="runtime_backend_failure",
            error=error,
        )
    else:
        _record_host_failure(intent, error_kind="runtime_backend_failure", error=error)


def _record_host_failure(intent: LaunchIntent, *, error_kind: str, error: BaseException) -> int:
    store = LaunchIntentStore(intent.directory.parent)
    try:
        store.mark_failed(intent, error_kind=error_kind, error=error)
    except BaseException:
        return 1
    return 1


def _host_error_kind(error: BaseException) -> str:
    if isinstance(error, ShellUnavailableError):
        return "shell_startup_failure"
    if isinstance(error, TerminalBackendError):
        return "child_shell_startup_failure"
    return "host_startup_failure"


def _spawn_wt_client(argv: Sequence[str], **kwargs: object) -> Any:
    return subprocess.Popen(tuple(argv), **kwargs)


def _require_store(store: LaunchIntentStore, intent: LaunchIntent) -> None:
    if intent.directory.parent != store.root:
        raise LaunchProtocolError("dedicated Lab intent belongs to another store")


def _validate_command(command: Sequence[str]) -> tuple[str, ...]:
    values = tuple(command)
    if not values or any(
        not isinstance(value, str) or not value or "\x00" in value for value in values
    ):
        raise ValueError("dedicated Lab command must contain non-empty strings")
    return values


def _validate_token(token: str) -> None:
    if (
        not token
        or len(token) > 128
        or any(character not in "0123456789abcdef-" for character in token)
    ):
        raise LaunchProtocolError("dedicated Lab launch token is invalid")


def _validate_session_id(session_id: str) -> None:
    if not session_id or Path(session_id).name != session_id or session_id in {".", ".."}:
        raise LaunchProtocolError("dedicated Lab session id is invalid")


def _required_string(payload: dict[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value or "\x00" in value:
        raise LaunchProtocolError(f"dedicated Lab launch field {key} is invalid")
    return value


def _required_int(payload: dict[str, object], key: str) -> int:
    value = payload.get(key)
    if type(value) is not int:
        raise TypeError(key)
    return value


def _optional_string(payload: dict[str, object], key: str) -> str | None:
    value = payload.get(key)
    if value is None:
        return None
    return value if isinstance(value, str) else None


def _optional_int(payload: dict[str, object], key: str) -> int | None:
    value = payload.get(key)
    if value is None:
        return None
    return value if type(value) is int else None


def _exception_chain(error: BaseException | None) -> str:
    parts: list[str] = []
    current = error
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        detail = str(current).strip()
        parts.append(f"{type(current).__name__}: {detail}" if detail else type(current).__name__)
        linked = getattr(current, "cause", None)
        if not isinstance(linked, BaseException):
            linked = current.__cause__ or current.__context__
        current = linked
    return " -> ".join(parts)[:_MAX_EXCEPTION_CHAIN_CHARS]
