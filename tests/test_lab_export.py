from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from csbox.core.events import TerminalEvent, TerminalEventType
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
    assert "### 2. ../../课程/实验二:*?" in markdown
    assert "![实验记录](evidence/02-课程-实验二.png)" in markdown
    assert "命令：" not in markdown
    assert result.commands is not None
    assert result.commands.read_text(encoding="utf-8") == "ip addr\n"
    assert "unknown" not in result.commands.read_text(encoding="utf-8").lower()
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
