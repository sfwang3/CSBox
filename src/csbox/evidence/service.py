"""Configured service construction for Evidence Set report handoff."""

from __future__ import annotations

from pathlib import Path

from csbox.api.renderer import ApiEvidenceRenderer
from csbox.api.repository import ApiRunRepository
from csbox.config.loader import load_config
from csbox.evidence.exporter import EvidenceReportExporter, PhaseCallback, ReportExportResult
from csbox.evidence.models import EvidenceSet
from csbox.evidence.resolver import ApiStepResolver, EvidenceSourceResolver, LabCaptureResolver
from csbox.lab.fonts import FontResolver
from csbox.lab.renderer import TerminalEvidenceRenderer
from csbox.lab.repository import SessionRepository
from csbox.report.models import ReportProfile


class ReportHandoffService:
    """Build the configured renderer and delegate to the testable exporter."""

    def __init__(self, exporter: EvidenceReportExporter) -> None:
        self.exporter = exporter

    def export(
        self,
        evidence_set: EvidenceSet,
        destination: Path | str,
        *,
        force: bool = False,
        phase_callback: PhaseCallback | None = None,
        report_profile: ReportProfile | None = None,
    ) -> ReportExportResult:
        return self.exporter.export(
            evidence_set,
            destination,
            force=force,
            phase_callback=phase_callback,
            report_profile=report_profile,
        )


def create_report_handoff_service(
    cwd: Path | str,
    *,
    repository: SessionRepository | None = None,
    renderer: TerminalEvidenceRenderer | None = None,
    api_repository: ApiRunRepository | None = None,
    api_renderer: ApiEvidenceRenderer | None = None,
) -> ReportHandoffService:
    working_directory = Path(cwd)
    config = load_config(working_directory)
    session_repository = repository or SessionRepository.from_cwd(working_directory)
    api_run_repository = api_repository or ApiRunRepository.from_cwd(working_directory)
    selected_renderer = renderer or TerminalEvidenceRenderer(
        FontResolver(explicit=config.render.font)
    )
    selected_api_renderer = api_renderer or ApiEvidenceRenderer(
        FontResolver(explicit=config.render.font)
    )
    return ReportHandoffService(
        EvidenceReportExporter(
            EvidenceSourceResolver(
                LabCaptureResolver(session_repository),
                ApiStepResolver(api_run_repository),
            ),
            selected_renderer,
            api_renderer=selected_api_renderer,
            api_theme=config.render.theme,
        )
    )


__all__ = ["ReportHandoffService", "create_report_handoff_service"]
