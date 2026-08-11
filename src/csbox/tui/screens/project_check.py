from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.events import Resize
from textual.screen import Screen

from csbox.check.models import CheckFinding, CheckReport, CheckStatus
from csbox.core.display_width import truncate_cells
from csbox.locales import Translator
from csbox.tui.widgets.project_check import CheckFindings, CheckFooter, CheckSummary


class ProjectCheckScreen(Screen[None]):
    BINDINGS = [("escape", "go_back", "返回"), ("q", "go_back", "返回")]

    def __init__(self, *, report: CheckReport, locale: Translator) -> None:
        super().__init__(name="project-check")
        self.report = report
        self.locale = locale
        self.is_wide = False

    def compose(self) -> ComposeResult:
        yield Vertical(
            CheckSummary(id="check-summary"),
            Horizontal(
                CheckFindings(id="check-findings"),
                CheckFindings(id="check-projects"),
                id="check-body",
            ),
            CheckFindings(id="check-narrow"),
            CheckFooter(id="check-footer", markup=False),
            id="check-layout",
        )

    def on_mount(self) -> None:
        self._set_layout(self.size.width >= 120)
        self._refresh()

    def on_resize(self, event: Resize) -> None:
        self._set_layout(event.size.width >= 120)
        self._refresh()

    def _set_layout(self, wide: bool) -> None:
        self.is_wide = wide
        self.set_class(wide, "wide")
        self.set_class(not wide, "narrow")

    def _refresh(self) -> None:
        self.query_one("#check-summary", CheckSummary).update(self._summary())
        findings = self._findings(self.report.findings)
        self.query_one("#check-findings", CheckFindings).update(findings)
        projects = [f"PROJECTS ({len(self.report.projects)})"]
        projects.extend(
            f"{project.kind}: {truncate_cells(str(project.root), 48, ellipsis='…')}"
            for project in self.report.projects
        )
        if self.report.builds:
            projects.append("BUILDS")
            projects.extend(
                f"{build.adapter_id}: {build.status.value}  {build.message}"
                for build in self.report.builds
            )
        self.query_one("#check-projects", CheckFindings).update("\n".join(projects))
        self.query_one("#check-narrow", CheckFindings).update(findings)
        self.query_one("#check-footer", CheckFooter).update(
            truncate_cells("Q/Esc 返回", max(1, self.size.width - 2), ellipsis="…")
        )

    def _summary(self) -> str:
        root = truncate_cells(
            str(self.report.root),
            max(1, self.size.width - 18),
            ellipsis="…",
        )
        return (
            f"CHECK  //  {root}\n"
            f"STATUS: {self.report.status.value}    "
            f"FINDINGS: {len(self.report.findings)}    PROJECTS: {len(self.report.projects)}"
        )

    def _findings(self, findings: tuple[CheckFinding, ...]) -> str:
        lines = ["FINDINGS"]
        for finding in findings:
            location = ""
            if finding.path is not None:
                location = truncate_cells(str(finding.path), 36, ellipsis="…")
            if finding.line is not None:
                location += f":{finding.line}"
            category = f" [{finding.category}]" if finding.category else ""
            if (
                finding.category in {"env", "private-key", "hard-coded-secret"}
                and finding.status is CheckStatus.FAIL
            ):
                lines.append(f"{_status_symbol(finding.status)} {location} {finding.category}")
                continue
            lines.append(
                f"{_status_symbol(finding.status)} {finding.status.value} "
                f"{finding.rule_id}{category} {location} {finding.message}"
            )
        return "\n".join(lines)

    def action_go_back(self) -> None:
        if getattr(self.app, "owns_check_screen", False):
            self.app.exit()
        else:
            self.app.pop_screen()


CheckScreen = ProjectCheckScreen


def _status_symbol(status: CheckStatus) -> str:
    return {
        CheckStatus.PASS: "[OK]",
        CheckStatus.WARN: "[WARN]",
        CheckStatus.FAIL: "[FAIL]",
        CheckStatus.SKIP: "[SKIP]",
    }[status]


__all__ = ["CheckScreen", "ProjectCheckScreen"]
