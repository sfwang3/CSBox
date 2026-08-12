from __future__ import annotations

import platform
import tempfile
from pathlib import Path

from csbox.check.build import DEFAULT_BUILD_ADAPTERS, BuildAdapter, CommandRunner
from csbox.check.detectors import DEFAULT_DETECTORS, FileInventory, ProjectDetector, detect_projects
from csbox.check.models import BuildOutcome, CheckContext, CheckFinding, CheckReport
from csbox.check.rules import DEFAULT_RULES, CheckRule, run_deep_secret_scan
from csbox.config.loader import load_config
from csbox.config.models import CSBoxConfig


class CheckServiceError(RuntimeError):
    """The project could not be inspected safely."""


class CheckService:
    def __init__(
        self,
        *,
        config: CSBoxConfig | None = None,
        command_runner: CommandRunner | None = None,
        rules: tuple[CheckRule, ...] = DEFAULT_RULES,
        detectors: tuple[ProjectDetector, ...] = DEFAULT_DETECTORS,
        adapters: dict[str, type[BuildAdapter]] = DEFAULT_BUILD_ADAPTERS,
        platform_name: str | None = None,
    ) -> None:
        self.config = config or CSBoxConfig()
        self.command_runner = command_runner
        self.rules = rules
        self.detectors = detectors
        self.adapters = adapters
        self.platform_name = platform_name or platform.system()

    def run(
        self,
        root: Path | str,
        *,
        build: bool = False,
        deep: bool = False,
        inventory: FileInventory | None = None,
    ) -> CheckReport:
        try:
            project_root = Path(root).resolve()
            current_inventory = inventory or FileInventory.build(
                project_root,
                scan_limit_bytes=256 * 1024,
            )
            if current_inventory.root != project_root:
                raise ValueError("inventory root does not match project root")
        except (OSError, RuntimeError, ValueError):
            raise CheckServiceError("无法安全检查项目目录。") from None
        context = CheckContext(
            root=project_root,
            inventory=current_inventory,
            large_file_threshold_bytes=self.config.check.large_file_threshold_mb * 1024 * 1024,
        )
        findings: list[CheckFinding] = []
        for rule in self.rules:
            findings.extend(rule.evaluate(context))
        projects = detect_projects(project_root, self.detectors, inventory=current_inventory)
        builds: list[BuildOutcome] = []
        if build and projects:
            builds.extend(self._build_projects(projects, current_inventory))
        deep_scan = run_deep_secret_scan(project_root) if deep else None
        return CheckReport(
            root=project_root,
            projects=projects,
            findings=tuple(findings),
            builds=tuple(builds),
            deep_scan=deep_scan,
            text_scan_stats=current_inventory.text_scan_stats.as_dict(),
        )

    def _build_projects(self, projects, inventory: FileInventory) -> list[BuildOutcome]:
        outcomes: list[BuildOutcome] = []
        with tempfile.TemporaryDirectory(prefix="csbox-check-build-") as temporary:
            output_dir = Path(temporary)
            for project in projects:
                adapter_type = self.adapters.get(project.kind)
                if adapter_type is None:
                    continue
                adapter = adapter_type(
                    runner=self.command_runner,
                    platform_name=self.platform_name,
                )
                outcomes.append(adapter.build(project, output_dir, inventory=inventory))
        return outcomes


def create_check_service(cwd: Path | str | None = None) -> CheckService:
    project_dir = Path.cwd() if cwd is None else Path(cwd)
    return CheckService(config=load_config(project_dir))


__all__ = ["CheckService", "CheckServiceError", "create_check_service"]
