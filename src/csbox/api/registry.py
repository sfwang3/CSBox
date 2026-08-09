from csbox.core.protocols import EvidenceProvider, EvidenceRenderer
from csbox.core.registry import Registry

EVIDENCE_PROVIDERS = Registry[EvidenceProvider]("evidence-provider")
EVIDENCE_RENDERERS = Registry[EvidenceRenderer]("evidence-renderer")
