from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from textual.app import App
from textual.timer import Timer

from csbox.api.cli import create_api_runner_factory
from csbox.api.repository import ApiRunRepository
from csbox.api.scenario import ScenarioLoader
from csbox.check.models import CheckReport
from csbox.core.models import EnvironmentSnapshot
from csbox.evidence.repository import EvidenceSetRepository
from csbox.lab.home_data import RealHomeDataSource
from csbox.lab.models import SessionMetadata, SessionPaths
from csbox.lab.ports import HomeDataSource
from csbox.lab.repository import (
    SessionRepository,
    SessionRepositoryError,
    load_session_metadata,
)
from csbox.locales import Translator
from csbox.pack.service import PackService, create_pack_service
from csbox.tui.help import HELP_BINDINGS, open_help
from csbox.tui.lab_workflow import (
    ActiveLabSession,
    HomeNotice,
    LabStartRequest,
    ShellOption,
    lab_status_notice,
    metadata_retry_notice,
)
from csbox.tui.screens.api import ApiScreen
from csbox.tui.screens.evidence import (
    EvidenceSetEditorScreen,
    EvidenceSetsScreen,
    ReportExportAction,
)
from csbox.tui.screens.home import HomeScreen
from csbox.tui.screens.pack import PackConfirmationScreen
from csbox.tui.screens.project_check import ProjectCheckScreen
from csbox.tui.screens.records import ExportAction, RecordsScreen
from csbox.tui.screens.review import ReviewController, ReviewScreen


class CSBoxApp(App[LabStartRequest | None]):
    TITLE = "CSBox"
    CSS_PATH = Path(__file__).parent / "themes" / "csbox.tcss"
    BINDINGS = [
        *HELP_BINDINGS,
        ("q", "quit_app", ""),
    ]

    def __init__(
        self,
        *,
        data_source: HomeDataSource,
        environment: EnvironmentSnapshot,
        locale: Translator,
        api_repository: ApiRunRepository | None = None,
        api_runner_factory: Callable[..., object] | None = None,
        pack_plan_factory: Callable[..., object] | None = None,
        pack_action: Callable[[object], object] | None = None,
        shell_options: tuple[ShellOption, ...] = (),
        shell_error: str | None = None,
        home_notice: HomeNotice | None = None,
        active_session: ActiveLabSession | None = None,
        metadata_loader: Callable[[SessionPaths], SessionMetadata] = load_session_metadata,
        monitor_interval: float = 0.8,
        session_repository: SessionRepository | None = None,
        evidence_repository: EvidenceSetRepository | None = None,
        export_action: ExportAction | None = None,
        report_export_action: ReportExportAction | None = None,
    ) -> None:
        super().__init__()
        self.data_source = data_source
        self.environment = environment
        self.locale = locale
        self.snapshot = data_source.get_home_snapshot(environment)
        project_dir = self.snapshot.project_dir or Path.cwd()
        self.project_dir = project_dir
        if self.snapshot.project_dir is None:
            self.snapshot = self.snapshot.model_copy(update={"project_dir": project_dir})
        self.shell_options = shell_options
        self.shell_error = shell_error
        self.home_notice = home_notice
        self.active_session = active_session
        self.metadata_loader = metadata_loader
        self.monitor_interval = monitor_interval
        self.export_action = export_action
        self.report_export_action = report_export_action
        source_repository = getattr(data_source, "repository", None)
        self.session_repository = session_repository or (
            source_repository
            if isinstance(source_repository, SessionRepository)
            else SessionRepository.from_cwd(project_dir)
        )
        self.evidence_repository = evidence_repository or EvidenceSetRepository.from_cwd(
            project_dir
        )
        self._active_timer: Timer | None = None
        self.api_repository = api_repository or ApiRunRepository.from_cwd(project_dir)
        self.api_runner_factory = api_runner_factory or create_api_runner_factory(project_dir)
        self.pack_service: PackService = create_pack_service(project_dir)
        self.pack_plan_factory = pack_plan_factory or (
            lambda destination=None: self.pack_service.plan(
                project_dir,
                destination=destination,
            )
        )
        self.pack_action = pack_action or (
            lambda plan: self.pack_service.pack_plan(plan, verify=True)
        )

    def on_mount(self) -> None:
        self.home_screen = HomeScreen(
            snapshot=self.snapshot,
            locale=self.locale,
            api_screen_factory=self._api_screen,
            pack_plan_factory=self.pack_plan_factory,
            pack_action=self.pack_action,
            shell_options=self.shell_options,
            shell_error=self.shell_error,
            notice=self.home_notice,
            records_screen_factory=self._records_screen,
            evidence_screen_factory=self._evidence_screen,
            check_service=self.pack_service.check_service,
        )
        self.push_screen(self.home_screen)
        if self.active_session is not None:
            self._active_timer = self.set_interval(
                self.monitor_interval,
                self._poll_active_session,
            )

    def _api_screen(self) -> ApiScreen:
        return ApiScreen(
            repository=self.api_repository,
            scenario_loader=ScenarioLoader(),
            runner_factory=self.api_runner_factory,
            locale=self.locale,
        )

    def _records_screen(self) -> RecordsScreen:
        return RecordsScreen(
            repository=self.session_repository,
            locale=self.locale,
            project_dir=self.project_dir,
            shell_options=self.shell_options,
            shell_error=self.shell_error,
            export_action=self.export_action,
        )

    def _evidence_screen(self) -> EvidenceSetsScreen:
        return EvidenceSetsScreen(
            repository=self.evidence_repository,
            session_repository=self.session_repository,
            locale=self.locale,
            project_dir=self.project_dir,
            report_export_action=self.report_export_action,
        )

    def _critical_workflow_active(self) -> bool:
        screen = self.screen
        if isinstance(screen, HomeScreen):
            return screen.is_working
        if isinstance(screen, PackConfirmationScreen):
            return screen.is_working
        if isinstance(screen, RecordsScreen):
            return screen.is_working
        if isinstance(screen, EvidenceSetEditorScreen):
            return screen.is_working
        if isinstance(screen, ApiScreen):
            return screen.is_working
        return False

    def action_show_help(self) -> None:
        if self._critical_workflow_active():
            return
        open_help(self, self.locale)

    def refresh_home(self) -> None:
        self.snapshot = self.data_source.get_home_snapshot(self.environment)
        if self.snapshot.project_dir is None:
            self.snapshot = self.snapshot.model_copy(update={"project_dir": self.project_dir})
        self.home_screen.update_snapshot(self.snapshot)

    def _poll_active_session(self) -> None:
        active_session = self.active_session
        if active_session is None:
            return
        try:
            metadata = self.metadata_loader(active_session.paths)
        except (OSError, UnicodeError, ValueError, SessionRepositoryError):
            self._set_home_notice(metadata_retry_notice(active_session.experiment_name))
            return
        if metadata.status in {"starting", "running"}:
            self._set_home_notice(lab_status_notice(active_session.experiment_name, "running"))
            return
        if metadata.status not in {"completed", "interrupted", "failed"}:
            return
        if self._active_timer is not None:
            self._active_timer.stop()
            self._active_timer = None
        self.active_session = None
        self._set_home_notice(lab_status_notice(active_session.experiment_name, metadata.status))
        self.refresh_home()

    def _set_home_notice(self, notice: HomeNotice) -> None:
        self.home_notice = notice
        self.home_screen.update_notice(notice)

    async def action_quit(self) -> None:
        if self._critical_workflow_active():
            return
        self.exit(None)

    def action_quit_app(self) -> None:
        if self._critical_workflow_active():
            return
        self.exit(None)


class ReviewApp(App[None]):
    """Minimal app shell for the service-owned Review screen."""

    TITLE = "CSBox Review"
    CSS_PATH = Path(__file__).parent / "themes" / "csbox.tcss"
    BINDINGS = [*HELP_BINDINGS]

    def __init__(self, *, controller: ReviewController, locale: Translator) -> None:
        super().__init__()
        self.controller = controller
        self.locale = locale
        self.owns_review_screen = True

    def on_mount(self) -> None:
        self.push_screen(ReviewScreen(controller=self.controller, locale=self.locale))

    def action_show_help(self) -> None:
        open_help(self, self.locale)

    def action_quit(self) -> None:
        self.exit()


class ApiApp(App[None]):
    """Standalone app shell for the API scenario/evidence workflow."""

    TITLE = "CSBox API"
    CSS_PATH = Path(__file__).parent / "themes" / "csbox.tcss"
    BINDINGS = [*HELP_BINDINGS]

    def __init__(
        self,
        repository: ApiRunRepository,
        scenario_loader: ScenarioLoader,
        runner_factory: Callable[..., object],
        locale: Translator,
        *,
        exporter_factory: Callable[[], object] | None = None,
    ) -> None:
        super().__init__()
        self.repository = repository
        self.scenario_loader = scenario_loader
        self.runner_factory = runner_factory
        self.locale = locale
        self.exporter_factory = exporter_factory
        self.owns_api_screen = True

    def on_mount(self) -> None:
        self.push_screen(
            ApiScreen(
                repository=self.repository,
                scenario_loader=self.scenario_loader,
                runner_factory=self.runner_factory,
                locale=self.locale,
                exporter_factory=self.exporter_factory,
            )
        )

    def action_show_help(self) -> None:
        screen = self.screen
        if isinstance(screen, ApiScreen) and screen.is_working:
            return
        open_help(self, self.locale)

    def action_quit(self) -> None:
        screen = self.screen
        if isinstance(screen, ApiScreen) and screen.is_working:
            return
        self.exit()


class CheckApp(App[None]):
    """Minimal app shell for a service-produced project check report."""

    TITLE = "CSBox Check"
    CSS_PATH = Path(__file__).parent / "themes" / "csbox.tcss"
    BINDINGS = [*HELP_BINDINGS]

    def __init__(self, *, report: CheckReport, locale: Translator) -> None:
        super().__init__()
        self.report = report
        self.locale = locale
        self.owns_check_screen = True

    def on_mount(self) -> None:
        self.push_screen(ProjectCheckScreen(report=self.report, locale=self.locale))

    def action_show_help(self) -> None:
        open_help(self, self.locale)

    def action_quit(self) -> None:
        self.exit()


def real_home_data_source(project_dir: Path) -> HomeDataSource:
    """Construct the runtime Home source without retaining demo state."""

    return RealHomeDataSource(SessionRepository.from_cwd(project_dir), project_dir)
