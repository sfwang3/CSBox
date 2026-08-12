"""Pillow renderer for redacted API evidence cards."""

from __future__ import annotations

import json
import math
import unicodedata
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path
from typing import Literal

from PIL import Image, ImageDraw, ImageFont
from wcwidth import wcwidth

from csbox.api.evidence import redact_evidence
from csbox.api.models import ApiEvidence, ApiRequest, ApiResponse
from csbox.core.fonts import FontResolutionError, FontResolver, ResolvedFonts, font_supports_text
from csbox.core.safe_paths import atomic_write_bytes

RGB = tuple[int, int, int]
ThemeName = Literal["dark", "light"]
MAX_RENDER_PIXELS = 16_000_000
_MAX_FONT_CACHE_ENTRIES = 1024


@dataclass(frozen=True, slots=True)
class ApiRenderTheme:
    background: RGB
    surface: RGB
    section: RGB
    border: RGB
    foreground: RGB
    muted_foreground: RGB
    accent: RGB
    passed: RGB
    failed: RGB
    warning: RGB
    on_status: RGB

    @classmethod
    def dark(cls) -> ApiRenderTheme:
        return cls(
            background=(15, 23, 42),
            surface=(30, 41, 59),
            section=(39, 52, 74),
            border=(71, 85, 105),
            foreground=(248, 250, 252),
            muted_foreground=(203, 213, 225),
            accent=(34, 197, 94),
            passed=(34, 197, 94),
            failed=(239, 68, 68),
            warning=(245, 158, 11),
            on_status=(15, 23, 42),
        )

    @classmethod
    def light(cls) -> ApiRenderTheme:
        return cls(
            background=(248, 250, 252),
            surface=(255, 255, 255),
            section=(241, 245, 249),
            border=(148, 163, 184),
            foreground=(15, 23, 42),
            muted_foreground=(51, 65, 85),
            accent=(21, 128, 61),
            passed=(21, 128, 61),
            failed=(185, 28, 28),
            warning=(180, 83, 9),
            on_status=(255, 255, 255),
        )


@dataclass(frozen=True, slots=True)
class _RenderLine:
    text: str
    role: str = "body"


@dataclass(slots=True)
class _LayoutBuffer:
    width: int
    maximum_lines: int
    lines: list[_RenderLine] = field(default_factory=list)
    truncated: bool = False

    @property
    def full(self) -> bool:
        return len(self.lines) >= self.maximum_lines

    def append(
        self,
        text: str,
        role: str = "body",
        *,
        wrap: bool = True,
        prefix: str = "",
    ) -> bool:
        if self.full:
            self.truncated = True
            return False
        if not wrap:
            self.lines.append(_RenderLine(prefix + text, role))
            return True
        remaining = self.maximum_lines - len(self.lines)
        for piece in _iter_wrapped_display_lines(
            text,
            self.width,
            max_lines=remaining + 1,
            prefix=prefix,
        ):
            if self.full:
                self.truncated = True
                return False
            self.lines.append(_RenderLine(piece, role))
        return True

    def finish(self) -> tuple[_RenderLine, ...]:
        if self.truncated:
            notice = _RenderLine(
                "PNG 内容已截断；完整响应请查看 results.json 或 Markdown。",
                "runtime",
            )
            if self.lines:
                self.lines[-1] = notice
            else:
                self.lines.append(notice)
        return tuple(self.lines)


class ApiEvidenceRenderer:
    """Render an immutable API evidence item on a fixed display-cell grid."""

    def __init__(
        self,
        font_resolver: FontResolver | None = None,
        *,
        font_size: int = 16,
        padding: int = 28,
        width_cells: int = 112,
    ) -> None:
        if font_size <= 0:
            raise ValueError("font_size must be positive")
        if padding < 0:
            raise ValueError("padding must not be negative")
        if width_cells < 20:
            raise ValueError("width_cells must be at least 20")
        self.font_size = font_size
        self.padding = padding
        self.width_cells = width_cells
        self.fonts: ResolvedFonts = (font_resolver or FontResolver()).resolve()
        self._ascii_font = ImageFont.truetype(str(self.fonts.ascii), size=font_size)
        self._cjk_font = ImageFont.truetype(str(self.fonts.cjk), size=font_size)
        self._fallback_fonts = tuple(
            ImageFont.truetype(str(path), size=font_size) for path in self.fonts.fallbacks
        )
        self._font_cache: dict[str, ImageFont.FreeTypeFont] = {}
        ascii_advance = self._ascii_font.getlength("M")
        cjk_half_advance = self._cjk_font.getlength("中") / 2
        self.cell_width = max(1, math.ceil(ascii_advance), math.ceil(cjk_half_advance))
        ascii_ascent, ascii_descent = self._ascii_font.getmetrics()
        cjk_ascent, cjk_descent = self._cjk_font.getmetrics()
        self.line_height = max(ascii_ascent + ascii_descent, cjk_ascent + cjk_descent) + 6

    def render(
        self,
        evidence: ApiEvidence,
        destination: Path | str,
        theme: ThemeName = "dark",
    ) -> Path:
        if not isinstance(evidence, ApiEvidence):
            raise TypeError("evidence must be an ApiEvidence")
        selected_theme = _theme(theme)
        safe_evidence = redact_evidence(evidence)
        image_width = self.width_cells * self.cell_width + self.padding * 2
        minimum_height = self.line_height + self.padding * 2
        if image_width * minimum_height > MAX_RENDER_PIXELS:
            raise ValueError("API 证据图片像素尺寸过大，请缩小渲染宽度后重试。")
        maximum_height = MAX_RENDER_PIXELS // image_width
        maximum_lines = max(1, (maximum_height - self.padding * 2) // self.line_height)
        lines = _layout_lines(safe_evidence, self.width_cells - 4, maximum_lines)
        image_height = max(
            self.line_height + self.padding * 2,
            len(lines) * self.line_height + self.padding * 2,
        )
        image = Image.new("RGB", (image_width, image_height), selected_theme.background)
        draw = ImageDraw.Draw(image)
        self._draw_card(draw, image_width, lines, selected_theme)
        _write_png_atomic(image, Path(destination))
        return Path(destination)

    def _draw_card(
        self,
        draw: ImageDraw.ImageDraw,
        image_width: int,
        lines: tuple[_RenderLine, ...],
        theme: ApiRenderTheme,
    ) -> None:
        for row, line in enumerate(lines):
            y = self.padding + row * self.line_height
            fill = _line_background(line.role, theme)
            if fill is not None:
                draw.rectangle(
                    (self.padding - 8, y, image_width - self.padding + 8, y + self.line_height - 1),
                    fill=fill,
                )
            if line.role == "divider":
                draw.line(
                    (
                        self.padding,
                        y + self.line_height // 2,
                        image_width - self.padding,
                        y + self.line_height // 2,
                    ),
                    fill=theme.border,
                    width=1,
                )
                continue
            foreground = _line_foreground(line.role, theme)
            font = self._font_for_text(line.text)
            draw.text((self.padding, y + 1), line.text, font=font, fill=foreground)

    def _font_for_text(self, text: str) -> ImageFont.FreeTypeFont:
        cached = self._font_cache.get(text)
        if cached is not None:
            return cached
        preferred = (self._cjk_font, *self._fallback_fonts, self._ascii_font)
        if text.isascii():
            preferred = (self._ascii_font, self._cjk_font, *self._fallback_fonts)
        for font in preferred:
            if font_supports_text(font, text):
                if len(self._font_cache) >= _MAX_FONT_CACHE_ENTRIES:
                    self._font_cache.clear()
                self._font_cache[text] = font
                return font
        codepoints = " ".join(f"U+{ord(character):04X}" for character in text)
        raise FontResolutionError(
            f"找不到能显示字符 {codepoints} 的字体，请配置包含该字形的 render.font。"
        )


def _theme(name: ThemeName) -> ApiRenderTheme:
    if name == "dark":
        return ApiRenderTheme.dark()
    if name == "light":
        return ApiRenderTheme.light()
    raise ValueError("theme must be dark or light")


def _layout_lines(
    evidence: ApiEvidence,
    width: int,
    maximum_lines: int,
) -> tuple[_RenderLine, ...]:
    if width <= 8:
        raise ValueError("evidence layout width is too small")
    result = _LayoutBuffer(width, maximum_lines)
    result.append("CSBox / 接口测试证据", "header")
    result.append("=" * min(width, 32), "divider", wrap=False)
    scenario = evidence.scenario_name or "—"
    step_name = evidence.title or "—"
    result.append(f"场景：{scenario}")
    if evidence.scenario_source:
        result.append(f"场景文件：{evidence.scenario_source}", "muted")
    if evidence.run_id:
        result.append(f"运行 ID：{evidence.run_id}", "muted")
    if evidence.started_at is not None:
        result.append(f"开始时间：{evidence.started_at.isoformat()}", "muted")
    result.append(f"步骤 {evidence.step_index or '—'}：{step_name}", "title")
    if evidence.run_status is not None:
        result.append(
            f"运行状态：{_status_text(evidence.run_status)} ({evidence.run_status})",
            _status_role(evidence.run_status),
        )

    result.append("请求", "section")
    result.append(f"{evidence.request.method} {evidence.request.url}", "endpoint")
    _append_mapping(result, "查询参数", evidence.request.query)
    _append_mapping(result, "请求头", evidence.request.headers)
    _append_request_body(result, evidence.request)

    result.append("响应", "section")
    if evidence.response is None:
        result.append("未收到响应", "runtime")
    else:
        _append_response(result, evidence.response)

    result.append("断言", "section")
    if evidence.result is None or not evidence.result.assertions:
        result.append("未记录断言结果", "muted")
    else:
        for assertion in evidence.result.assertions:
            if result.full:
                result.truncated = True
                break
            location = assertion.assertion.location or assertion.assertion.kind
            expected = _format_inline_value(assertion.assertion.expected)
            actual = _format_inline_value(assertion.actual)
            status = _status_text(assertion.status)
            role = _assertion_role(assertion.status)
            result.append(
                f"{status} ({assertion.status})  {location}  expected={expected}  actual={actual}",
                role,
            )
            result.append(f"说明：{assertion.message}", "muted")
    if evidence.result is not None and evidence.result.error:
        result.append(f"错误：{evidence.result.error}", "runtime")

    result.append("元数据", "section")
    if evidence.result is not None:
        result.append(
            f"步骤状态：{_status_text(evidence.result.status)} ({evidence.result.status})",
            _status_role(evidence.result.status),
        )
        result.append(f"步骤耗时：{evidence.result.elapsed_ms:.1f} ms", "muted")
    return result.finish()


def _append_response(result: _LayoutBuffer, response: ApiResponse) -> None:
    content_type = response.content_type or "—"
    exact = "" if response.response_size_exact else "（下限）"
    result.append(
        f"HTTP {response.status_code}  {response.elapsed_ms:.1f} ms  "
        f"{content_type}  {response.response_size} bytes{exact}",
        "response",
    )
    _append_mapping(result, "响应头", response.headers)
    _append_text_block(result, "响应体", response.body)


def _append_request_body(result: _LayoutBuffer, request: ApiRequest) -> None:
    if request.json_body is not None:
        _append_value_block(result, "JSON 请求体", request.json_body)
    if request.body is not None:
        _append_text_block(result, "请求体", request.body)
    if request.form:
        _append_mapping(result, "Form 请求体", request.form)
    if request.multipart:
        _append_mapping(
            result,
            "Multipart 请求体",
            {part.name: part.value for part in request.multipart},
        )
    if (
        request.json_body is None
        and request.body is None
        and not request.form
        and not request.multipart
    ):
        result.append("请求体：未记录", "muted")


def _append_mapping(
    result: _LayoutBuffer,
    label: str,
    values: Mapping[str, str],
) -> None:
    if not values:
        return
    if not result.append(label, "subsection"):
        return
    for name, value in sorted(values.items(), key=lambda item: item[0].casefold()):
        if not result.append(f"  {name}: {value}"):
            return


def _append_value_block(result: _LayoutBuffer, label: str, value: object) -> None:
    if not result.append(label, "subsection"):
        return
    for line in _json_lines(value):
        if not result.append(f"  {line}", "code"):
            return


def _append_text_block(result: _LayoutBuffer, label: str, value: str) -> None:
    if not result.append(label, "subsection"):
        return
    result.append(value, "code", prefix="  ")


def _json_lines(value: object) -> Iterator[str]:
    try:
        rendered = json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2, sort_keys=True)
    except (TypeError, ValueError, RecursionError):
        rendered = "null"
    yield from _iter_logical_lines(rendered)


def _iter_logical_lines(value: str) -> Iterator[str]:
    normalized = value.replace("\r\n", "\n").replace("\r", "\n")
    start = 0
    while True:
        end = normalized.find("\n", start)
        if end < 0:
            yield normalized[start:]
            return
        yield normalized[start:end]
        start = end + 1


def _iter_safe_display_characters(value: str) -> Iterator[str | None]:
    """Yield sanitized characters, using ``None`` as a logical-line boundary."""

    index = 0
    while index < len(value):
        character = value[index]
        if character == "\r":
            if index + 1 < len(value) and value[index + 1] == "\n":
                index += 1
            yield None
        elif character == "\n":
            yield None
        elif character == "\t":
            yield from "    "
        elif unicodedata.category(character) in {"Cc", "Cf"}:
            yield from f"\\u{ord(character):04x}"
        else:
            yield character
        index += 1


def _iter_wrapped_display_lines(
    value: str,
    width: int,
    *,
    max_lines: int,
    prefix: str = "",
) -> Iterator[str]:
    """Sanitize and cell-wrap only as much input as the render budget can show."""

    if width <= 0:
        raise ValueError("width must be positive")
    if max_lines <= 0:
        return

    current: list[str] = []
    used = 0
    cluster: list[str] = []
    cluster_width = 0
    emitted = 0

    def commit_cluster() -> str | None:
        nonlocal current, used, cluster, cluster_width
        if not cluster:
            return None
        if cluster_width > width:
            raise ValueError(
                f"cannot fit a display-width {cluster_width} cluster into width {width}"
            )
        wrapped: str | None = None
        if current and used + cluster_width > width:
            wrapped = "".join(current)
            current = []
            used = 0
        current.append("".join(cluster))
        used += cluster_width
        cluster = []
        cluster_width = 0
        return wrapped

    def characters() -> Iterator[str | None]:
        yield from prefix
        for character in _iter_safe_display_characters(value):
            if character is None:
                yield None
                yield from prefix
            else:
                yield character

    for character in characters():
        if character is None:
            wrapped = commit_cluster()
            if wrapped is not None:
                yield wrapped
                emitted += 1
                if emitted >= max_lines:
                    return
            yield "".join(current)
            emitted += 1
            if emitted >= max_lines:
                return
            current = []
            used = 0
            continue

        character_width = max(wcwidth(character), 0)
        if character_width == 0:
            cluster.append(character)
            continue
        wrapped = commit_cluster()
        if wrapped is not None:
            yield wrapped
            emitted += 1
            if emitted >= max_lines:
                return
        cluster = [character]
        cluster_width = character_width

    wrapped = commit_cluster()
    if wrapped is not None:
        yield wrapped
        emitted += 1
        if emitted >= max_lines:
            return
    yield "".join(current)


def _format_inline_value(value: object) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    except (TypeError, ValueError, RecursionError):
        return "null"


def _status_text(status: str) -> str:
    return {
        "PASS": "通过",
        "FAIL": "失败",
        "SKIP": "跳过",
        "CONFIG_ERROR": "配置错误",
        "RUNTIME_ERROR": "运行错误",
    }.get(status, "未知状态")


def _status_role(status: str) -> str:
    if status == "PASS":
        return "pass"
    if status == "FAIL":
        return "fail"
    return "warning"


def _assertion_role(status: str) -> str:
    if status == "PASS":
        return "pass"
    if status == "FAIL":
        return "fail"
    return "warning"


def _line_background(role: str, theme: ApiRenderTheme) -> RGB | None:
    if role == "header":
        return theme.surface
    if role == "section":
        return theme.section
    if role in {"pass", "fail", "warning"}:
        return theme.surface
    return None


def _line_foreground(role: str, theme: ApiRenderTheme) -> RGB:
    if role == "header":
        return theme.accent
    if role in {"pass", "fail", "warning"}:
        return {"pass": theme.passed, "fail": theme.failed, "warning": theme.warning}[role]
    if role in {"muted", "subsection"}:
        return theme.muted_foreground
    if role == "endpoint":
        return theme.accent
    return theme.foreground


def _write_png_atomic(image: Image.Image, destination: Path) -> None:
    stream = BytesIO()
    image.save(stream, format="PNG")
    try:
        atomic_write_bytes(destination, stream.getvalue())
    except ValueError:
        raise
    except OSError as error:
        from csbox.api.errors import ApiPersistenceError

        raise ApiPersistenceError(
            "发生了什么：PNG 文件无法安全写出。"
            "在哪里：API Evidence PNG。"
            "怎么处理：检查输出目录、普通文件和写入权限后重试。"
        ) from error


__all__ = ["ApiEvidenceRenderer", "ApiRenderTheme", "ThemeName"]
