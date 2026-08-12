from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

import csbox.api.renderer as renderer_module
from csbox.api.evidence import ApiEvidenceBuilder
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
from csbox.core.fonts import FontResolver

SECRET = "CSBOX_SECRET_SENTINEL_renderer_task10"
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
def renderer() -> ApiEvidenceRenderer:
    fonts = (
        _installed(ASCII_FONT_CANDIDATES, "ASCII monospace"),
        _installed(CJK_FONT_CANDIDATES, "CJK"),
    )
    return ApiEvidenceRenderer(FontResolver(candidates=fonts), font_size=16)


def _evidence():
    request = ApiRequest(
        method="POST",
        url=f"https://example.test/中文路径/{SECRET}?q=中文",
        query={"q": "中文", "access_token": SECRET},
        headers={"X-Long": "头" * 80, "Authorization": f"Bearer {SECRET}"},
        json_body={"深层": {"password": SECRET, "items": ["中文", 1]}},
    )
    body = json.dumps(
        {"message": "中文响应", "long": "长文本" * 1800, "nested": {"token": SECRET}},
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
    assertion = ApiAssertion(kind="json_path", location="$.message", expected="中文响应")
    result = ApiRunResult(
        step_name="长内容中文步骤",
        status="PASS",
        response=response,
        assertions=(
            ApiAssertionResult(
                assertion=assertion,
                status="PASS",
                message="JSON 路径符合预期",
                actual="中文响应",
            ),
        ),
        elapsed_ms=84.0,
    )
    run = ApiRun(
        id="20260811T100000-renderer",
        scenario=ApiScenario(
            name="中文场景",
            variables={"token": SECRET},
            steps=(ApiStep(name="长内容中文步骤", request=request, assertions=(assertion,)),),
        ),
        started_at=datetime(2026, 8, 11, 10, 0, tzinfo=UTC),
        ended_at=datetime(2026, 8, 11, 10, 0, 1, tzinfo=UTC),
        status="PASS",
        results=(result,),
        elapsed_ms=84.0,
    )
    return ApiEvidenceBuilder.from_run(run)[0]


def test_renderer_creates_parseable_dark_and_light_png_with_wrapped_cjk_content(
    tmp_path: Path, renderer: ApiEvidenceRenderer
) -> None:
    evidence = _evidence()

    dark = renderer.render(evidence, tmp_path / "深色.png", theme="dark")
    light = renderer.render(evidence, tmp_path / "浅色.png", theme="light")

    assert dark.is_file()
    assert light.is_file()
    with Image.open(dark) as image:
        assert image.format == "PNG"
        assert image.mode == "RGB"
        assert image.width > 500
        assert image.height > 500
        assert image.convert("RGB").getpixel((0, 0)) == (15, 23, 42)
    with Image.open(light) as image:
        assert image.format == "PNG"
        assert image.mode == "RGB"
        assert image.convert("RGB").getpixel((0, 0)) == (248, 250, 252)


def test_renderer_only_draws_redacted_text_and_distinguishes_status_text(
    tmp_path: Path, renderer: ApiEvidenceRenderer, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[str] = []
    original_text = ImageDraw.ImageDraw.text

    def observe_text(
        draw: ImageDraw.ImageDraw,
        xy: tuple[float, float],
        text: str,
        *args: object,
        **kwargs: object,
    ) -> None:
        del xy
        seen.append(text)
        original_text(draw, (0, 0), text, *args, **kwargs)

    monkeypatch.setattr(ImageDraw.ImageDraw, "text", observe_text)
    renderer.render(_evidence(), tmp_path / "safe.png", theme="dark")

    assert seen
    assert SECRET not in "\n".join(seen)
    drawn = "\n".join(seen)
    assert "PASS" in drawn
    assert "通过" in drawn
    assert "POST" in drawn
    assert "中文" in drawn


@pytest.mark.parametrize(
    ("status", "label"),
    [("PASS", "通过"), ("FAIL", "失败"), ("RUNTIME_ERROR", "运行错误")],
)
def test_renderer_distinguishes_pass_fail_and_runtime_error(
    tmp_path: Path,
    renderer: ApiEvidenceRenderer,
    monkeypatch: pytest.MonkeyPatch,
    status: str,
    label: str,
) -> None:
    evidence = _evidence()
    result = evidence.result
    assert result is not None
    safe_result = result.model_copy(
        update={
            "status": status,
            "response": None if status == "RUNTIME_ERROR" else result.response,
        }
    )
    item = evidence.model_copy(
        update={
            "run_status": status,
            "response": safe_result.response,
            "result": safe_result,
        }
    )
    seen: list[str] = []
    original_text = ImageDraw.ImageDraw.text

    def observe_text(
        draw: ImageDraw.ImageDraw,
        xy: tuple[float, float],
        text: str,
        *args: object,
        **kwargs: object,
    ) -> None:
        del xy
        seen.append(text)
        original_text(draw, (0, 0), text, *args, **kwargs)

    monkeypatch.setattr(ImageDraw.ImageDraw, "text", observe_text)
    renderer.render(item, tmp_path / f"{status}.png", theme="dark")

    drawn = "\n".join(seen)
    assert f"({status})" in drawn
    assert label in drawn


def test_renderer_preserves_existing_destination_when_png_encoding_fails(
    tmp_path: Path, renderer: ApiEvidenceRenderer, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "evidence.png"
    destination.write_bytes(b"previous png")

    def fail_save(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise OSError("disk full")

    monkeypatch.setattr(Image.Image, "save", fail_save)

    with pytest.raises(OSError, match="disk full"):
        renderer.render(_evidence(), destination, theme="dark")

    assert destination.read_bytes() == b"previous png"
    assert not list(tmp_path.glob("*.tmp"))


def test_renderer_refuses_symlink_destination_without_touching_target(
    tmp_path: Path, renderer: ApiEvidenceRenderer
) -> None:
    protected = tmp_path / "protected.png"
    protected.write_bytes(b"protected")
    destination = tmp_path / "evidence.png"
    try:
        destination.symlink_to(protected)
    except OSError as error:
        pytest.skip(f"symlinks are unavailable: {error}")

    with pytest.raises((ValueError, OSError), match="符号链接|symlink"):
        renderer.render(_evidence(), destination, theme="dark")

    assert protected.read_bytes() == b"protected"


def test_renderer_bounds_pixel_allocation_for_a_large_but_valid_api_response(
    tmp_path: Path,
    renderer: ApiEvidenceRenderer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evidence = _evidence()
    assert evidence.response is not None
    response = evidence.response.model_copy(
        update={
            "body": "响应中文" * 32_768,
            "response_size": len(("响应中文" * 32_768).encode("utf-8")),
        }
    )
    item = evidence.model_copy(
        update={
            "response": response,
            "result": evidence.result.model_copy(update={"response": response})
            if evidence.result is not None
            else None,
        }
    )
    created_sizes: list[tuple[int, int]] = []
    original_new = Image.new

    def bounded_new(
        mode: str, size: tuple[int, int], *args: object, **kwargs: object
    ) -> Image.Image:
        created_sizes.append(size)
        assert size[0] * size[1] <= 16_000_000
        return original_new(mode, size, *args, **kwargs)

    monkeypatch.setattr(Image, "new", bounded_new)

    output = renderer.render(item, tmp_path / "bounded.png")

    assert output.is_file()
    assert created_sizes
    with Image.open(output) as image:
        assert image.width * image.height <= 16_000_000


def test_renderer_bounds_wrapped_line_work_before_truncating_large_response(
    tmp_path: Path,
    renderer: ApiEvidenceRenderer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evidence = _evidence()
    assert evidence.response is not None
    body = "x" * (1024 * 1024)
    response = evidence.response.model_copy(
        update={"body": body, "response_size": len(body.encode("utf-8"))}
    )
    item = evidence.model_copy(
        update={
            "response": response,
            "result": evidence.result.model_copy(update={"response": response})
            if evidence.result is not None
            else None,
        }
    )
    classified_characters = 0
    drawn: list[str] = []
    real_category = renderer_module.unicodedata.category
    real_text = ImageDraw.ImageDraw.text

    def observe_category(character: str) -> str:
        nonlocal classified_characters
        classified_characters += 1
        return real_category(character)

    def observe_text(
        draw: ImageDraw.ImageDraw,
        xy: tuple[float, float],
        text: str,
        *args: object,
        **kwargs: object,
    ) -> None:
        drawn.append(text)
        real_text(draw, xy, text, *args, **kwargs)

    monkeypatch.setattr(renderer_module.unicodedata, "category", observe_category)
    monkeypatch.setattr(ImageDraw.ImageDraw, "text", observe_text)

    renderer.render(item, tmp_path / "bounded-layout.png")

    image_width = renderer.width_cells * renderer.cell_width + renderer.padding * 2
    maximum_height = renderer_module.MAX_RENDER_PIXELS // image_width
    maximum_lines = max(1, (maximum_height - renderer.padding * 2) // renderer.line_height)
    assert classified_characters <= maximum_lines * renderer.width_cells * 4
    assert any("内容已截断" in text for text in drawn)


def test_renderer_rejects_an_oversized_width_before_image_allocation(
    tmp_path: Path,
    renderer: ApiEvidenceRenderer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    renderer.cell_width = 1_000_000

    def unexpected_image(*args: object, **kwargs: object) -> Image.Image:
        del args, kwargs
        raise AssertionError("Image.new must not receive an oversized width")

    monkeypatch.setattr(Image, "new", unexpected_image)

    with pytest.raises(ValueError, match="像素|pixel|过大"):
        renderer.render(_evidence(), tmp_path / "too-wide.png")
