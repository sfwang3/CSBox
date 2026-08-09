from csbox.core.protocols import CheckRule, ProjectDetector
from csbox.core.registry import Registry

CHECK_RULES = Registry[CheckRule]("check-rule")
PROJECT_DETECTORS = Registry[ProjectDetector]("project-detector")
