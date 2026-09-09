from __future__ import annotations

import json
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from xml.etree import ElementTree

import pytest
from PIL import Image

from csbox.api.models import ApiRequest, ApiResponse, ApiRun, ApiRunResult, ApiScenario, ApiStep
from csbox.api.repository import ApiRunRepository
from csbox.core.events import TerminalSize
from csbox.evidence.exporter import EvidenceReportExporter, ReportExportError
from csbox.evidence.models import ApiStepSource, EvidenceItem, EvidenceSet, LabCaptureSource
from csbox.evidence.repository import EvidenceSetRepository
from csbox.evidence.resolver import (
    ApiStepResolver,
    EvidenceSourceResolver,
    LabCaptureResolver,
)
from csbox.lab.captures import CaptureStore
from csbox.lab.repository import SessionRepository
from csbox.lab.screen import TerminalCell, TerminalCursor, TerminalSnapshot


def _snapshot(character: str) -> TerminalSnapshot:
    return TerminalSnapshot(
        rows=1,
        columns=4,
        cells=((TerminalCell(character), TerminalCell(" "), TerminalCell(" "), TerminalCell(" ")),),
        cursor=TerminalCursor(row=0, column=0),
    )


def _lab_fixture(tmp_path: Path) -> tuple[SessionRepository, str, str, str]:
    repository = SessionRepository(tmp_path / ".csbox" / "sessions")
    paths = repository.create_starting(
        "终端实验",
        shell="bash",
        shell_version=None,
        size=TerminalSize(columns=80, rows=24),
        cwd=tmp_path,
        session_id="session-1",
        started_at=datetime(2026, 9, 1, tzinfo=UTC),
    )
    paths.cast.write_bytes(b"canonical cast")
    captures = CaptureStore(
        paths.captures,
        id_factory=iter(("capture-1", "capture-2")).__next__,
        clock=lambda: datetime(2026, 9, 1, tzinfo=UTC),
    )
    first = captures.create_capture(_snapshot("A"), cwd=tmp_path, title="终端开始")
    second = captures.create_capture(_snapshot("B"), cwd=tmp_path, title="终端结束")
    repository.finish(
        paths,
        "completed",
        ended_at=datetime(2026, 9, 1, 0, 1, tzinfo=UTC),
    )
    return repository, paths.root.name, first.capture_id, second.capture_id


def _api_fixture(tmp_path: Path) -> ApiRunRepository:
    repository = ApiRunRepository(tmp_path / ".csbox" / "api" / "runs")
    secret = "API_REPORT_SECRET_SENTINEL"
    steps = (
        ApiStep(
            name="登录结果",
            request=ApiRequest(
                method="GET",
                url=f"https://example.test/login?token={secret}",
                headers={"Authorization": f"Bearer {secret}"},
            ),
        ),
    )
    run = ApiRun(
        id="run-1",
        scenario=ApiScenario(
            name="中文接口场景",
            variables={"token": secret},
            steps=steps,
        ),
        started_at=datetime(2026, 9, 1, tzinfo=UTC),
        results=(
            ApiRunResult(
                step_name="登录结果",
                response=ApiResponse(
                    status_code=200,
                    body=json.dumps({"token": secret}, ensure_ascii=False),
                    url=steps[0].request.url,
                    headers={"Content-Type": "application/json"},
                    content_type="application/json",
                ),
            ),
        ),
        ended_at=datetime(2026, 9, 1, 0, 1, tzinfo=UTC),
    )
    repository.save(run)
    return repository


class _RecordingLabRenderer:
    def __init__(self) -> None:
        self.snapshots: list[TerminalSnapshot] = []

    def render(self, snapshot: TerminalSnapshot, destination: Path, theme: object = None) -> Path:
        del theme
        self.snapshots.append(snapshot)
        destination.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (32, 16), (12, 12, 12)).save(destination, format="PNG")
        return destination


class _RecordingApiRenderer:
    def __init__(self) -> None:
        self.evidence = []

    def render(self, evidence: object, destination: Path, theme: str = "dark") -> Path:
        del theme
        self.evidence.append(evidence)
        destination.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (32, 16), (18, 36, 54)).save(destination, format="PNG")
        return destination


def _mixed_set(session_id: str, first: str, second: str) -> EvidenceSet:
    now = datetime(2026, 9, 1, 0, 2, tzinfo=UTC)
    return EvidenceSet(
        id="set-1",
        title="混合证据",
        items=(
            EvidenceItem(
                source=LabCaptureSource(session_id=session_id, capture_id=first),
                title="终端开始",
                caption="第一项",
            ),
            EvidenceItem(
                source=ApiStepSource(run_id="run-1", step_index=1),
                title="接口登录",
                caption="第二项",
                note="用户备注",
            ),
            EvidenceItem(
                source=LabCaptureSource(session_id=session_id, capture_id=second),
                title="终端结束",
                caption="第三项",
            ),
        ),
        created_at=now,
        updated_at=now,
    )


def test_mixed_report_uses_existing_renderers_and_evidence_order(tmp_path: Path) -> None:
    sessions, session_id, first, second = _lab_fixture(tmp_path)
    api_repository = _api_fixture(tmp_path)
    lab_renderer = _RecordingLabRenderer()
    api_renderer = _RecordingApiRenderer()
    resolver = EvidenceSourceResolver(
        LabCaptureResolver(sessions),
        ApiStepResolver(api_repository),
    )
    destination = tmp_path / "mixed-report"

    result = EvidenceReportExporter(
        resolver,
        lab_renderer,
        api_renderer=api_renderer,
    ).export(_mixed_set(session_id, first, second), destination)

    markdown = result.markdown.read_text(encoding="utf-8")
    assert (
        markdown.index("## 终端开始")
        < markdown.index("## 接口登录")
        < markdown.index("## 终端结束")
    )
    assert "图 1 第一项" in markdown
    assert "图 2 第二项" in markdown
    assert "用户备注" in markdown
    assert [path.name[:3] for path in result.images] == ["01-", "02-", "03-"]
    manifest = json.loads(
        (result.destination / ".csbox-generated-report.json").read_text(encoding="utf-8")
    )
    assert [entry["path"] for entry in manifest["files"]] == [
        f"assets/{path.name}" for path in result.images
    ]
    assert [snapshot.cells[0][0].character for snapshot in lab_renderer.snapshots] == ["A", "B"]
    assert [item.step_index for item in api_renderer.evidence] == [1]
    assert all(
        "API_REPORT_SECRET_SENTINEL" not in item.model_dump_json() for item in api_renderer.evidence
    )
    assert "API_REPORT_SECRET_SENTINEL" not in markdown
    assert ".csbox" not in markdown
    assert all(not line.startswith("/") for line in markdown.splitlines())


def test_loaded_v1_lab_set_still_exports_without_rewriting_source_document(
    tmp_path: Path,
) -> None:
    sessions, session_id, first, _second = _lab_fixture(tmp_path)
    repository = EvidenceSetRepository(tmp_path / ".csbox" / "evidence")
    path = repository.path_for("legacy")
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "id": "legacy",
                "title": "旧 Lab 集合",
                "items": [
                    {
                        "source": {
                            "source_type": "lab_capture",
                            "session_id": session_id,
                            "capture_id": first,
                        },
                        "title": "旧标题",
                        "caption": "旧图注",
                        "note": "旧备注",
                    }
                ],
                "created_at": "2026-09-01T00:00:00Z",
                "updated_at": "2026-09-01T00:01:00Z",
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    before = path.read_bytes()
    evidence_set = repository.load("legacy")
    resolver = LabCaptureResolver(sessions)

    result = EvidenceReportExporter(resolver, _RecordingLabRenderer()).export(
        evidence_set,
        tmp_path / "legacy-report",
    )

    assert result.evidence_item_count == 1
    assert path.read_bytes() == before
    assert "旧标题" in result.markdown.read_text(encoding="utf-8")


def test_api_only_report_and_mixed_docx_keep_item_order(tmp_path: Path) -> None:
    sessions, session_id, first, second = _lab_fixture(tmp_path)
    api_repository = _api_fixture(tmp_path)
    resolver = EvidenceSourceResolver(
        LabCaptureResolver(sessions),
        ApiStepResolver(api_repository),
    )
    evidence_set = _mixed_set(session_id, first, second)
    result = EvidenceReportExporter(
        resolver,
        _RecordingLabRenderer(),
        api_renderer=_RecordingApiRenderer(),
    ).export(evidence_set, tmp_path / "api-and-lab")

    with zipfile.ZipFile(result.docx) as package:
        document = ElementTree.fromstring(package.read("word/document.xml"))
        text = "".join(document.itertext())

    assert text.index("终端开始") < text.index("接口登录") < text.index("终端结束")
    assert text.index("图 1 第一项") < text.index("图 2 第二项") < text.index("图 3 第三项")
    assert len(result.images) == 3

    api_only = EvidenceSet(
        id="api-only",
        title="API-only",
        items=(
            EvidenceItem(
                source=ApiStepSource(run_id="run-1", step_index=1),
                title="接口登录",
            ),
        ),
        created_at=evidence_set.created_at,
        updated_at=evidence_set.updated_at,
    )
    api_result = EvidenceReportExporter(
        resolver,
        _RecordingLabRenderer(),
        api_renderer=_RecordingApiRenderer(),
    ).export(api_only, tmp_path / "api-only")
    assert api_result.evidence_item_count == 1
    assert api_result.images[0].is_file()


def test_missing_mixed_source_fails_before_report_publication(tmp_path: Path) -> None:
    sessions, session_id, first, _second = _lab_fixture(tmp_path)
    api_repository = _api_fixture(tmp_path)
    resolver = EvidenceSourceResolver(
        LabCaptureResolver(sessions),
        ApiStepResolver(api_repository),
    )
    evidence_set = _mixed_set(session_id, first, "missing-capture")
    destination = tmp_path / "should-not-publish"

    with pytest.raises(ReportExportError) as raised:
        EvidenceReportExporter(
            resolver,
            _RecordingLabRenderer(),
            api_renderer=_RecordingApiRenderer(),
        ).export(evidence_set, destination)

    assert raised.value.kind == "missing_source"
    assert not destination.exists()


def test_api_source_root_is_protected_as_a_report_destination(tmp_path: Path) -> None:
    sessions, _session_id, _first, _second = _lab_fixture(tmp_path)
    api_repository = _api_fixture(tmp_path)
    resolver = EvidenceSourceResolver(
        LabCaptureResolver(sessions),
        ApiStepResolver(api_repository),
    )
    evidence_set = EvidenceSet(
        id="set-api-destination",
        title="API 证据",
        items=(
            EvidenceItem(
                source=ApiStepSource(run_id="run-1", step_index=1),
                title="接口步骤",
            ),
        ),
        created_at=datetime(2026, 9, 1, tzinfo=UTC),
        updated_at=datetime(2026, 9, 1, tzinfo=UTC),
    )
    destination = api_repository.root / "run-1" / "report"

    with pytest.raises(ReportExportError) as raised:
        EvidenceReportExporter(
            resolver,
            _RecordingLabRenderer(),
            api_renderer=_RecordingApiRenderer(),
        ).export(evidence_set, destination)

    assert raised.value.kind == "destination"
    assert not destination.exists()
