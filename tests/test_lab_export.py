from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest

from csbox.core.events import TerminalEvent, TerminalEventType
from csbox.lab import exporter as exporter_module
from csbox.lab.captures import CaptureStore
from csbox.lab.exporter import LabExporter, LabExportError
from csbox.lab.fonts import FontResolver
from csbox.lab.models import SessionPaths
from csbox.lab.renderer import RenderTheme, TerminalEvidenceRenderer
from csbox.lab.screen import TerminalEmulator, TerminalSnapshot

FIXTURE_CAST = Path(__file__).parent / "fixtures" / "sessions" / "mixed.cast"
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


def installed_font(candidates: tuple[Path, ...], purpose: str) -> Path:
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    pytest.skip(f"system {purpose} test font is unavailable")


def rendered_snapshot(text: str, relative_time: float) -> TerminalSnapshot:
    emulator = TerminalEmulator(columns=16, rows=2)
    emulator.apply(
        TerminalEvent(
            sequence=1,
            monotonic_time=relative_time,
            relative_time=relative_time,
            type=TerminalEventType.OUTPUT,
            payload=text.encode(),
        )
    )
    return emulator.snapshot()


@pytest.fixture
def session_with_captures(tmp_path: Path) -> SessionPaths:
    paths = SessionPaths(tmp_path / "session")
    paths.root.mkdir()
    paths.cast.write_bytes(FIXTURE_CAST.read_bytes())
    ids = iter(("capture-1", "capture-2"))
    times = iter(
        (
            datetime(2026, 8, 10, 14, 23, 41, tzinfo=UTC),
            datetime(2026, 8, 10, 14, 24, 10, tzinfo=UTC),
        )
    )
    store = CaptureStore(
        paths.captures,
        id_factory=lambda: next(ids),
        clock=lambda: next(times),
    )
    store.create_capture(
        rendered_snapshot("ip addr", 1.0),
        cwd=tmp_path,
        command="ip addr",
        title="查看网络接口",
    )
    store.create_capture(
        rendered_snapshot("实验完成", 2.0),
        cwd=tmp_path,
        command=None,
        title="../../课程/实验二:*?",
    )
    return paths


@pytest.fixture
def exporter() -> LabExporter:
    fonts = (
        installed_font(ASCII_FONT_CANDIDATES, "ASCII monospace"),
        installed_font(CJK_FONT_CANDIDATES, "CJK"),
    )
    renderer = TerminalEvidenceRenderer(FontResolver(candidates=fonts), font_size=16)
    return LabExporter(renderer, theme=RenderTheme.dark())


def test_export_writes_ordered_safe_pngs_cast_commands_and_stable_chinese_markdown(
    tmp_path: Path,
    session_with_captures: SessionPaths,
    exporter: LabExporter,
) -> None:
    destination = tmp_path / "导出" / "网络实验"

    result = exporter.export(session_with_captures, destination)

    assert result.destination == destination
    assert [path.name for path in result.evidence] == [
        "01-查看网络接口.png",
        "02-课程-实验二.png",
    ]
    assert all(path.parent == destination / "evidence" for path in result.evidence)
    assert all(path.is_file() for path in result.evidence)
    assert result.cast.read_bytes() == session_with_captures.cast.read_bytes()
    markdown = result.markdown.read_text(encoding="utf-8")
    assert markdown.startswith(
        "## 实验记录\n\n"
        "### 1. 查看网络接口\n\n"
        "时间：\n"
        "14:23:41\n\n"
        "结果：\n\n"
        "![实验记录](evidence/01-查看网络接口.png)\n"
    )
    assert r"### 2. ../../课程/实验二:\*?" in markdown
    assert "![实验记录](evidence/02-课程-实验二.png)" in markdown
    assert "命令：" not in markdown
    assert result.commands is not None
    assert result.commands.read_text(encoding="utf-8") == "ip addr\n"
    assert "unknown" not in result.commands.read_text(encoding="utf-8").lower()
    assert any("metadata" in warning for warning in result.warnings)
    assert not (tmp_path / "课程").exists()


def test_export_omits_commands_file_when_every_capture_command_is_unknown(
    tmp_path: Path, exporter: LabExporter
) -> None:
    paths = SessionPaths(tmp_path / "session")
    paths.root.mkdir()
    paths.cast.write_bytes(FIXTURE_CAST.read_bytes())
    CaptureStore(paths.captures).create_capture(
        rendered_snapshot("done", 1.0),
        cwd=tmp_path,
        command=None,
        title="完成",
    )

    result = exporter.export(paths.root, tmp_path / "export")

    assert result.commands is None
    assert not (result.destination / "commands.txt").exists()


def test_export_with_empty_captures_still_copies_cast_and_writes_markdown(
    tmp_path: Path, exporter: LabExporter
) -> None:
    paths = SessionPaths(tmp_path / "session")
    paths.root.mkdir()
    paths.cast.write_bytes(FIXTURE_CAST.read_bytes())

    result = exporter.export(paths, tmp_path / "export")

    assert result.evidence == ()
    assert result.commands is None
    assert result.cast.read_bytes() == paths.cast.read_bytes()
    assert result.markdown.read_text(encoding="utf-8") == "## 实验记录\n"


def test_export_propagates_png_renderer_failure(
    tmp_path: Path, session_with_captures: SessionPaths
) -> None:
    class FailingRenderer:
        def render(self, *args: object, **kwargs: object) -> Path:
            raise RuntimeError("png renderer failed")

    exporter = LabExporter(cast(Any, FailingRenderer()))

    with pytest.raises(RuntimeError, match="png renderer failed"):
        exporter.export(session_with_captures, tmp_path / "export")


def test_export_refuses_existing_destination_without_force_and_force_is_explicit(
    tmp_path: Path,
    session_with_captures: SessionPaths,
    exporter: LabExporter,
) -> None:
    destination = tmp_path / "existing"
    destination.mkdir()
    marker = destination / "keep.txt"
    marker.write_text("user data", encoding="utf-8")

    with pytest.raises(LabExportError, match="已存在|覆盖"):
        exporter.export(session_with_captures, destination)

    assert marker.read_text(encoding="utf-8") == "user data"
    forced = exporter.export(session_with_captures, destination, force=True)
    assert forced.markdown.is_file()
    assert marker.read_text(encoding="utf-8") == "user data"


def test_export_escapes_markdown_html_and_percent_encodes_reserved_link_characters(
    tmp_path: Path, exporter: LabExporter
) -> None:
    paths = SessionPaths(tmp_path / "session")
    paths.root.mkdir()
    paths.cast.write_bytes(FIXTURE_CAST.read_bytes())
    CaptureStore(paths.captures).create_capture(
        rendered_snapshot("done", 1.0),
        cwd=tmp_path,
        title="C# (测试)% <b>*粗*</b> [链接](x)",
    )

    result = exporter.export(paths, tmp_path / "export")
    markdown = result.markdown.read_text(encoding="utf-8")

    assert "<b>" not in markdown
    assert "&lt;b&gt;" in markdown
    assert r"C\# \(测试\)\%" in markdown
    assert r"\*粗\*" in markdown
    assert r"\[链接\]\(x\)" in markdown
    image_link = markdown.split("![实验记录](", maxsplit=1)[1].split(")", maxsplit=1)[0]
    assert "%23" in image_link
    assert "%28" in image_link
    assert "%29" in image_link
    assert "%25" in image_link
    assert "测试" in image_link


def test_export_limits_utf8_and_windows_component_length_with_collision_hash(
    tmp_path: Path, exporter: LabExporter
) -> None:
    paths = SessionPaths(tmp_path / "session")
    paths.root.mkdir()
    paths.cast.write_bytes(FIXTURE_CAST.read_bytes())
    store = CaptureStore(paths.captures)
    common = "实验😀" * 100
    for suffix in ("甲", "乙"):
        store.create_capture(
            rendered_snapshot("done", 1.0),
            cwd=tmp_path,
            title=common + suffix,
        )

    result = exporter.export(paths, tmp_path / "export")

    assert len(result.evidence) == 2
    assert result.evidence[0].name != result.evidence[1].name
    for image in result.evidence:
        assert len(image.name.encode("utf-8")) <= 240
        assert len(image.name.encode("utf-16-le")) // 2 <= 240
        assert image.is_file()


def test_export_rejects_control_and_bidi_commands_instead_of_emitting_terminal_actions(
    tmp_path: Path, exporter: LabExporter
) -> None:
    paths = SessionPaths(tmp_path / "session")
    paths.root.mkdir()
    paths.cast.write_bytes(FIXTURE_CAST.read_bytes())
    store = CaptureStore(paths.captures)
    commands = (
        "ip addr",
        "printf '\x1b[2J'",
        "printf '\x1b]8;;https://example.invalid\x07link\x1b]8;;\x07'",
        "echo ab\bcd",
        "echo safe\u202egnp.exe",
    )
    for index, command in enumerate(commands):
        store.create_capture(
            rendered_snapshot(str(index), float(index)),
            cwd=tmp_path,
            command=command,
            title=f"记录 {index}",
        )

    result = exporter.export(paths, tmp_path / "export")

    assert result.commands is not None
    assert result.commands.read_text(encoding="utf-8") == "ip addr\n"


def test_export_rejects_session_sidecar_symlinks_before_reading_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = SessionPaths(tmp_path / "session")
    paths.root.mkdir()
    external = tmp_path / "outside-sensitive-placeholder.cast"
    external.write_text("fake private content", encoding="utf-8")
    try:
        paths.cast.symlink_to(external)
    except OSError as exc:
        pytest.skip(f"symlinks are unavailable: {exc}")

    class UnexpectedRenderer:
        def render(self, *args: object, **kwargs: object) -> Path:
            raise AssertionError("renderer must not run for an unsafe session")

    def unexpected_copy(*args: object, **kwargs: object) -> None:
        raise AssertionError("unsafe session.cast must not be copied")

    monkeypatch.setattr("csbox.lab.exporter.shutil.copyfile", unexpected_copy)
    exporter = LabExporter(cast(Any, UnexpectedRenderer()))
    destination = tmp_path / "export"

    with pytest.raises(LabExportError, match="符号链接|实验目录"):
        exporter.export(paths, destination)

    assert external.read_text(encoding="utf-8") == "fake private content"
    assert not destination.exists()


def test_force_export_removes_only_stale_generated_evidence(
    tmp_path: Path,
    session_with_captures: SessionPaths,
    exporter: LabExporter,
) -> None:
    destination = tmp_path / "export"
    first = exporter.export(session_with_captures, destination)
    stale = first.evidence[1]
    user_png = destination / "evidence" / "notes.png"
    user_png.write_bytes(b"user image")
    user_file = destination / "evidence" / "keep.txt"
    user_file.write_text("keep", encoding="utf-8")
    assert CaptureStore(session_with_captures.captures).delete("capture-2")

    second = exporter.export(session_with_captures, destination, force=True)

    assert [path.name for path in second.evidence] == ["01-查看网络接口.png"]
    assert not stale.exists()
    assert user_png.read_bytes() == b"user image"
    assert user_file.read_text(encoding="utf-8") == "keep"


def test_force_export_preserves_user_evidence_referenced_by_markdown(
    tmp_path: Path,
    session_with_captures: SessionPaths,
    exporter: LabExporter,
) -> None:
    destination = tmp_path / "export"
    first = exporter.export(session_with_captures, destination)
    stale = first.evidence[1]
    user_png = destination / "evidence" / "user-reference.png"
    user_png.write_bytes(b"user image")
    with (destination / "evidence.md").open("a", encoding="utf-8") as stream:
        stream.write("\n![实验记录](evidence/user-reference.png)\n")
    assert CaptureStore(session_with_captures.captures).delete("capture-2")

    exporter.export(session_with_captures, destination, force=True)

    assert not stale.exists()
    assert user_png.read_bytes() == b"user image"


def test_force_export_does_not_follow_backslash_encoded_history_path(
    tmp_path: Path, session_with_captures: SessionPaths, exporter: LabExporter
) -> None:
    destination = tmp_path / "export"
    destination.mkdir()
    (destination / "evidence").mkdir()
    victim = tmp_path / "victim.txt"
    victim.write_text("keep", encoding="utf-8")
    (destination / "evidence.md").write_text(
        "![实验记录](evidence/..%5C..%5Cvictim.txt)\n", encoding="utf-8"
    )
    (destination / ".csbox-generated-evidence.json").write_text(
        json.dumps({"version": 1, "files": ["evidence/..\\..\\victim.txt"]}),
        encoding="utf-8",
    )

    generated, _ = exporter_module._previous_generated_evidence(destination)

    assert generated == ()

    exporter.export(session_with_captures, destination, force=True)

    assert victim.read_text(encoding="utf-8") == "keep"


def test_force_export_skips_all_cleanup_for_a_mixed_invalid_manifest(
    tmp_path: Path,
    session_with_captures: SessionPaths,
    exporter: LabExporter,
) -> None:
    destination = tmp_path / "export"
    first = exporter.export(session_with_captures, destination)
    stale = first.evidence[1]
    manifest = destination / ".csbox-generated-evidence.json"
    document = json.loads(manifest.read_text(encoding="utf-8"))
    document["files"].append("evidence/../../victim.txt")
    manifest.write_text(json.dumps(document), encoding="utf-8")
    assert CaptureStore(session_with_captures.captures).delete("capture-2")

    result = exporter.export(session_with_captures, destination, force=True)

    assert stale.exists()
    assert any("manifest" in warning for warning in result.warnings)


def test_export_uses_session_start_plus_capture_offset_and_warns_on_bad_metadata(
    tmp_path: Path, exporter: LabExporter
) -> None:
    paths = SessionPaths(tmp_path / "session")
    paths.root.mkdir()
    paths.cast.write_bytes(FIXTURE_CAST.read_bytes())
    CaptureStore(
        paths.captures,
        clock=lambda: datetime(2026, 8, 10, 20, 0, tzinfo=UTC),
    ).create_capture(
        rendered_snapshot("done", 90.0),
        cwd=tmp_path,
        title="时间证据",
    )
    metadata = {
        "id": "session-1",
        "name": "实验",
        "status": "completed",
        "startedAt": "2026-08-10T14:23:00Z",
        "endedAt": "2026-08-10T14:30:00Z",
        "platform": "linux",
        "shell": "bash",
        "shellVersion": "5.2",
        "initialRows": 24,
        "initialColumns": 80,
        "cwd": str(tmp_path),
        "csboxVersion": "0.1.0",
    }
    paths.metadata.write_text(json.dumps(metadata), encoding="utf-8")

    reliable = exporter.export(paths, tmp_path / "reliable")

    assert "14:24:30" in reliable.markdown.read_text(encoding="utf-8")
    assert not any("metadata" in warning for warning in reliable.warnings)

    paths.metadata.write_text("{broken", encoding="utf-8")
    fallback = exporter.export(paths, tmp_path / "fallback")
    assert "20:00:00" in fallback.markdown.read_text(encoding="utf-8")
    assert any("metadata" in warning for warning in fallback.warnings)
