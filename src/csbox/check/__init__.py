"""Project check and build boundaries."""

from csbox.check.detectors import FileEntry, FileInventory, TextScanStats
from csbox.check.models import (
    BuildOutcome,
    CheckFinding,
    CheckReport,
    CheckStatus,
    DetectedProject,
)
from csbox.check.registry import CHECK_RULES, PROJECT_DETECTORS
from csbox.check.service import CheckService, CheckServiceError, create_check_service

__all__ = [
    "CHECK_RULES",
    "PROJECT_DETECTORS",
    "BuildOutcome",
    "CheckFinding",
    "CheckReport",
    "CheckService",
    "CheckServiceError",
    "CheckStatus",
    "DetectedProject",
    "FileEntry",
    "FileInventory",
    "TextScanStats",
    "create_check_service",
]
