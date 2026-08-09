from __future__ import annotations

from pathlib import Path
from typing import ClassVar, Protocol

from csbox.core.models import (
    BuildResult,
    CheckContext,
    CheckResult,
    Evidence,
    ExportResult,
    ProjectInfo,
    RenderedEvidence,
)


class EvidenceProvider(Protocol):
    provider_id: ClassVar[str]

    def collect(self, source: Path) -> Evidence: ...


class EvidenceRenderer(Protocol):
    renderer_id: ClassVar[str]

    def render(self, evidence: Evidence) -> RenderedEvidence: ...


class CheckRule(Protocol):
    rule_id: ClassVar[str]

    def evaluate(self, context: CheckContext) -> CheckResult: ...


class ProjectDetector(Protocol):
    detector_id: ClassVar[str]

    def detect(self, root: Path) -> ProjectInfo | None: ...


class BuildAdapter(Protocol):
    adapter_id: ClassVar[str]

    def build(self, project: ProjectInfo, output_dir: Path) -> BuildResult: ...


class Exporter(Protocol):
    format_id: ClassVar[str]

    def export(self, result: object, destination: Path) -> ExportResult: ...
