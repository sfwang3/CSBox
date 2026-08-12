from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from textual.app import App

from csbox.api.cli import create_api_runner_factory
from csbox.api.repository import ApiRunRepository
from csbox.api.scenario import ScenarioLoader
from csbox.check.models import CheckReport
from csbox.core.models import EnvironmentSnapshot
from csbox.lab.home_data import RealHomeDataSource
from csbox.lab.ports import HomeDataSource
from csbox.lab.repository import SessionRepository
from csbox.locales import Translator
from csbox.pack.service import PackService, create_pack_service
from csbox.tui.dialogs.unavailable import UnavailableDialog
from csbox.tui.screens.api import ApiScreen
from csbox.tui.screens.home import HomeScreen
from csbox.tui.screens.project_check import ProjectCheckScreen
from csbox.tui.screens.review import ReviewController, ReviewScreen


class CSBoxApp(App[None]):
    TITLE = "CSBox"
    CSS_PATH = Path(__file__).parent / "themes" / "csbox.tcss"
    BINDINGS = [
        ("f1", "show_help", ""),
        ("f5", "refresh_home", ""),
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
        pack_plan_factory: Callable[[], object] | None = None,
        pack_action: Callable[[object], object] | None = None,
    ) -> None:
        super().__init__()
        self.data_source = data_source
        self.environment = environment
        self.locale = locale
        self.snapshot = data_source.get_home_snapshot(environment)
        project_dir = self.snapshot.project_dir or Path.cwd()
        self.api_repository = api_repository or ApiRunRepository.from_cwd(project_dir)
        self.api_runner_factory = api_runner_factory or create_api_runner_factory(project_dir)
        self.pack_service: PackService = create_pack_service(project_dir)
        self.pack_plan_factory = pack_plan_factory or (lambda: self.pack_service.plan(project_dir))
        self.pack_action = pack_action or (
            lambda plan: self.pack_service.pack_plan(plan, verify=True)
        )

    def on_mount(self) -> None:
        self.push_screen(
            HomeScreen(
                snapshot=self.snapshot,
                locale=self.locale,
                api_screen_factory=self._api_screen,
                pack_plan_factory=self.pack_plan_factory,
                pack_action=self.pack_action,
            )
        )

    def _api_screen(self) -> ApiScreen:
        return ApiScreen(
            repository=self.api_repository,
            scenario_loader=ScenarioLoader(),
            runner_factory=self.api_runner_factory,
            locale=self.locale,
        )

    def action_show_help(self) -> None:
        self.push_screen(
            UnavailableDialog(
                title=self.locale("home.help.title"),
                locale=self.locale,
                message_key="home.help.body",
                close_key="home.help.close",
            )
        )

    def action_refresh_home(self) -> None:
        self.snapshot = self.data_source.get_home_snapshot(self.environment)
        if isinstance(self.screen, HomeScreen):
            self.screen.update_snapshot(self.snapshot)

    def action_quit_app(self) -> None:
        self.exit()


class ReviewApp(App[None]):
    """Minimal app shell for the service-owned Review screen."""

    TITLE = "CSBox Review"
    CSS_PATH = Path(__file__).parent / "themes" / "csbox.tcss"

    def __init__(self, *, controller: ReviewController, locale: Translator) -> None:
        super().__init__()
        self.controller = controller
        self.locale = locale
        self.owns_review_screen = True

    def on_mount(self) -> None:
        self.push_screen(ReviewScreen(controller=self.controller, locale=self.locale))

    def action_quit(self) -> None:
        self.exit()


class ApiApp(App[None]):
    """Standalone app shell for the API scenario/evidence workflow."""

    TITLE = "CSBox API"
    CSS_PATH = Path(__file__).parent / "themes" / "csbox.tcss"

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

    def action_quit(self) -> None:
        self.exit()


class CheckApp(App[None]):
    """Minimal app shell for a service-produced project check report."""

    TITLE = "CSBox Check"
    CSS_PATH = Path(__file__).parent / "themes" / "csbox.tcss"

    def __init__(self, *, report: CheckReport, locale: Translator) -> None:
        super().__init__()
        self.report = report
        self.locale = locale
        self.owns_check_screen = True

    def on_mount(self) -> None:
        self.push_screen(ProjectCheckScreen(report=self.report, locale=self.locale))

    def action_quit(self) -> None:
        self.exit()


def real_home_data_source(project_dir: Path) -> HomeDataSource:
    """Construct the runtime Home source without retaining demo state."""

    return RealHomeDataSource(SessionRepository.from_cwd(project_dir), project_dir)
