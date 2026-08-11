from __future__ import annotations

import os
import platform
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from csbox.config.loader import load_config
from csbox.config.models import CSBoxConfig
from csbox.core.events import TerminalSize
from csbox.core.shell import detect_shell_version, select_shell
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
    TerminalProxy,
    current_terminal_size,
)
from csbox.lab.recorder import AsciicastV3Recorder
from csbox.lab.renderer import RenderTheme, TerminalEvidenceRenderer
from csbox.lab.repository import SessionRepository, SessionSummary, default_experiment_name
from csbox.lab.screen import TerminalEmulator


@dataclass(frozen=True, slots=True)
class LabRunResult:
    session: SessionPaths
    status: str
    exit_code: int | None
    advisory: str


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
        terminal_state_factory: Callable[[], object] = RawTerminalState,
    ) -> None:
        self.repository = repository
        self.config = config
        self.cwd = Path(cwd)
        self.backend_factory = backend_factory
        self.input_adapter_factory = input_adapter_factory
        self.output_adapter_factory = output_adapter_factory
        self.terminal_state_factory = terminal_state_factory

    def list(self) -> tuple[SessionSummary, ...]:
        return self.repository.list_sessions()

    def capture_advisory(self, adapter: object | None = None) -> CaptureBindingAdvisory:
        """Return the host-key warning before entering the raw terminal loop."""

        selected_adapter = adapter if adapter is not None else self.input_adapter_factory()
        return CaptureBindingProbe(
            self.config.lab.capture_key,
            input_supported=bool(getattr(selected_adapter, "supports_capture", True)),
        ).probe()

    def start(
        self,
        name: str | None = None,
        *,
        shell: str | None = None,
        command: Sequence[str] | None = None,
        size: TerminalSize | None = None,
    ) -> LabRunResult:
        selected_size = size or current_terminal_size()
        profile = select_shell(
            shell,
            self.config.lab.shell,
            system=platform.system(),
            environ=os.environ,
        )
        shell_version = detect_shell_version(profile)
        adapter = self.input_adapter_factory()
        advisory = self.capture_advisory(adapter)
        paths = self.repository.create_running(
            name or default_experiment_name(),
            shell=profile.kind.value,
            shell_version=shell_version,
            size=selected_size,
            cwd=self.cwd,
        )
        recorder: AsciicastV3Recorder | None = None
        exit_code: int | None = None
        status = "failed"
        failure: BaseException | None = None
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

            proxy = TerminalProxy(
                self.backend_factory(),
                command=tuple(command or profile.command),
                input_adapter=adapter,
                output_adapter=self.output_adapter_factory(),
                terminal_state_factory=self.terminal_state_factory,
                dispatcher=dispatcher,
                emulator=emulator,
                capture_key=self.config.lab.capture_key,
                capture_handler=create_capture,
                cwd=self.cwd,
                size=selected_size,
            )
            exit_code = proxy.run()
            status = "completed"
        except KeyboardInterrupt as exc:
            status = "interrupted"
            failure = exc
        except BaseException as exc:
            status = "failed"
            failure = exc
        finally:
            if recorder is not None:
                try:
                    recorder.close()
                except BaseException as exc:
                    if failure is None:
                        failure = exc
                        status = "failed"
            try:
                self.repository.finish(paths, status, exit_code=exit_code)
            except BaseException as exc:
                if failure is None:
                    failure = exc
        if failure is not None:
            raise failure
        return LabRunResult(paths, status, exit_code, advisory.message)

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


def create_lab_service(cwd: Path | str | None = None) -> LabService:
    working_directory = Path.cwd() if cwd is None else Path(cwd)
    config = load_config(working_directory)
    return LabService(
        repository=SessionRepository.from_cwd(working_directory),
        config=config,
        cwd=working_directory,
    )
