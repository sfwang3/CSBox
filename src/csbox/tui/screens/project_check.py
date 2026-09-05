from __future__ import annotations

from pathlib import Path

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.events import Resize
from textual.screen import Screen

from csbox.check.messages import finding_title, public_finding_message
from csbox.check.models import CheckFinding, CheckReport, CheckStatus, DetectedProject
from csbox.core.display_width import truncate_cells
from csbox.core.safe_paths import safe_relative_path
from csbox.core.text_layout import wrap_cells
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
        self.call_after_refresh(self._refresh)

    def on_resize(self, event: Resize) -> None:
        self._set_layout(event.size.width >= 120)
        self.call_after_refresh(self._refresh)

    def _set_layout(self, wide: bool) -> None:
        self.is_wide = wide
        self.set_class(wide, "wide")
        self.set_class(not wide, "narrow")

    def _refresh(self) -> None:
        self.query_one("#check-summary", CheckSummary).update(self._summary())
        report_findings = (*self.report.findings, *self._deep_findings())
        findings_panel = self.query_one("#check-findings", CheckFindings)
        narrow_panel = self.query_one("#check-narrow", CheckFindings)
        findings_panel.update(self._findings(report_findings, self._content_width(findings_panel)))
        projects = [f"项目（{len(self.report.projects)}）"]
        projects.extend(
            _project_label(self.report.root, project) for project in self.report.projects
        )
        if self.report.builds:
            projects.append("构建结果")
            projects.extend(
                f"{build.adapter_id}: {build.status.value}  {build.message}"
                for build in self.report.builds
            )
        projects_panel = self.query_one("#check-projects", CheckFindings)
        project_lines = [
            line
            for project in projects
            for line in wrap_cells(project, self._content_width(projects_panel))
        ]
        projects_panel.update("\n".join(project_lines))
        narrow_panel.update(self._findings(report_findings, self._content_width(narrow_panel)))
        self.query_one("#check-footer", CheckFooter).update(
            truncate_cells(
                "Q/Esc 返回  ? 帮助",
                self._content_width(self.query_one("#check-footer", CheckFooter)),
                ellipsis="…",
            )
        )

    def _deep_findings(self) -> tuple[CheckFinding, ...]:
        return () if self.report.deep_scan is None else (self.report.deep_scan,)

    def _summary(self) -> str:
        root = truncate_cells(
            ".",
            max(1, self.size.width - 18),
            ellipsis="…",
        )
        return (
            f"检查项目  //  {root}\n"
            f"状态：{self.report.status.value}    "
            f"提示：{len(self.report.findings) + len(self._deep_findings())}    "
            f"项目：{len(self.report.projects)}"
        )

    def _findings(self, findings: tuple[CheckFinding, ...], width: int) -> str:
        lines = ["检查结果"]
        if not any(finding.status in {CheckStatus.WARN, CheckStatus.FAIL} for finding in findings):
            lines.append("检查完成：未发现需要处理的问题。")
            if any(finding.status is CheckStatus.SKIP for finding in findings):
                lines.append("部分检查已跳过，结果不代表全部项目均已检查。")
            return "\n".join(lines)

        for finding in findings:
            location = ""
            if finding.path is not None:
                location = truncate_cells(
                    _relative_location(self.report.root, finding.path),
                    max(8, min(36, width // 2)),
                    ellipsis="…",
                )
            if finding.line is not None:
                location += f":{finding.line}"
            category = f" [{finding.category}]" if finding.category else ""
            if finding.status in {CheckStatus.WARN, CheckStatus.FAIL}:
                header = (
                    f"{_status_symbol(finding.status)} {finding_title(finding)} "
                    f"[{finding.rule_id}] {location}"
                ).rstrip()
                lines.extend(wrap_cells(header, width))
                detail_width = max(2, width - 2)
                for message_line in public_finding_message(finding).splitlines()[1:]:
                    lines.extend(wrap_cells(f"  {message_line}", detail_width))
                continue
            lines.extend(
                wrap_cells(
                    f"{_status_symbol(finding.status)} {finding.status.value} "
                    f"{finding.rule_id}{category} {location} {public_finding_message(finding)}",
                    width,
                )
            )
        return "\n".join(lines)

    def _content_width(self, widget: CheckFindings | CheckFooter) -> int:
        return max(2, widget.content_region.width or self.size.width - 6)

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


def _relative_location(root: Path, value: Path | str) -> str:
    path = Path(value)
    resolved_root = Path(root).resolve(strict=False)
    if path.is_absolute():
        try:
            relative = path.resolve(strict=False).relative_to(resolved_root)
        except ValueError:
            return "<outside-project>"
    else:
        try:
            relative = Path(safe_relative_path(path.as_posix()))
        except ValueError:
            return "<outside-project>"
    return relative.as_posix() or "."


def _project_label(root: Path, project: DetectedProject) -> str:
    location = truncate_cells(_relative_location(root, project.root), 48, ellipsis="…")
    return f"{project.kind}: {location}"


__all__ = ["CheckScreen", "ProjectCheckScreen"]
