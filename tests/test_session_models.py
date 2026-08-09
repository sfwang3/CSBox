from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from csbox.lab.models import SessionMetadata, SessionPaths


def test_session_metadata_defaults_to_running_and_uses_stable_json_aliases() -> None:
    metadata = SessionMetadata(
        id="session-001",
        name="网络实验",
        startedAt=datetime(2026, 8, 10, 6, 0, tzinfo=UTC),
        platform="linux",
        shell="bash",
        shellVersion="5.2",
        initialRows=24,
        initialColumns=80,
        cwd=Path("/tmp/中文项目"),
        csboxVersion="0.1.0",
    )

    assert metadata.status == "running"
    assert metadata.ended_at is None
    assert metadata.model_dump(mode="json", by_alias=True) == {
        "id": "session-001",
        "name": "网络实验",
        "status": "running",
        "startedAt": "2026-08-10T06:00:00Z",
        "endedAt": None,
        "platform": "linux",
        "shell": "bash",
        "shellVersion": "5.2",
        "initialRows": 24,
        "initialColumns": 80,
        "cwd": "/tmp/中文项目",
        "csboxVersion": "0.1.0",
    }


def test_session_paths_have_stable_sidecar_names(tmp_path: Path) -> None:
    paths = SessionPaths(tmp_path / "session-001")

    assert paths.cast == tmp_path / "session-001" / "session.cast"
    assert paths.metadata == tmp_path / "session-001" / "metadata.json"
    assert paths.captures == tmp_path / "session-001" / "captures.json"
    assert paths.checkpoints == tmp_path / "session-001" / "checkpoints.json"


def test_session_metadata_rejects_a_naive_non_utc_start_time() -> None:
    with pytest.raises(ValidationError, match="timezone-aware"):
        SessionMetadata(
            id="session-001",
            name="网络实验",
            startedAt=datetime(2026, 8, 10, 6, 0),
            platform="linux",
            shell="bash",
            shellVersion=None,
            initialRows=24,
            initialColumns=80,
            cwd=Path("/tmp/project"),
            csboxVersion="0.1.0",
        )
