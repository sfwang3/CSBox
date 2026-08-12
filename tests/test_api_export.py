from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from PIL import Image
from typer.testing import CliRunner

import csbox.api.exporter as exporter_module
from csbox.api.errors import ApiPersistenceError
from csbox.api.exporter import ApiEvidenceExporter
from csbox.api.models import (
    ApiAssertion,
    ApiAssertionResult,
    ApiRequest,
    ApiResponse,
    ApiRun,
    ApiRunResult,
    ApiScenario,
    ApiStep,
)
from csbox.api.renderer import ApiEvidenceRenderer
from csbox.api.repository import ApiRunRepository
from csbox.cli.main import app
from csbox.core.fonts import FontResolver

SECRET = "CSBOX_SECRET_SENTINEL_export_task10"
ASCII_FONT_CANDIDATES = (
    Path("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"),
    Path("/mnt/c/Windows/Fonts/CascadiaMono.ttf"),
    Path("C:/Windows/Fonts/consola.ttf"),
)
CJK_FONT_CANDIDATES = (
    Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
    Path("/usr/share/fonts/opentype/noto/NotoSansMonoCJK-Regular.ttc"),
    Path("/mnt/c/Windows/Fonts/msyh.ttc"),
    Path("C:/Windows/Fonts/msyh.ttc"),
)


def _installed(candidates: tuple[Path, ...], purpose: str) -> Path:
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    pytest.skip(f"system {purpose} test font is unavailable")


@pytest.fixture
def exporter() -> ApiEvidenceExporter:
    fonts = (
        _installed(ASCII_FONT_CANDIDATES, "ASCII monospace"),
        _installed(CJK_FONT_CANDIDATES, "CJK"),
    )
    return ApiEvidenceExporter(
        renderer=ApiEvidenceRenderer(FontResolver(candidates=fonts), font_size=16)
    )


def _run() -> ApiRun:
    pass_assertion = ApiAssertion(kind="status", expected=200)
    request = ApiRequest(
        method="GET",
        url=f"https://user:{SECRET}@example.test/中文路径?token={SECRET}&q=中文",
        query={"q": "中文", "token": SECRET},
        headers={"Authorization": f"Bearer {SECRET}", "X-Header": "中文头"},
        json_body={"nested": {"password": SECRET, "message": "请求中文"}},
    )
    body = json.dumps(
        {"ok": True, "message": "响应中文", "nested": {"token": SECRET}},
        ensure_ascii=False,
    )
    response = ApiResponse(
        status_code=200,
        headers={"Content-Type": "application/json", "Set-Cookie": SECRET},
        body=body,
        url=request.url,
        content_type="application/json",
        response_size=len(body.encode()),
    )
    result = ApiRunResult(
        step_name=f"步骤-{SECRET}/../../路径",
        status="PASS",
        response=response,
        assertions=(
            ApiAssertionResult(
                assertion=pass_assertion,
                status="PASS",
                message="断言通过",
                actual=200,
            ),
        ),
        elapsed_ms=12.5,
    )
    return ApiRun(
        id="20260811T100000-export",
        scenario=ApiScenario(
            name="API 导出中文场景",
            source="scenarios/中文.toml",
            variables={"token": SECRET},
            steps=(ApiStep(name=result.step_name, request=request, assertions=(pass_assertion,)),),
        ),
        started_at=datetime(2026, 8, 11, 10, 0, tzinfo=UTC),
        ended_at=datetime(2026, 8, 11, 10, 0, 1, tzinfo=UTC),
        status="PASS",
        results=(result,),
        elapsed_ms=12.5,
    )


def test_export_writes_safe_png_markdown_and_strict_json_roundtrip(
    tmp_path: Path, exporter: ApiEvidenceExporter
) -> None:
    destination = tmp_path / "交付" / "api-evidence"

    result = exporter.export(_run(), destination, theme="dark")

    assert result.destination == destination
    assert [path.relative_to(destination).as_posix() for path in result.evidence] == [
        "evidence/01-步骤-redacted-路径.png"
    ]
    assert result.markdown.name == "api-evidence.md"
    assert result.results.name == "results.json"
    assert result.markdown.is_file()
    assert result.results.is_file()
    for image_path in result.evidence:
        with Image.open(image_path) as image:
            assert image.format == "PNG"
            assert image.mode == "RGB"

    markdown = result.markdown.read_text(encoding="utf-8")
    serialized = result.results.read_text(encoding="utf-8")
    payload = json.loads(
        serialized,
        parse_constant=lambda value: (_ for _ in ()).throw(ValueError()),
    )
    assert payload["schema_version"] == 1
    assert payload["status"] == "PASS"
    assert payload["evidence"][0]["result"]["status"] == "PASS"
    assert "场景" in markdown
    assert "请求" in markdown
    assert "响应" in markdown
    assert "断言" in markdown
    assert "通过" in markdown
    assert "GET" in markdown
    assert "中文" in markdown
    assert SECRET not in markdown
    assert SECRET not in serialized
    assert all(SECRET not in str(path) for path in result.evidence)


def test_export_accepts_a_large_response_already_accepted_by_run_persistence(
    tmp_path: Path,
) -> None:
    body = "x" * (11 * 1024 * 1024)
    base = _run()
    response = base.results[0].response
    assert response is not None
    large_response = response.model_copy(
        update={"body": body, "response_size": len(body.encode("utf-8"))}
    )
    steps = tuple(
        base.scenario.steps[0].model_copy(update={"name": f"large-step-{index}"})
        for index in range(1, 4)
    )
    results = tuple(
        base.results[0].model_copy(update={"step_name": step.name, "response": large_response})
        for step in steps
    )
    run = base.model_copy(
        update={
            "id": "20260811T100000-large-export",
            "scenario": base.scenario.model_copy(update={"steps": steps}),
            "results": results,
        }
    )
    repository = ApiRunRepository(tmp_path / ".csbox" / "api" / "runs")
    repository.save(run)
    persisted = repository.load(run.id)

    class TinyRenderer:
        def render(
            self,
            _evidence: object,
            destination: Path,
            theme: str = "dark",
        ) -> Path:
            del theme
            Image.new("RGB", (1, 1)).save(destination, format="PNG")
            return destination

    class TinyMarkdownRenderer:
        def render(self, _evidence: object) -> str:
            return "# bounded test\n"

    exported = ApiEvidenceExporter(
        renderer=TinyRenderer(),  # type: ignore[arg-type]
        markdown_renderer=TinyMarkdownRenderer(),  # type: ignore[arg-type]
    ).export(persisted, tmp_path / "large-evidence")

    assert exported.results.stat().st_size > 64 * 1024 * 1024
    assert exported.results.stat().st_size <= 144 * 1024 * 1024
    assert json.loads(exported.results.read_text(encoding="utf-8"))["status"] == "PASS"


def test_export_handles_a_deep_json_response_without_leaking_its_secret(
    tmp_path: Path,
) -> None:
    secret = "CSBOX_SECRET_SENTINEL_task17_deep_response"
    body = '{"token":' + ("[" * 10_000) + f'"{secret}"' + ("]" * 10_000) + "}"
    base = _run()
    response = base.results[0].response
    assert response is not None
    deep_response = response.model_copy(
        update={"body": body, "response_size": len(body.encode("utf-8"))}
    )
    run = base.model_copy(
        update={
            "id": "20260811T100000-deep-response",
            "results": (base.results[0].model_copy(update={"response": deep_response}),),
        }
    )
    repository = ApiRunRepository(tmp_path / ".csbox" / "api" / "runs")
    repository.save(run)
    persisted = repository.load(run.id)

    class TinyRenderer:
        def render(
            self,
            _evidence: object,
            destination: Path,
            theme: str = "dark",
        ) -> Path:
            del theme
            Image.new("RGB", (1, 1)).save(destination, format="PNG")
            return destination

    exported = ApiEvidenceExporter(renderer=TinyRenderer()).export(
        persisted,
        tmp_path / "deep-evidence",
    )

    assert secret not in exported.markdown.read_text(encoding="utf-8")
    assert secret not in exported.results.read_text(encoding="utf-8")


def test_export_force_is_explicit_and_preserves_unrelated_user_files(
    tmp_path: Path, exporter: ApiEvidenceExporter
) -> None:
    destination = tmp_path / "existing"
    destination.mkdir()
    marker = destination / "keep.txt"
    marker.write_text("user data", encoding="utf-8")

    with pytest.raises(ApiPersistenceError, match="已存在|覆盖"):
        exporter.export(_run(), destination, theme="light")

    forced = exporter.export(_run(), destination, theme="light", force=True)

    assert forced.results.is_file()
    assert marker.read_text(encoding="utf-8") == "user data"


def test_force_export_removes_only_stale_api_screenshots(
    tmp_path: Path, exporter: ApiEvidenceExporter
) -> None:
    base = _run()
    first_step = base.scenario.steps[0]
    first_result = base.results[0]
    second_step = first_step.model_copy(update={"name": "第二步"})
    second_result = first_result.model_copy(update={"step_name": "第二步"})
    two_step_run = base.model_copy(
        update={
            "scenario": base.scenario.model_copy(update={"steps": (first_step, second_step)}),
            "results": (first_result, second_result),
        }
    )
    destination = tmp_path / "api-evidence"
    first = exporter.export(two_step_run, destination)
    stale = first.evidence[1]
    user_file = destination / "evidence" / "user-note.txt"
    user_file.write_text("keep", encoding="utf-8")

    second = exporter.export(base, destination, force=True)

    assert len(second.evidence) == 1
    assert not stale.exists()
    assert user_file.read_text(encoding="utf-8") == "keep"


def test_force_export_does_not_infer_ownership_for_a_pre_manifest_export(
    tmp_path: Path, exporter: ApiEvidenceExporter
) -> None:
    base = _run()
    first_step = base.scenario.steps[0]
    first_result = base.results[0]
    two_step_run = base.model_copy(
        update={
            "scenario": base.scenario.model_copy(
                update={
                    "steps": (
                        first_step,
                        first_step.model_copy(update={"name": "旧版第二步"}),
                    )
                }
            ),
            "results": (
                first_result,
                first_result.model_copy(update={"step_name": "旧版第二步"}),
            ),
        }
    )
    destination = tmp_path / "legacy-api-evidence"
    first = exporter.export(two_step_run, destination)
    stale = first.evidence[1]
    (destination / ".csbox-generated-api-evidence.json").unlink()

    exporter.export(base, destination, force=True)

    assert stale.exists()


def test_force_export_treats_a_deep_old_manifest_as_untrusted(
    tmp_path: Path,
    exporter: ApiEvidenceExporter,
) -> None:
    destination = tmp_path / "api-evidence"
    exporter.export(_run(), destination)
    manifest = destination / ".csbox-generated-api-evidence.json"
    manifest.write_text("[" * 10_000 + "0" + "]" * 10_000, encoding="utf-8")

    result = exporter.export(_run(), destination, force=True)

    assert result.results.is_file()
    assert json.loads(manifest.read_text(encoding="utf-8"))["version"] == 1


def test_force_export_preserves_a_stale_screenshot_replaced_by_the_user(
    tmp_path: Path, exporter: ApiEvidenceExporter
) -> None:
    base = _run()
    first_step = base.scenario.steps[0]
    first_result = base.results[0]
    two_step_run = base.model_copy(
        update={
            "scenario": base.scenario.model_copy(
                update={
                    "steps": (
                        first_step,
                        first_step.model_copy(update={"name": "第二步"}),
                    )
                }
            ),
            "results": (
                first_result,
                first_result.model_copy(update={"step_name": "第二步"}),
            ),
        }
    )
    destination = tmp_path / "api-evidence"
    first = exporter.export(two_step_run, destination)
    stale = first.evidence[1]
    stale.write_bytes(b"user replacement")

    exporter.export(base, destination, force=True)

    assert stale.read_bytes() == b"user replacement"


def test_force_export_rollback_preserves_a_concurrent_destination_and_old_backup(
    tmp_path: Path,
    exporter: ApiEvidenceExporter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "api-evidence"
    first = exporter.export(_run(), destination)
    protected = first.evidence[0]
    old_generated = protected.read_bytes()
    concurrent = b"concurrent-user-data"
    real_safe_rename = exporter_module.safe_rename
    raced = False

    def race_after_backup(source: Path, target: Path, *, replace_existing: bool = True) -> None:
        nonlocal raced
        if target == protected and source.name == protected.name and not raced:
            raced = True
            assert not target.exists()
            target.write_bytes(concurrent)
        real_safe_rename(source, target, replace_existing=replace_existing)

    monkeypatch.setattr(exporter_module, "safe_rename", race_after_backup)

    with pytest.raises(ApiPersistenceError):
        exporter.export(_run(), destination, force=True)

    assert raced
    assert protected.read_bytes() == old_generated
    recovery = tuple(protected.parent.glob(".csbox-recovery-*.bak"))
    assert len(recovery) == 1
    assert recovery[0].read_bytes() == concurrent
    assert not tuple(tmp_path.glob(".csbox-api-evidence-*.partial"))


def test_force_export_restores_the_old_set_when_a_later_publish_fails(
    tmp_path: Path,
    exporter: ApiEvidenceExporter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "api-evidence"
    first = exporter.export(_run(), destination)
    generated = (
        *first.files,
        destination / ".csbox-generated-api-evidence.json",
    )
    old_contents = {path: path.read_bytes() for path in generated}
    updated = _run().model_copy(update={"id": "20260811T100000-updated"})
    real_safe_rename = exporter_module.safe_rename
    publish_calls = 0

    def fail_second_publish(source: Path, target: Path, *, replace_existing: bool = True) -> None:
        nonlocal publish_calls
        if target in old_contents and source.name == target.name:
            publish_calls += 1
            if publish_calls == 2:
                raise OSError("simulated later publication failure")
        real_safe_rename(source, target, replace_existing=replace_existing)

    monkeypatch.setattr(exporter_module, "safe_rename", fail_second_publish)

    with pytest.raises(ApiPersistenceError):
        exporter.export(updated, destination, force=True)

    assert publish_calls == 2
    assert {path: path.read_bytes() for path in generated} == old_contents
    assert tuple(destination.rglob(".csbox-recovery-*.bak"))
    assert not tuple(tmp_path.glob(".csbox-api-evidence-*.partial"))


def test_export_refuses_symlink_and_non_regular_generated_targets(
    tmp_path: Path, exporter: ApiEvidenceExporter
) -> None:
    destination = tmp_path / "existing"
    (destination / "evidence").mkdir(parents=True)
    protected = tmp_path / "protected.md"
    protected.write_text("protected", encoding="utf-8")
    try:
        (destination / "api-evidence.md").symlink_to(protected)
    except OSError as error:
        pytest.skip(f"symlinks are unavailable: {error}")

    with pytest.raises(ApiPersistenceError, match="符号链接|symlink"):
        exporter.export(_run(), destination, force=True)
    assert protected.read_text(encoding="utf-8") == "protected"

    (destination / "api-evidence.md").unlink()
    (destination / "api-evidence.md").mkdir()
    with pytest.raises(ApiPersistenceError, match="普通文件|regular"):
        exporter.export(_run(), destination, force=True)


def test_export_failure_cleans_private_staging_without_publishing_partial_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FailingRenderer:
        def render(self, *args: object, **kwargs: object) -> Path:
            del args, kwargs
            raise OSError("renderer failed")

    exporter = ApiEvidenceExporter(renderer=FailingRenderer())
    destination = tmp_path / f"{SECRET}-not-published"

    with pytest.raises(ApiPersistenceError, match="导出|保存"):
        exporter.export(_run(), destination)

    assert not destination.exists()
    assert not list(tmp_path.glob(".csbox-api-evidence-*.partial"))
    assert not list(tmp_path.glob(f".{SECRET}-*.partial"))


def test_markdown_escapes_untrusted_assertion_and_error_text(
    tmp_path: Path, exporter: ApiEvidenceExporter
) -> None:
    run = _run().model_copy(
        update={
            "results": (
                _run()
                .results[0]
                .model_copy(
                    update={
                        "assertions": (
                            _run()
                            .results[0]
                            .assertions[0]
                            .model_copy(
                                update={
                                    "message": "\n# injected [link](https://invalid) *em*",
                                }
                            ),
                        ),
                        "error": "\n> quote ~~strike~~",
                    }
                ),
            ),
            "status": "RUNTIME_ERROR",
        }
    )

    exported = exporter.export(run, tmp_path / "escaped")
    markdown = exported.markdown.read_text(encoding="utf-8")

    assert "\\# injected \\[link\\]\\(https://invalid\\) \\*em\\*" in markdown
    assert "\\> quote \\~\\~strike\\~\\~" in markdown
    assert "\n# injected" not in markdown
    assert "\n> quote" not in markdown


def test_export_rejects_symlinked_output_parent_before_creating_artifacts(
    tmp_path: Path, exporter: ApiEvidenceExporter
) -> None:
    real_parent = tmp_path / "real-parent"
    real_parent.mkdir()
    linked_parent = tmp_path / "linked-parent"
    try:
        linked_parent.symlink_to(real_parent, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"symlinks are unavailable: {error}")

    with pytest.raises(ApiPersistenceError, match="符号链接|symlink"):
        exporter.export(_run(), linked_parent / "output")

    assert not (real_parent / "output").exists()


def test_api_export_cli_loads_run_by_prefix_and_never_prints_raw_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, exporter: ApiEvidenceExporter
) -> None:
    monkeypatch.chdir(tmp_path)
    repository = ApiRunRepository.from_cwd(tmp_path)
    run = _run().model_copy(update={"id": "20260811T100000-cli-export"})
    repository.save(run)
    monkeypatch.setattr("csbox.api.cli.ApiEvidenceExporter", lambda: exporter)

    cli_result = CliRunner().invoke(
        app,
        [
            "api",
            "export",
            "20260811T100000-cli",
            "--output",
            "证据输出",
            "--json",
        ],
    )

    assert cli_result.exit_code == 0
    assert SECRET not in cli_result.stdout
    payload = json.loads(cli_result.stdout)
    assert payload["schema_version"] == 1
    assert payload["files"] == [
        "evidence/01-步骤-redacted-路径.png",
        "api-evidence.md",
        "results.json",
    ]
    assert (tmp_path / "证据输出" / "results.json").is_file()
