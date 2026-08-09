from pathlib import Path

import pytest
from pydantic import ValidationError

from csbox.api.registry import EVIDENCE_PROVIDERS, EVIDENCE_RENDERERS
from csbox.check.registry import CHECK_RULES, PROJECT_DETECTORS
from csbox.core.models import (
    BuildResult,
    CheckContext,
    CheckResult,
    EnvironmentSnapshot,
    Evidence,
    ExportResult,
    HomeSnapshot,
    ProjectInfo,
    RecentExperiment,
    RenderedEvidence,
)
from csbox.core.protocols import (
    BuildAdapter,
    CheckRule,
    EvidenceProvider,
    EvidenceRenderer,
    Exporter,
    ProjectDetector,
)
from csbox.core.registry import Registry
from csbox.pack.registry import BUILD_ADAPTERS, EXPORTERS


def test_named_protocols_are_importable() -> None:
    protocols = (
        EvidenceProvider,
        EvidenceRenderer,
        CheckRule,
        ProjectDetector,
        BuildAdapter,
        Exporter,
    )

    assert all(getattr(protocol, "_is_protocol", False) for protocol in protocols)


def test_domain_models_validate_a_home_snapshot() -> None:
    environment = EnvironmentSnapshot(
        os_name="Linux",
        os_version="test",
        python_version="3.12.3",
        shell="Bash",
        shell_executable="bash",
        powershell_51_available=False,
        powershell_7_available=False,
        is_wsl=True,
        terminal_columns=80,
        terminal_rows=24,
    )
    experiment = RecentExperiment(
        name="拓扑连通性演示",
        status="complete",
        duration="12 min",
        demo=True,
    )

    snapshot = HomeSnapshot(environment=environment, recent_experiments=[experiment])

    assert snapshot.environment.is_wsl is True
    assert snapshot.recent_experiments[0].demo is True


def test_domain_models_reject_non_positive_terminal_dimensions() -> None:
    with pytest.raises(ValidationError):
        EnvironmentSnapshot(
            os_name="Linux",
            os_version="test",
            python_version="3.12.3",
            shell="Bash",
            shell_executable="bash",
            powershell_51_available=False,
            powershell_7_available=False,
            is_wsl=False,
            terminal_columns=0,
            terminal_rows=24,
        )


def test_business_registries_are_empty_and_typed() -> None:
    registries = (
        EVIDENCE_PROVIDERS,
        EVIDENCE_RENDERERS,
        CHECK_RULES,
        PROJECT_DETECTORS,
        BUILD_ADAPTERS,
        EXPORTERS,
    )

    assert all(isinstance(registry, Registry) for registry in registries)
    assert all(registry.items() == () for registry in registries)


def test_future_domain_models_have_small_stable_shapes() -> None:
    assert Evidence(kind="text", content="demo").kind == "text"
    assert RenderedEvidence(format_id="text", content="demo").format_id == "text"
    assert CheckContext(project_dir=Path(".")).project_dir == Path(".")
    assert CheckResult(rule_id="demo", passed=True, summary="ok").passed is True
    assert ProjectInfo(kind="unknown", root=Path(".")).kind == "unknown"
    assert BuildResult(adapter_id="demo", success=False, artifacts=()).success is False
    assert ExportResult(format_id="text", destination=Path("out.txt")).format_id == "text"
