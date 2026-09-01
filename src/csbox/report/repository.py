from __future__ import annotations

import json
from pathlib import Path

from pydantic import ValidationError

from csbox.core.safe_paths import atomic_write_text, ensure_private_directory, read_regular_text
from csbox.evidence.models import validate_identifier
from csbox.report.models import REPORT_PROFILE_VERSION, ReportProfile

_MAX_PROFILE_BYTES = 4 * 1024 * 1024
_DOCUMENT_KEYS = frozenset({"version", "profile"})


class ReportProfilePersistenceError(RuntimeError):
    """A report profile could not be safely read or written."""


class ReportProfileRepository:
    """Persist one independently readable profile per Evidence Set."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)

    @classmethod
    def from_cwd(cls, cwd: Path | str) -> ReportProfileRepository:
        return cls(Path(cwd) / ".csbox" / "report-profiles")

    def path_for(self, evidence_set_id: str) -> Path:
        identifier = validate_identifier(evidence_set_id, field_name="evidence_set_id")
        return self.root / f"{identifier}.json"

    def load(self, evidence_set_id: str) -> ReportProfile:
        path = self.path_for(evidence_set_id)
        try:
            raw = json.loads(read_regular_text(path, max_bytes=_MAX_PROFILE_BYTES))
            if (
                not isinstance(raw, dict)
                or set(raw) != _DOCUMENT_KEYS
                or type(raw["version"]) is not int
                or raw["version"] != REPORT_PROFILE_VERSION
                or not isinstance(raw["profile"], dict)
            ):
                raise ValueError("invalid report profile document")
            return ReportProfile.model_validate(raw["profile"])
        except FileNotFoundError:
            raise
        except (
            OSError,
            UnicodeError,
            json.JSONDecodeError,
            TypeError,
            ValueError,
            ValidationError,
            RecursionError,
        ) as exc:
            raise ReportProfilePersistenceError("报告结构配置不可读取。") from exc

    def load_or_default(
        self,
        evidence_set_id: str,
        *,
        default: ReportProfile | None = None,
    ) -> ReportProfile:
        try:
            return self.load(evidence_set_id)
        except FileNotFoundError:
            return default or ReportProfile.default()

    def save(self, evidence_set_id: str, profile: ReportProfile) -> ReportProfile:
        if not isinstance(profile, ReportProfile):
            raise TypeError("profile must be a ReportProfile")
        path = self.path_for(evidence_set_id)
        payload = {
            "version": REPORT_PROFILE_VERSION,
            "profile": profile.model_dump(mode="json"),
        }
        try:
            ensure_private_directory(self.root)
            atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
        except (OSError, UnicodeError, ValueError, TypeError, RecursionError) as exc:
            raise ReportProfilePersistenceError(
                "报告结构配置保存失败，请检查项目目录后重试。"
            ) from exc
        return profile


__all__ = ["ReportProfilePersistenceError", "ReportProfileRepository"]
