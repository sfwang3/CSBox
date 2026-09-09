from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

import csbox.evidence.models as evidence_models
import csbox.evidence.resolver as evidence_resolver
from csbox.api.models import ApiRequest, ApiResponse, ApiRun, ApiRunResult, ApiScenario, ApiStep
from csbox.api.repository import ApiRunRepository
from csbox.evidence.models import ApiStepSource, EvidenceItem
from csbox.evidence.repository import EvidencePersistenceError, EvidenceSetRepository
from csbox.lab.repository import SessionRepository


def _typed_source(name: str):
    assert hasattr(evidence_models, name), f"missing typed source model: {name}"
    return getattr(evidence_models, name)


def test_typed_sources_have_complete_discriminated_identity() -> None:
    lab_type = _typed_source("LabCaptureSource")
    api_type = _typed_source("ApiStepSource")

    lab = lab_type(source_type="lab_capture", session_id="session-1", capture_id="capture-1")
    api = api_type(source_type="api_step", run_id="run-1", step_index=1)

    assert lab.equality_key == ("lab_capture", "session-1", "capture-1")
    assert api.equality_key == ("api_step", "run-1", 1)
    assert EvidenceItem(source=lab, title="终端").source is lab
    assert EvidenceItem(source=api, title="接口").source is api


@pytest.mark.parametrize(
    "payload",
    [
        {"source_type": "future", "session_id": "s", "capture_id": "c"},
        {"source_type": "api_step", "run_id": "r", "step_index": 0},
        {"source_type": "api_step", "run_id": "../escape", "step_index": 1},
        {"source_type": "api_step", "run_id": "r", "step_index": True},
        {"source_type": "api_step", "run_id": "r", "step_index": 1, "extra": 1},
    ],
)
def test_source_union_rejects_invalid_or_unknown_payloads(payload: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        EvidenceItem.model_validate({"source": payload, "title": "证据"})


def test_legacy_constructor_rejects_unknown_source_discriminator() -> None:
    with pytest.raises(ValueError):
        evidence_models.EvidenceSource(
            source_type="future_source",
            session_id="s",
            capture_id="c",
        )


def test_v1_load_is_read_only_and_explicit_save_migrates_to_v2(tmp_path: Path) -> None:
    repository = EvidenceSetRepository(tmp_path / "evidence")
    path = repository.path_for("legacy")
    path.parent.mkdir()
    legacy = {
        "version": 1,
        "id": "legacy",
        "title": "旧集合",
        "items": [
            {
                "source": {
                    "source_type": "lab_capture",
                    "session_id": "s",
                    "capture_id": "c",
                },
                "title": "标题",
                "caption": "图注",
                "note": "备注",
            },
            {
                "source": {
                    "source_type": "lab_capture",
                    "session_id": "s",
                    "capture_id": "c-2",
                },
                "title": "第二项",
                "caption": "",
                "note": "",
            },
        ],
        "created_at": "2026-09-01T00:00:00Z",
        "updated_at": "2026-09-01T00:01:00Z",
    }
    path.write_text(json.dumps(legacy, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    before = path.read_bytes()

    loaded = repository.load("legacy")
    lab_type = _typed_source("LabCaptureSource")
    assert isinstance(loaded.items[0].source, lab_type)
    assert [item.title for item in loaded.items] == ["标题", "第二项"]
    assert loaded.items[0].caption == "图注"
    assert loaded.items[0].note == "备注"
    assert loaded.created_at == datetime(2026, 9, 1, tzinfo=UTC)
    assert path.read_bytes() == before

    saved = repository.save(loaded)

    persisted = json.loads(path.read_text(encoding="utf-8"))
    assert persisted["version"] == 2
    assert persisted["items"][0]["source"] == {
        "source_type": "lab_capture",
        "session_id": "s",
        "capture_id": "c",
    }
    assert [item.source.equality_key for item in saved.items] == [
        ("lab_capture", "s", "c"),
        ("lab_capture", "s", "c-2"),
    ]


def test_new_evidence_set_documents_use_version_two(tmp_path: Path) -> None:
    repository = EvidenceSetRepository(
        tmp_path / "evidence",
        id_factory=lambda: "new-set",
        clock=lambda: datetime(2026, 9, 1, tzinfo=UTC),
    )

    repository.create("新集合")

    persisted = json.loads(repository.path_for("new-set").read_text(encoding="utf-8"))
    assert persisted["version"] == 2


def test_api_source_round_trips_as_reference_only_v2_data(tmp_path: Path) -> None:
    api_repository = ApiRunRepository(tmp_path / ".csbox" / "api" / "runs")
    api_repository.save(_api_run("run-1"))
    repository = EvidenceSetRepository(
        tmp_path / ".csbox" / "evidence",
        id_factory=lambda: "api-set",
        clock=lambda: datetime(2026, 9, 1, tzinfo=UTC),
    )
    created = repository.create("API 证据集")
    source = ApiStepSource(run_id="run-1", step_index=2)

    saved = repository.save(
        created.model_copy(
            update={
                "items": (
                    EvidenceItem(
                        source=source,
                        title="用户编辑标题",
                        caption="用户图注",
                        note="用户备注",
                    ),
                )
            }
        )
    )
    raw = json.loads(repository.path_for("api-set").read_text(encoding="utf-8"))
    loaded = repository.load("api-set")

    assert raw["items"] == [
        {
            "source": {"source_type": "api_step", "run_id": "run-1", "step_index": 2},
            "title": "用户编辑标题",
            "caption": "用户图注",
            "note": "用户备注",
        }
    ]
    assert raw["items"][0]["source"] != {
        "source_type": "api_step",
        "run_id": "run-1",
        "step_index": 2,
        "request": {},
    }
    assert saved.items[0].source.equality_key == ("api_step", "run-1", 2)
    assert loaded.items[0].source == source
    assert loaded.items[0].title == "用户编辑标题"
    assert loaded.items[0].caption == "用户图注"
    assert loaded.items[0].note == "用户备注"


def test_mixed_evidence_set_mutations_preserve_order_and_source_identity(
    tmp_path: Path,
) -> None:
    repository = EvidenceSetRepository(
        tmp_path / "evidence",
        id_factory=lambda: "mixed-set",
        clock=lambda: datetime(2026, 9, 1, tzinfo=UTC),
    )
    created = repository.create("混合集合")
    lab_first = _typed_source("LabCaptureSource")(session_id="session-1", capture_id="capture-1")
    api = ApiStepSource(run_id="run-1", step_index=1)
    lab_second = _typed_source("LabCaptureSource")(session_id="session-1", capture_id="capture-2")
    items = (
        EvidenceItem(source=lab_first, title="终端一"),
        EvidenceItem(source=api, title="API 一"),
        EvidenceItem(source=lab_second, title="终端二"),
    )

    repository.save(created.model_copy(update={"items": items}))
    reloaded = repository.load("mixed-set")
    edited = reloaded.items[1].model_copy(
        update={"title": "API 已编辑", "caption": "图注", "note": "备注"}
    )
    reordered = (reloaded.items[2], edited, reloaded.items[0])
    repository.save(reloaded.model_copy(update={"items": reordered}))
    after_reorder = repository.load("mixed-set")
    repository.save(after_reorder.model_copy(update={"items": after_reorder.items[:2]}))
    final = repository.load("mixed-set")

    assert [item.source.equality_key for item in final.items] == [
        ("lab_capture", "session-1", "capture-2"),
        ("api_step", "run-1", 1),
    ]
    assert final.items[1].title == "API 已编辑"
    assert final.items[1].caption == "图注"
    assert final.items[1].note == "备注"


def test_save_revalidates_model_construct_candidates_before_persistence(tmp_path: Path) -> None:
    repository = EvidenceSetRepository(tmp_path / "evidence", id_factory=lambda: "set-1")
    created = repository.create("原始集合")
    before = repository.path_for("set-1").read_bytes()
    candidate = created.model_construct(
        evidence_set_id="set-1",
        title="不应持久化",
        items=(
            {
                "source": {
                    "source_type": "future_source",
                    "session_id": "s",
                    "capture_id": "c",
                },
                "title": "未知来源",
                "caption": "",
                "note": "",
            },
        ),
        created_at=created.created_at,
        updated_at=created.updated_at,
    )

    with pytest.raises(EvidencePersistenceError):
        repository.save(candidate)

    assert repository.path_for("set-1").read_bytes() == before


def _api_run(run_id: str, *, secret: str = "API_SECRET_SENTINEL") -> ApiRun:
    steps = tuple(
        ApiStep(
            name=name,
            request=ApiRequest(
                method="GET",
                url=f"https://example.test/{index}?token={secret}",
                headers={"Authorization": f"Bearer {secret}"},
            ),
        )
        for index, name in enumerate(("第一步", "第二步"), 1)
    )
    results = tuple(
        ApiRunResult(
            step_name=step.name,
            response=ApiResponse(
                status_code=200,
                body=json.dumps({"step": step.name, "token": secret}, ensure_ascii=False),
                url=step.request.url,
                headers={"Content-Type": "application/json"},
                content_type="application/json",
            ),
        )
        for step in steps
    )
    return ApiRun(
        id=run_id,
        scenario=ApiScenario(
            name="中文 API 场景",
            variables={"token": secret},
            steps=steps,
        ),
        started_at=datetime(2026, 9, 1, tzinfo=UTC),
        results=results,
        ended_at=datetime(2026, 9, 1, 0, 1, tzinfo=UTC),
    )


def _api_step_resolver(repository: ApiRunRepository):
    assert hasattr(evidence_resolver, "ApiStepResolver")
    return evidence_resolver.ApiStepResolver(repository)


def test_api_step_resolver_uses_saved_run_and_builder_without_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = ApiRunRepository(tmp_path / ".csbox" / "api" / "runs")
    repository.save(_api_run("run-1"))
    resolver = _api_step_resolver(repository)

    def fail_network(*args: object, **kwargs: object) -> None:
        raise AssertionError("API source resolution must not send a request")

    monkeypatch.setattr("httpx.Client.request", fail_network)
    resolved = resolver.resolve(ApiStepSource(source_type="api_step", run_id="run-1", step_index=2))

    assert resolved.available is True
    assert resolved.evidence is not None
    assert resolved.evidence.step_index == 2
    assert resolved.evidence.title == "第二步"
    assert resolved.scenario_name == "中文 API 场景"
    assert "API_SECRET_SENTINEL" not in resolved.evidence.model_dump_json()
    assert "••••••••" in resolved.evidence.model_dump_json()


@pytest.mark.parametrize(
    ("source", "expected_reason"),
    [
        (ApiStepSource(source_type="api_step", run_id="missing", step_index=1), "run_missing"),
        (ApiStepSource(source_type="api_step", run_id="run-1", step_index=3), "step_missing"),
        (
            ApiStepSource.model_construct(run_id="../escape", step_index=1),
            "invalid_source_reference",
        ),
    ],
)
def test_api_step_resolver_retains_controlled_unavailable_states(
    tmp_path: Path,
    source: ApiStepSource,
    expected_reason: str,
) -> None:
    repository = ApiRunRepository(tmp_path / ".csbox" / "api" / "runs")
    if source.run_id == "run-1":
        repository.save(_api_run("run-1"))

    resolved = _api_step_resolver(repository).resolve(source)

    assert resolved.available is False
    assert resolved.evidence is None
    assert resolved.unavailable_reason == expected_reason


def test_corrupt_api_run_is_unavailable_without_hiding_other_runs(tmp_path: Path) -> None:
    repository = ApiRunRepository(tmp_path / ".csbox" / "api" / "runs")
    repository.save(_api_run("healthy"))
    corrupt = repository.root / "corrupt"
    corrupt.mkdir(parents=True)
    (corrupt / "metadata.json").write_text("{bad", encoding="utf-8")

    resolved = _api_step_resolver(repository).resolve(
        ApiStepSource(source_type="api_step", run_id="corrupt", step_index=1)
    )

    assert resolved.available is False
    assert resolved.unavailable_reason == "run_unavailable"
    assert [summary.id for summary in repository.list()] == ["healthy"]


def test_source_router_dispatches_api_and_rejects_unknown_source_type(tmp_path: Path) -> None:
    repository = ApiRunRepository(tmp_path / ".csbox" / "api" / "runs")
    repository.save(_api_run("run-1"))
    assert hasattr(evidence_resolver, "EvidenceSourceResolver")
    router = evidence_resolver.EvidenceSourceResolver(
        evidence_resolver.LabCaptureResolver(SessionRepository(tmp_path / "sessions")),
        _api_step_resolver(repository),
    )

    resolved = router.resolve(ApiStepSource(source_type="api_step", run_id="run-1", step_index=1))
    unknown = router.resolve(
        SimpleNamespace(source_type="future_source", session_id="s", capture_id="c")
    )

    assert resolved.available is True
    assert resolved.api_evidence is not None
    assert unknown.available is False
    assert unknown.unavailable_reason == "unsupported_source_type"
