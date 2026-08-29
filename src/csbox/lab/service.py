from __future__ import annotations

import os
import platform
from collections.abc import Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from csbox.config.loader import load_config
from csbox.config.models import CSBoxConfig
from csbox.core.events import TerminalSize
from csbox.core.shell import (
    ShellProfile,
    ShellUnavailableError,
    detect_shell_version,
    select_shell,
    supported_shell_profiles,
)
from csbox.core.terminal import TerminalBackend, create_terminal_backend
from csbox.lab.captures import CaptureStore
from csbox.lab.dispatcher import TerminalEventDispatcher
from csbox.lab.exporter import LabExporter, LabExportResult
from csbox.lab.fonts import FontResolver
from csbox.lab.keymap import CaptureBindingAdvisory, CaptureBindingProbe
from csbox.lab.models import SessionPaths
from csbox.lab.proxy import (
    FileInputAdapter,
    FileOutputAdapter,
    RawTerminalState,
    TerminalCleanupError,
    TerminalProxy,
    TerminalStatus,
    current_terminal_size,
)
from csbox.lab.recorder import AsciicastV3Recorder
from csbox.lab.renderer import RenderTheme, TerminalEvidenceRenderer
from csbox.lab.repository import SessionRepository, SessionSummary, default_experiment_name
from csbox.lab.screen import TerminalEmulator
from csbox.lab.status import LabSurfacePresenter
from csbox.lab.surface import AlternateScreenSurface
from csbox.lab.windows_host import WindowsTerminalLabLauncher


@dataclass(frozen=True, slots=True)
class LabRunResult:
    session: SessionPaths
    status: str
    exit_code: int | None
    advisory: str
    status_events: tuple[TerminalStatus, ...] = ()


class LabService:
    """Orchestrate session lifecycle while keeping presentation out of the proxy."""

    def __init__(
        self,
        *,
        repository: SessionRepository,
        config: CSBoxConfig,
        cwd: Path | str,
        backend_factory: Callable[[], TerminalBackend] = create_terminal_backend,
        input_adapter_factory: Callable[[], object] = FileInputAdapter,
        output_adapter_factory: Callable[[], object] = FileOutputAdapter,
        terminal_state_factory: Callable[[], object] | None = None,
        terminal_surface_factory: Callable[[object], object] | None = None,
        windows_launcher: WindowsTerminalLabLauncher | None = None,
    ) -> None:
        self.repository = repository
        self.config = config
        self.cwd = Path(cwd)
        self.backend_factory = backend_factory
        self.input_adapter_factory = input_adapter_factory
        self.output_adapter_factory = output_adapter_factory
        self.terminal_state_factory = terminal_state_factory
        self.terminal_surface_factory = terminal_surface_factory
        self.windows_launcher = windows_launcher

    def list(self) -> tuple[SessionSummary, ...]:
        return self.repository.list_sessions()

    def capture_advisory(self, adapter: object | None = None) -> CaptureBindingAdvisory:
        """Return the host-key warning before entering the raw terminal loop."""

        selected_adapter = adapter if adapter is not None else self.input_adapter_factory()
        return CaptureBindingProbe(
            self.config.lab.capture_key,
            input_supported=bool(getattr(selected_adapter, "supports_capture", True)),
        ).probe()

    def resolve_shell(self, requested: str | None = None) -> ShellProfile:
        """Resolve a launchable shell through the established selection boundary."""

        return select_shell(
            requested,
            self.config.lab.shell,
            system=platform.system(),
            environ=os.environ,
        )

    def available_shells(self) -> tuple[ShellProfile, ...]:
        """Return launchable profiles with the automatic choice first."""

        available: list[ShellProfile] = []
        seen: set[str] = set()
        requested_values: tuple[str | None, ...] = (
            None,
            *(profile.kind.value for profile in supported_shell_profiles()),
        )
        for requested in requested_values:
            try:
                profile = self.resolve_shell(requested)
            except ShellUnavailableError:
                continue
            if profile.kind.value in seen:
                continue
            seen.add(profile.kind.value)
            available.append(profile)
        return tuple(available)

    def start(
        self,
        name: str | None = None,
        *,
        shell: str | None = None,
        command: Sequence[str] | None = None,
        size: TerminalSize | None = None,
        dedicated_host: bool = False,
        ready_callback: Callable[[SessionPaths], object] | None = None,
    ) -> LabRunResult:
        selected_size = size or current_terminal_size()
        experiment_name = name or default_experiment_name()
        profile = self.resolve_shell(shell)
        shell_version = detect_shell_version(profile)
        adapter = self.input_adapter_factory()
        advisory = self.capture_advisory(adapter)
        selected_command = tuple(command or profile.command)
        if self.windows_launcher is not None and not dedicated_host:
            launch = self.windows_launcher.start(
                name=experiment_name,
                shell=profile.kind.value,
                command=selected_command,
                cwd=self.cwd,
                size=selected_size,
            )
            return LabRunResult(
                SessionPaths(self.repository.root / launch.session_id),
                launch.status,
                None,
                advisory.message,
            )
        paths = self.repository.create_starting(
            experiment_name,
            shell=profile.kind.value,
            shell_version=shell_version,
            size=selected_size,
            cwd=self.cwd,
        )
        recorder: AsciicastV3Recorder | None = None
        exit_code: int | None = None
        status = "failed"
        failure: BaseException | None = None
        status_reason: str | None = None
        status_events: tuple[TerminalStatus, ...] = ()
        try:
            recorder = AsciicastV3Recorder(
                paths.cast,
                columns=selected_size.columns,
                rows=selected_size.rows,
                env=dict(os.environ),
            )
            emulator = TerminalEmulator(columns=selected_size.columns, rows=selected_size.rows)
            captures = CaptureStore(paths.captures)
            dispatcher = TerminalEventDispatcher([recorder])

            def create_capture(snapshot, timestamp):
                return captures.create_capture(snapshot, timestamp, cwd=self.cwd)

            def rollback_capture(value: object) -> None:
                capture_id = getattr(value, "capture_id", None)
                if capture_id is not None:
                    captures.delete(str(capture_id))

            output_adapter = self.output_adapter_factory()
            surface_presenter = LabSurfacePresenter(
                output_adapter,
                experiment_name=experiment_name,
                capture_key=self.config.lab.capture_key,
            )
            terminal_state_factory = self.terminal_state_factory or RawTerminalState
            surface_factory = None if dedicated_host else self.terminal_surface_factory
            if surface_factory is None and not dedicated_host:

                def surface_factory(output: object) -> object:
                    return AlternateScreenSurface(output)

            def mark_running_and_ready() -> None:
                self.repository.mark_running(paths)
                if ready_callback is not None:
                    ready_callback(paths)

            proxy = TerminalProxy(
                self.backend_factory(),
                command=selected_command,
                input_adapter=adapter,
                output_adapter=output_adapter,
                terminal_state_factory=terminal_state_factory,
                dispatcher=dispatcher,
                emulator=emulator,
                capture_key=self.config.lab.capture_key,
                capture_handler=create_capture,
                cwd=self.cwd,
                size=selected_size,
                started_callback=mark_running_and_ready,
                capture_rollback=rollback_capture,
                host_boundary=surface_presenter.start,
                status_sink=surface_presenter,
                terminal_surface_factory=(
                    (lambda: surface_factory(output_adapter))
                    if surface_factory is not None
                    else None
                ),
            )
            exit_code = proxy.run()
            status_events = tuple(proxy.status_events)
            status = "completed"
        except KeyboardInterrupt as exc:
            status = "interrupted"
            status_reason = "user interrupted session"
            failure = exc
        except BaseException as exc:
            status = "failed"
            if isinstance(exc, TerminalCleanupError):
                exit_code = exc.exit_code
            status_reason = _lifecycle_failure_reason(exc)
            failure = exc
        finally:
            if recorder is not None:
                try:
                    recorder.close()
                except BaseException as exc:
                    if failure is None:
                        failure = exc
                        status = "failed"
                        status_reason = _lifecycle_failure_reason(exc)
            try:
                self.repository.finish(
                    paths,
                    status,
                    exit_code=exit_code,
                    reason=status_reason,
                )
            except BaseException as exc:
                persistence_reason = _error_reason(exc)
                status = "failed"
                status_reason = _append_reason(status_reason, persistence_reason)
                if failure is None:
                    failure = exc
                try:
                    self.repository.finish(paths, "failed", reason=status_reason)
                except BaseException as retry_error:
                    status_reason = _append_reason(status_reason, _error_reason(retry_error))
                    with suppress(BaseException):
                        self.repository.release_owner(paths)
        if failure is not None:
            raise failure
        return LabRunResult(paths, status, exit_code, advisory.message, status_events)

    def export(
        self,
        session: str,
        destination: Path | str | None = None,
        *,
        theme: str | None = None,
        force: bool = False,
    ) -> LabExportResult:
        paths = self.repository.resolve(session)
        output = (
            Path(destination)
            if destination is not None
            else self.cwd / f"{paths.root.name}-evidence"
        )
        selected_theme = RenderTheme.light() if theme == "light" else RenderTheme.dark()
        renderer = TerminalEvidenceRenderer(FontResolver(explicit=self.config.render.font))
        return LabExporter(renderer, theme=selected_theme).export(paths, output, force=force)


def _lifecycle_failure_reason(error: BaseException) -> str:
    if isinstance(error, KeyboardInterrupt):
        return "user interrupted session"
    if isinstance(error, TerminalCleanupError):
        return str(error)
    return f"{type(error).__name__}: session infrastructure failed"


def _error_reason(error: BaseException) -> str:
    message = str(error).strip()
    return message or type(error).__name__


def _append_reason(previous: str | None, addition: str) -> str:
    return addition if not previous else f"{previous}; {addition}"


def create_lab_service(
    cwd: Path | str | None = None,
    *,
    dedicated_host: bool = False,
) -> LabService:
    working_directory = Path.cwd() if cwd is None else Path(cwd)
    config = load_config(working_directory)
    windows_launcher = None
    if os.name == "nt" and not dedicated_host:
        windows_launcher = WindowsTerminalLabLauncher(
            launch_root=working_directory / ".csbox" / "launches"
        )
    return LabService(
        repository=SessionRepository.from_cwd(working_directory),
        config=config,
        cwd=working_directory,
        windows_launcher=windows_launcher,
    )
