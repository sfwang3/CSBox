# CSBox Usable Core Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 交付 CSBox 首个可用版本：真实跨平台 Shell 代理、session/Capture/replay/PNG/Markdown 闭环，以及可用的 check、build adapter、pack、真实配置和响应式中文 TUI。

**Architecture:** backend 只负责 PTY/ConPTY I/O，`TerminalEvent` 是实时、录制、仿真和回放的统一边界；lab 服务消费事件并用 asciicast v3、领域 snapshot 和原子 sidecar 实现恢复与导出。check、pack、config 保持独立 service/model，CLI/TUI 只做编排和展示。

**Tech Stack:** Python 3.11+, uv, Typer, Textual, Rich, Pydantic, pyte, Pillow, wcwidth, pywinpty（仅 Windows）, pytest, pytest-asyncio, Ruff。

## Global Constraints

- 默认简体中文，技术名称保留英文；正式 UI 不使用 Emoji。
- Windows PowerShell 5.1、PowerShell 7、WSL2/Linux Bash 是正式目标；Zsh 尽可能兼容。
- `pywinpty` 必须保留 `sys_platform == 'win32'` marker，Linux/WSL 导入与安装不得依赖它。
- backend 不依赖 Recorder、pyte、Pillow、Textual 或 Evidence；TUI 不直接 subprocess 或读取 session 文件。
- 所有终端宽度、截断和布局统一使用 display-cell/wcwidth 语义，禁止用 `len(text)` 计算显示宽度。
- 实验进行态优先 terminal fidelity，不画永久状态栏；除 Capture 键外原样转发输入。
- `.cast` 保持 asciicast v3 兼容，扩展 metadata/captures/checkpoints 使用独立文件。
- API 模块保持占位，本轮不实现 HTTP/API 测试能力。
- pack 永不删除、移动或原地改写项目文件，不跟随逃逸 root 的符号链接。
- Windows 本机未验证项必须由 Windows CI 证明或明确标记“仅实现，未本机验证”。
- 每个新增行为先写会按预期失败的测试并确认 RED，再写最小实现并确认 GREEN。
- 不读取、提交或输出真实 `.env`、Token、密码、私钥或字体文件；secret finding 不显示匹配值。

---

### Task 1: Foundation dependencies, display width, events, and configuration

**Files:**
- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Modify: `.gitignore`
- Create: `src/csbox/core/display_width.py`
- Create: `src/csbox/core/events.py`
- Create: `src/csbox/config/__init__.py`
- Create: `src/csbox/config/models.py`
- Create: `src/csbox/config/paths.py`
- Create: `src/csbox/config/loader.py`
- Test: `tests/test_display_width.py`
- Test: `tests/test_terminal_events.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Produces: `display_width(text) -> int`, `truncate_cells(text, width, ellipsis="") -> str`, `pad_cells(text, width) -> str`.
- Produces: `TerminalSize`, `TerminalEventType`, `TerminalEvent`, `TerminalEventClock.next(...)`.
- Produces: `CSBoxConfig`, `ConfigPaths`, `load_config(cwd, *, environ=None, overrides=None)`, `save_project_config(config, cwd)`.

- [ ] **Step 1: Write failing display-width and event tests**

Use the exact width corpus and verify typed event clocks:

```python
@pytest.mark.parametrize(
    ("text", "width"),
    [
        ("hello", 5),
        ("项目检查", 8),
        ("API 测试", 8),
        ("CSBox 项目检查", 14),
        ("abc中文123", 10),
        ("计算机网络实验", 14),
        (r"C:\Users\测试用户\桌面\实验一", 29),
        ("~/课程实验/计算机网络/实验一", 28),
        ("e\u0301", 1),
        ("Ａ", 2),
    ],
)
def test_display_width_uses_terminal_cells(text: str, width: int) -> None:
    assert display_width(text) == width


def test_event_clock_records_monotonic_relative_sequence() -> None:
    times = iter((100.0, 100.25))
    clock = TerminalEventClock(monotonic=lambda: next(times))
    event = clock.next(TerminalEventType.OUTPUT, b"ok")
    assert (event.sequence, event.monotonic_time, event.relative_time) == (1, 100.25, 0.25)
```

- [ ] **Step 2: Run RED**

Run: `uv run pytest tests/test_display_width.py tests/test_terminal_events.py -q`

Expected: import failure for `csbox.core.display_width` and `csbox.core.events`.

- [ ] **Step 3: Implement width helpers and event types**

The public shapes must be:

```python
class TerminalEventType(StrEnum):
    OUTPUT = "output"
    INPUT = "input"
    RESIZE = "resize"
    MARK = "mark"
    CAPTURE = "capture"
    EXIT = "exit"


@dataclass(frozen=True, slots=True)
class TerminalSize:
    columns: int
    rows: int


@dataclass(frozen=True, slots=True)
class TerminalEvent:
    sequence: int
    monotonic_time: float
    relative_time: float
    type: TerminalEventType
    payload: bytes | str | TerminalSize | int | None
```

`truncate_cells` must never split a base character from following combining marks or emit a partial double-width character. A negative width raises `ValueError`; width zero returns `""`; an ellipsis wider than the limit is itself safely truncated.

- [ ] **Step 4: Run GREEN for width/events**

Run: `uv run pytest tests/test_display_width.py tests/test_terminal_events.py -q`

Expected: all focused tests pass.

- [ ] **Step 5: Write failing configuration tests**

Cover defaults, user/project precedence, CLI overrides, bad TOML, invalid capture key/theme, Windows/XDG paths, unknown fields, and atomic project persistence. Use injected temporary paths and environment; never touch a real user configuration.

```python
def test_project_config_overrides_user_config(tmp_path: Path) -> None:
    paths = ConfigPaths(user=tmp_path / "user.toml", project=tmp_path / ".csbox/config.toml")
    paths.user.write_text('[lab]\nshell = "bash"\n', encoding="utf-8")
    paths.project.parent.mkdir()
    paths.project.write_text('[lab]\nshell = "zsh"\n', encoding="utf-8")
    assert load_config(tmp_path, paths=paths).lab.shell == "zsh"
```

- [ ] **Step 6: Run configuration RED**

Run: `uv run pytest tests/test_config.py -q`

Expected: import failure for `csbox.config`.

- [ ] **Step 7: Implement and validate configuration**

Use strict nested Pydantic models with these stable fields:

```python
class LabConfig(BaseModel):
    shell: Literal["powershell", "pwsh", "bash", "zsh"] | None = None
    capture_key: Literal["f12"] = "f12"


class RenderConfig(BaseModel):
    theme: Literal["dark", "light"] = "dark"
    font: str | None = None


class PackConfig(BaseModel):
    filename: str = "{id}-{name}-{course}.zip"


class CheckConfig(BaseModel):
    large_file_threshold_mb: int = Field(default=50, ge=1, le=4096)
```

Add `student.id/name`, `course.name`, and top-level `locale="zh_CN"`. Merge dictionaries recursively in default < user < project < overrides order. Wrap TOML/Pydantic failures in `ConfigurationError(path, message)` with Chinese `__str__`. Persist known schema with same-directory temporary file and `os.replace`.

- [ ] **Step 8: Move runtime dependencies and verify Task 1**

Move `pyte>=0.8,<1`, `Pillow>=10,<13`, and `wcwidth>=0.2.13,<1` into main dependencies; remove `future`/HTTPX; retain conditional pywinpty. Add `.superpowers/` and runtime `.csbox/` session/config outputs to `.gitignore` without excluding `.csbox/config.example.toml` if later added. Run:

```bash
uv lock
uv sync
uv run pytest tests/test_display_width.py tests/test_terminal_events.py tests/test_config.py tests/test_platform_dependency.py -q
uv run ruff check src/csbox/core src/csbox/config tests/test_display_width.py tests/test_terminal_events.py tests/test_config.py
```

Expected: focused tests and Ruff pass; Linux dependency graph contains no pywinpty installation.

- [ ] **Step 9: Commit**

```bash
git add pyproject.toml uv.lock .gitignore src/csbox/core/display_width.py src/csbox/core/events.py src/csbox/config tests/test_display_width.py tests/test_terminal_events.py tests/test_config.py
git commit -m "feat: add terminal foundations and configuration"
```

### Task 2: Shell strategy and real PTY/ConPTY backends

**Files:**
- Modify: `src/csbox/core/shell.py`
- Modify: `src/csbox/core/terminal.py`
- Create: `src/csbox/core/terminal_unix.py`
- Create: `src/csbox/core/terminal_windows.py`
- Modify: `tests/test_environment.py`
- Modify: `tests/test_terminal.py`
- Create: `tests/test_shell_selection.py`
- Create: `tests/test_terminal_unix_integration.py`
- Create: `tests/test_terminal_windows.py`

**Interfaces:**
- Consumes: `TerminalSize`, `CSBoxConfig.lab.shell`.
- Produces: `select_shell(requested, config_shell, system, environ, which) -> ShellProfile` and `detect_shell_version(profile) -> str | None`.
- Produces: concrete `UnixPTYBackend`, `WindowsConPTYBackend`, `create_terminal_backend(system=None)`.

- [ ] **Step 1: Write shell-selection and error tests**

Cover all aliases, explicit unavailable shell, auto order on Windows/Unix/WSL, current shell preference, and Chinese guidance. The unavailable pwsh assertion must include both `未找到 pwsh.exe` and `--shell powershell`.

- [ ] **Step 2: Run shell RED**

Run: `uv run pytest tests/test_shell_selection.py tests/test_environment.py -q`

Expected: missing `select_shell`/capability errors.

- [ ] **Step 3: Implement profile adapters and capability detection**

Expose four concrete profile classes or frozen instances named `PowerShell51Profile`, `PowerShell7Profile`, `BashProfile`, `ZshProfile`; preserve `ShellKind` compatibility. Use `shutil.which` injection and short-timeout version probes with argument arrays. `ShellUnavailableError.user_message` is Chinese and `.cause` retains the technical exception.

- [ ] **Step 4: Write Unix backend RED integration tests**

Mark with `@pytest.mark.pty` and skip on Windows. Exercise a real `bash --noprofile --norc` or `/bin/sh` through the backend:

```python
backend.spawn(["bash", "--noprofile", "--norc"], cwd=tmp_path, size=TerminalSize(80, 24))
backend.write(b"printf '\\033[31m红色中文\\033[0m\\n'\n")
output = read_until(backend, "红色中文".encode(), timeout=5)
assert b"\x1b[31m" in output
backend.resize(100, 30)
backend.write(b"stty size\nexit\n")
assert b"30 100" in read_until(backend, b"30 100", timeout=5)
```

Also test no-data returns `None`, Ctrl+C interrupts `sleep 30`, long output completes, EOF is `b""`, exit code is available, and `close()` is idempotent.

- [ ] **Step 5: Run Unix RED**

Run: `uv run pytest tests/test_terminal.py tests/test_terminal_unix_integration.py -q`

Expected: reserved backend behavior fails because the real backend is absent.

- [ ] **Step 6: Implement UnixPTYBackend and factory**

Use `pty.fork`, non-blocking master fd, `selectors.DefaultSelector`, `TIOCSWINSZ`, `os.write`, `waitpid(WNOHANG)`, and bounded cleanup. Public contract:

```python
class TerminalBackend(ABC):
    def spawn(self, command, *, cwd=None, env=None, size=TerminalSize(80, 24)) -> None: ...
    def read(self, max_bytes=65536, timeout=0.05) -> bytes | None: ...
    def write(self, data: bytes) -> int: ...
    def resize(self, columns: int, rows: int) -> None: ...
    def is_alive(self) -> bool: ...
    def wait(self, timeout: float = 0.0) -> int | None: ...
    @property
    def exit_code(self) -> int | None: ...
    def close(self) -> None: ...
```

Distinguish no-data from EOF, validate dimensions, close fds exactly once, and reap the child.

- [ ] **Step 7: Write mocked Windows backend RED tests**

Inject a fake `PtyProcess` factory and assert `spawn(... dimensions=(rows, cols), backend=0)`, UTF-8 read conversion, bytes write decoding, `setwinsize(rows, cols)`, EOF, exit status, close-drain, and missing-pywinpty error. Assert importing `csbox.core.terminal` on Linux never imports `winpty`.

- [ ] **Step 8: Implement WindowsConPTYBackend**

Delay `from winpty import PtyProcess` until construction/spawn. Explicitly request ConPTY backend `0`, keep read/write coordination thread-safe, treat high-level `read()` strings as UTF-8 bytes, drain the final frame before close when possible, and restore/close idempotently. No Windows module import may execute Unix-only imports.

- [ ] **Step 9: Verify and commit Task 2**

Run:

```bash
uv run pytest tests/test_environment.py tests/test_shell_selection.py tests/test_terminal.py tests/test_terminal_windows.py tests/test_terminal_unix_integration.py -q
uv run ruff check src/csbox/core tests/test_shell_selection.py tests/test_terminal_unix_integration.py tests/test_terminal_windows.py
git add src/csbox/core tests/test_environment.py tests/test_terminal.py tests/test_shell_selection.py tests/test_terminal_unix_integration.py tests/test_terminal_windows.py
git commit -m "feat: implement cross-platform terminal backends"
```

### Task 3: Session recording, screen model, and Capture persistence

**Files:**
- Create: `src/csbox/lab/models.py`
- Create: `src/csbox/lab/recorder.py`
- Create: `src/csbox/lab/screen.py`
- Create: `src/csbox/lab/captures.py`
- Create: `src/csbox/lab/dispatcher.py`
- Test: `tests/test_session_models.py`
- Test: `tests/test_recorder.py`
- Test: `tests/test_terminal_screen.py`
- Test: `tests/test_captures.py`

**Interfaces:**
- Consumes: `TerminalEvent` stream.
- Produces: `SessionMetadata`, `CaptureRecord`, `SessionPaths`, `AsciicastV3Recorder/Reader`.
- Produces: domain `TerminalCell`, `TerminalCursor`, `TerminalSnapshot`, `TerminalEmulator`.
- Produces: `CaptureStore` and `TerminalEventDispatcher`.

- [ ] **Step 1: Write failing model/recorder tests**

Assert initial `status="running"`, required metadata, stable JSON aliases, v3 header, `o/i/r/m/x` mapping, millisecond interval error diffusion, UTF-8 split across output events, queue shutdown, truncated last cast line recovery, and unknown event tolerance.

```python
header = json.loads(lines[0])
assert header["version"] == 3
assert header["term"] == {"cols": 80, "rows": 24, "type": "xterm-256color"}
assert json.loads(lines[-1])[1:] == ["x", "0"]
```

- [ ] **Step 2: Run recorder RED**

Run: `uv run pytest tests/test_session_models.py tests/test_recorder.py -q`

Expected: missing lab models/recorder imports.

- [ ] **Step 3: Implement metadata and asynchronous asciicast recorder**

Use a bounded `queue.Queue`, one writer thread, append+flush NDJSON, incremental UTF-8 decoder, explicit `RecorderError`, and `close()` that joins with a finite timeout. Header env contains only `SHELL` and `TERM` when present. Reader returns `CastReadResult(header, events, warnings)` and retains all valid lines around isolated corruption.

- [ ] **Step 4: Write failing screen tests**

Feed bytes for ASCII/CJK mix, ANSI 16/256/truecolor, cursor movement, erase line/screen, CR progress updates, reverse/bold/underline, PowerShell table, Chinese paths, resize, combining marks, and wide character at the last column. Assert callers only see CSBox dataclasses, never pyte types.

- [ ] **Step 5: Run screen RED and implement pyte adapter**

Run RED: `uv run pytest tests/test_terminal_screen.py -q`.

Then implement:

```python
@dataclass(frozen=True, slots=True)
class TerminalCell:
    character: str = " "
    width: int = 1
    foreground: str = "default"
    background: str = "default"
    bold: bool = False
    italic: bool = False
    underline: bool = False
    strikethrough: bool = False
    reverse: bool = False


@dataclass(frozen=True, slots=True)
class TerminalSnapshot:
    rows: int
    columns: int
    cells: tuple[tuple[TerminalCell, ...], ...]
    cursor: TerminalCursor
    relative_time: float = 0.0
```

`TerminalEmulator.apply` consumes only OUTPUT/RESIZE, and snapshot conversion centralizes any pyte workaround.

- [ ] **Step 6: Write failing CaptureStore/dispatcher tests**

Cover immutable snapshot capture, timestamp/rows/cols/cwd/optional command, atomic writes, `.bak` recovery, invalid individual capture warning, title edit, delete, sink ordering, and Recorder sink failure propagation.

- [ ] **Step 7: Implement CaptureStore and dispatcher**

`captures.json` schema is `{ "version": 1, "captures": [...] }`. Use UUID ids, ISO UTC creation time, relative timestamp sorting, same-directory temporary write + fsync + replace, and a recoverable backup. Dispatcher maintains registration order and reports sink errors without coupling to backend types.

- [ ] **Step 8: Verify and commit Task 3**

```bash
uv run pytest tests/test_session_models.py tests/test_recorder.py tests/test_terminal_screen.py tests/test_captures.py -q
uv run ruff check src/csbox/lab tests/test_session_models.py tests/test_recorder.py tests/test_terminal_screen.py tests/test_captures.py
git add src/csbox/lab tests/test_session_models.py tests/test_recorder.py tests/test_terminal_screen.py tests/test_captures.py
git commit -m "feat: add lab session recording and captures"
```

### Task 4: Replay checkpoints, PNG renderer, and lab export

**Files:**
- Create: `src/csbox/lab/replay.py`
- Create: `src/csbox/lab/fonts.py`
- Create: `src/csbox/lab/renderer.py`
- Create: `src/csbox/lab/exporter.py`
- Test: `tests/test_replay.py`
- Test: `tests/test_renderer.py`
- Test: `tests/test_lab_export.py`
- Create: `tests/fixtures/sessions/mixed.cast`

**Interfaces:**
- Consumes: cast reader, `TerminalEmulator`, `CaptureStore`, config render settings.
- Produces: `ReplayService.seek(relative_time) -> TerminalSnapshot`, `CheckpointStore`.
- Produces: `TerminalEvidenceRenderer.render(snapshot, destination, theme) -> Path`.
- Produces: `LabExporter.export(session, destination) -> LabExportResult`.

- [ ] **Step 1: Write replay RED tests**

Use the fixed mixed cast to assert deterministic snapshots at 0, between events, after resize, after exit, backward seeks, unknown events, empty session, and checkpoints at 5 seconds or 500 events. Instrument the emulator factory so a late seek proves it starts at the nearest checkpoint rather than event zero.

- [ ] **Step 2: Implement replay/checkpoints and run GREEN**

Run RED: `uv run pytest tests/test_replay.py -q`.

Implement `Checkpoint(event_index, cast_offset, relative_time, snapshot)` and a versioned atomic `checkpoints.json`; rebuild an invalid derived index without modifying `session.cast`. Restore emulator state from the domain snapshot inside the pyte adapter. Run the same test again; expected all pass.

- [ ] **Step 3: Write renderer RED tests**

Inject bundled test font paths from the operating system rather than committing fonts. Assert image dimensions, dark/light backgrounds, exact cell origins, CJK occupying two cells, combining glyph sharing origin, reverse colors, underline, missing-CJK-font Chinese error, and Chinese output path. Avoid full-image pixel goldens.

- [ ] **Step 4: Implement font discovery and cell renderer**

Provide deterministic `FontResolver(explicit=None, candidates=None)` and `RenderTheme.dark()/light()`. Discover Cascadia/Consolas plus Microsoft YaHei/SimSun on Windows, DejaVu/Noto CJK and `/mnt/c/Windows/Fonts` on Linux/WSL. Compute cell width from ASCII mono advance and half the CJK fallback advance; draw by column index, not string advance.

- [ ] **Step 5: Write export RED and implement exporter**

Assert safe Chinese/slash-containing titles, ordered `01-*.png`, copied cast, Chinese Markdown relative links, commands omitted when unknown, and no overwrite without force.

`evidence.md` entries use this stable shape:

```markdown
## 实验记录

### 1. 查看网络接口

时间：
14:23:41

结果：

![实验记录](evidence/01-ip-addr.png)
```

- [ ] **Step 6: Verify and commit Task 4**

```bash
uv run pytest tests/test_replay.py tests/test_renderer.py tests/test_lab_export.py -q
uv run ruff check src/csbox/lab tests/test_replay.py tests/test_renderer.py tests/test_lab_export.py
git add src/csbox/lab tests/test_replay.py tests/test_renderer.py tests/test_lab_export.py tests/fixtures/sessions/mixed.cast
git commit -m "feat: add terminal replay and evidence export"
```

### Task 5: Transparent lab proxy, session service, and CLI workflow

**Files:**
- Create: `src/csbox/lab/keymap.py`
- Create: `src/csbox/lab/proxy.py`
- Create: `src/csbox/lab/repository.py`
- Create: `src/csbox/lab/service.py`
- Modify: `src/csbox/cli/main.py`
- Modify: `src/csbox/locales/zh_CN.json`
- Test: `tests/test_capture_key.py`
- Test: `tests/test_lab_service.py`
- Test: `tests/test_lab_cli.py`
- Create: `tests/test_lab_proxy_integration.py`

**Interfaces:**
- Consumes: config, selected shell, backend, dispatcher, recorder, emulator, capture store.
- Produces: `CaptureKeyMatcher`, `TerminalProxy.run() -> int`, `SessionRepository`, `LabService`.
- Produces commands: `csbox lab start`, `csbox lab export`, `csbox lab list`.

- [ ] **Step 1: Write capture matcher RED tests**

Default F12 sequence is `b"\x1b[24~"`. Test full sequence, prefix split over every boundary, false prefix flushing, multiple captures in one chunk, ordinary controls unchanged, and `flush()` preserving an unfinished non-match. Complete F12 bytes must never appear in forwarded bytes.

- [ ] **Step 2: Implement key matcher and environment advisory**

Use a deterministic byte prefix state machine. `CaptureBindingProbe` reports configured key, TERM_PROGRAM/WT_SESSION environment hints, and whether the input adapter can recognize it; it does not mutate VS Code, Windows Terminal, Bash or PSReadLine configuration.

- [ ] **Step 3: Write service/proxy RED tests**

With a scripted backend and memory streams assert start creates initial metadata before spawn, INPUT/OUTPUT/RESIZE/CAPTURE/EXIT ordering, capture bytes filtered, stdout receives original output bytes, terminal state context exits on exceptions, metadata completed/interrupted/failed, and recorder data survives backend crash.

- [ ] **Step 4: Implement proxy/repository/service**

`TerminalProxy` takes input/output adapters so Unix raw termios and Windows console mode are isolated. Install SIGWINCH only on Unix, restore the previous handler and mode in `finally`, and avoid any status output while raw proxying. `SessionRepository.resolve` accepts exact id or unique prefix and returns a Chinese ambiguity/not-found error.

- [ ] **Step 5: Write CLI RED tests and add lab command group**

Use injected `LabService`/Typer runner for command parsing and errors. The CLI surface is:

```text
csbox lab start [NAME] [--shell powershell|pwsh|bash|zsh] [--verbose]
csbox lab export SESSION [--output PATH] [--theme dark|light] [--force]
csbox lab list [--json]
```

Default experiment name is a localized timestamp-based name. User errors show Chinese guidance; `--verbose` prints the underlying exception after the primary message. `lab review` is added with the real Review TUI in Task 6, so Task 5 exposes no incomplete review command.

- [ ] **Step 6: Run real Linux/WSL proxy smoke test**

Use a subprocess-owned pseudo-terminal so the test does not take over the developer terminal. Verify `echo`, Chinese `printf`, ANSI, Ctrl+C, resize, long output and clean `exit`, with per-case timeout. Run:

```bash
uv run pytest tests/test_capture_key.py tests/test_lab_service.py tests/test_lab_cli.py tests/test_lab_proxy_integration.py -q
uv run csbox lab list --json
```

- [ ] **Step 7: Verify and commit Task 5**

```bash
uv run ruff check src/csbox/cli src/csbox/lab tests/test_capture_key.py tests/test_lab_service.py tests/test_lab_cli.py tests/test_lab_proxy_integration.py
git add src/csbox/cli src/csbox/lab src/csbox/locales/zh_CN.json tests/test_capture_key.py tests/test_lab_service.py tests/test_lab_cli.py tests/test_lab_proxy_integration.py
git commit -m "feat: add transparent lab workflow"
```

### Task 6: Review TUI and real Home state

**Files:**
- Modify: `src/csbox/core/models.py`
- Modify: `src/csbox/cli/main.py`
- Create: `src/csbox/lab/home_data.py`
- Create: `src/csbox/tui/keymap.py`
- Create: `src/csbox/tui/screens/review.py`
- Create: `src/csbox/tui/widgets/review.py`
- Create: `src/csbox/tui/dialogs/capture_title.py`
- Modify: `src/csbox/tui/app.py`
- Modify: `src/csbox/tui/screens/home.py`
- Modify: `src/csbox/tui/widgets/home.py`
- Modify: `src/csbox/tui/themes/csbox.tcss`
- Modify: `src/csbox/locales/zh_CN.json`
- Modify: `tests/test_tui.py`
- Create: `tests/test_review_tui.py`

**Interfaces:**
- Consumes: `SessionRepository`, `ReplayService`, `CaptureStore`, domain snapshots/view models.
- Produces: `RealHomeDataSource`, `ReviewController`, `ReviewScreen`, shared `KEYMAP`, CLI `csbox lab review [SESSION]`.

- [ ] **Step 1: Load visual skills and write failing real-Home tests**

Before visual implementation, read and follow `ui-ux-pro-max` and `frontend-design`. Test that Home shows current platform/Shell/project, recent real session and Capture count; with no sessions it shows localized empty state and no `[DEMO]` data. API remains visibly unavailable.

- [ ] **Step 2: Implement real Home source without changing the established style**

Keep the charcoal/midnight Chinese system-tool direction, accessible focus, text+symbol status, no Emoji and no decorative charts. Replace `FakeHomeDataSource` in runtime composition; retain fake source only for isolated legacy tests if useful.

- [ ] **Step 3: Write ReviewController and TUI RED tests**

Test play/pause, timer advance, seek clamping, backward seek, Capture jump/create/delete/title edit, and progress formatting. With Textual `run_test`, assert:

```python
async with app.run_test(size=(80, 24)) as pilot:
    assert screen.is_wide is False
    await pilot.press("tab")
    assert screen.active_pane == "timeline"

async with app.run_test(size=(120, 30)):
    assert screen.is_wide is True
    assert screen.query_one("#review-terminal")
    assert screen.query_one("#review-sidebar")
```

- [ ] **Step 4: Implement ReviewScreen and shared keymap**

Wide layout: terminal left, timeline/captures stacked right. Narrow layout: exactly one content pane with Tab cycle. Footer shows Space, seek, C, E, Delete, Esc/Q and current time/progress using display-cell helpers. Render snapshots as Rich `Text` while mapping foreground/background/bold/underline/reverse; no pyte imports.

- [ ] **Step 5: Verify TUI at both breakpoints and commit**

```bash
uv run pytest tests/test_tui.py tests/test_review_tui.py -q
uv run ruff check src/csbox/tui src/csbox/lab/home_data.py tests/test_tui.py tests/test_review_tui.py
git add src/csbox/core/models.py src/csbox/cli/main.py src/csbox/lab/home_data.py src/csbox/tui src/csbox/locales/zh_CN.json tests/test_tui.py tests/test_review_tui.py
git commit -m "feat: add lab review and real home tui"
```

### Task 7: Project checks, build adapters, and Check TUI

**Files:**
- Create: `src/csbox/check/models.py`
- Create: `src/csbox/check/detectors.py`
- Create: `src/csbox/check/rules.py`
- Create: `src/csbox/check/build.py`
- Create: `src/csbox/check/service.py`
- Modify: `src/csbox/check/registry.py`
- Create: `src/csbox/tui/screens/project_check.py`
- Create: `src/csbox/tui/widgets/project_check.py`
- Modify: `src/csbox/tui/app.py`
- Modify: `src/csbox/tui/themes/csbox.tcss`
- Modify: `src/csbox/cli/main.py`
- Modify: `src/csbox/locales/zh_CN.json`
- Test: `tests/test_check_detectors.py`
- Test: `tests/test_check_rules.py`
- Test: `tests/test_build_adapters.py`
- Test: `tests/test_check_service.py`
- Test: `tests/test_check_cli.py`
- Test: `tests/test_check_tui.py`

**Interfaces:**
- Produces: `CheckStatus(PASS/WARN/FAIL/SKIP)`, `CheckFinding`, `CheckReport`, `DetectedProject`.
- Produces: Node/Maven/Gradle/Python detectors and build adapters.
- Produces: `CheckService.run(root, *, build=False) -> CheckReport` and CLI `csbox check`.

- [ ] **Step 1: Write detector/rule RED tests**

Create temporary fixture trees covering package.json, pom.xml, Gradle/KTS, pyproject/requirements; README; `.env`; PEM/OpenSSH key headers; node_modules/target/build/dist/.idea/__pycache__; logs; threshold-sized files; clean/dirty/untracked Git; Windows/Unix local absolute paths; and hard-coded secret assignments. Assert findings never contain the actual secret string.

- [ ] **Step 2: Implement scanner, detectors, rules, and run GREEN**

Use one bounded file inventory shared by rules. Skip `.git`, binary files and text above scan limit; artifact-existence rules inspect paths without recursively scanning them. Git commands use argument arrays with timeout. Severity mapping is explicit and every rule returns PASS/WARN/FAIL/SKIP even when not applicable.

- [ ] **Step 3: Write build adapter RED tests**

Inject a command runner and assert exact commands, wrapper priority, Windows suffixes, cwd, timeout, bounded output and localized missing-tool/timeout results. Node only runs an existing `scripts.build`; Maven/Gradle use wrapper then global; Python pyproject uses `uv build`, while requirements-only projects return SKIP because they have no reliable package build target.

- [ ] **Step 4: Implement build adapters and service**

Default `CheckService.run(..., build=False)` must not invoke the runner. With build enabled, select every detected project adapter in deterministic order and append build outcomes without throwing raw subprocess errors.

- [ ] **Step 5: Write CLI/TUI RED and implement presentation**

CLI supports `csbox check [PATH] [--build] [--plain] [--json]`; `--plain` emits no ANSI and `--json` is valid stable JSON. Default opens `ProjectCheckScreen`, which groups status and details, supports 80×24 and >=120, and receives a report rather than reading files or running subprocess itself.

- [ ] **Step 6: Verify and commit Task 7**

```bash
uv run pytest tests/test_check_detectors.py tests/test_check_rules.py tests/test_build_adapters.py tests/test_check_service.py tests/test_check_cli.py tests/test_check_tui.py -q
uv run ruff check src/csbox/check src/csbox/tui/screens/project_check.py src/csbox/tui/widgets/project_check.py tests/test_check_*.py tests/test_build_adapters.py
git add src/csbox/check src/csbox/tui src/csbox/cli/main.py src/csbox/locales/zh_CN.json tests/test_check_*.py tests/test_build_adapters.py
git commit -m "feat: add project checks and build adapters"
```

### Task 8: Safe staging and ZIP packaging

**Files:**
- Create: `src/csbox/pack/models.py`
- Create: `src/csbox/pack/filters.py`
- Create: `src/csbox/pack/service.py`
- Modify: `src/csbox/pack/registry.py`
- Modify: `src/csbox/cli/main.py`
- Modify: `src/csbox/locales/zh_CN.json`
- Test: `tests/test_pack_filters.py`
- Test: `tests/test_pack_service.py`
- Test: `tests/test_pack_cli.py`

**Interfaces:**
- Consumes: config naming/student/course, `CheckService`.
- Produces: `PackFilter`, `PackRequest`, `PackReport`, `PackService.pack(...)`, CLI `csbox pack`.

- [ ] **Step 1: Write pack-filter RED tests**

Cover every default exclusion, nested caches, `.env.example` inclusion, real `.env` and private key refusal, source files named `build.py`/`target.py` inclusion, user include/exclude patterns, symlink escape skip, output ZIP self-exclusion, Windows/Unicode paths, and path traversal rejection.

- [ ] **Step 2: Implement path-safe filter and inventory**

Match directory names only where rules are directory rules and suffixes only where file rules are file rules. Resolve every candidate relative to root without following symlinks. Produce an exclusion summary by reason, but never delete source paths.

- [ ] **Step 3: Write PackService/CLI RED tests**

Assert check runs first, staging is under `TemporaryDirectory`, source hashes/timestamps remain unchanged, ZIP paths use `/`, archive opens with `testzip() is None`, stats are correct, existing destination refuses overwrite, filename template is sanitized, and failure removes only the temporary staging—not source files.

- [ ] **Step 4: Implement staging, verify, ZIP, and reporting**

Copy with `shutil.copy2`, create ZIP through a temporary sibling then atomically replace only a non-existing or explicitly forced destination. `--verify` re-opens ZIP and confirms expected entries. CLI is `csbox pack [PATH] [--output PATH] [--verify] [--force] [--json]` and prints source size, archive size, file count and exclusions in Chinese.

- [ ] **Step 5: Verify and commit Task 8**

```bash
uv run pytest tests/test_pack_filters.py tests/test_pack_service.py tests/test_pack_cli.py -q
uv run ruff check src/csbox/pack tests/test_pack_filters.py tests/test_pack_service.py tests/test_pack_cli.py
git add src/csbox/pack src/csbox/cli/main.py src/csbox/locales/zh_CN.json tests/test_pack_filters.py tests/test_pack_service.py tests/test_pack_cli.py
git commit -m "feat: add safe project packaging"
```

### Task 9: Cross-platform CI, documentation, regression review, and release gate

**Files:**
- Modify: `pyproject.toml`
- Create: `.github/workflows/ci.yml`
- Modify: `README.md`
- Modify: `docs/TUI_DESIGN.md`
- Create: `docs/ARCHITECTURE.md`
- Create: `docs/TERMINAL_BACKENDS.md`
- Create: `docs/SESSION_FORMAT.md`
- Create: `tests/conftest.py`
- Create: `tests/test_windows_pty_integration.py`

**Interfaces:**
- Consumes all prior tasks.
- Produces reproducible Ubuntu/Windows CI and implementation-accurate documentation.

- [ ] **Step 1: Add test markers and CI RED/static validation**

Register `pty`, `windows`, and `integration` markers. Create an Ubuntu/Windows matrix for Python 3.11 and 3.14 using pinned official checkout/setup-uv actions. Every cell runs `uv sync --locked`, `uv run pytest`, `uv run ruff check .`, and `uv run ruff format --check .`.

Windows adds real commands for both shells:

```yaml
- name: Windows PowerShell 5.1 smoke
  shell: powershell
  run: uv run pytest -m "windows and integration" --shell-kind powershell
- name: PowerShell 7 smoke
  shell: pwsh
  run: uv run pytest -m "windows and integration" --shell-kind pwsh
```

The pytest option/fixture selects `powershell.exe` or `pwsh.exe`, launches through `WindowsConPTYBackend`, writes a Unicode command, resizes, exits and asserts status. Skip means a reported CI limitation, not a pass claim.

- [ ] **Step 2: Update implementation-accurate docs**

README begins with “面向计算机专业学生的实验记录与课程项目交付工具”, documents lab/check/pack commands and explicitly leaves API unavailable. Architecture docs cover dependency direction, event flow, backend platform details, F12 host-terminal caveat, session/capture/recovery schema, replay checkpoints, CJK policy, font setup and locally unverified Windows status.

- [ ] **Step 3: Request broad code review and fix findings**

Generate a review package from baseline `def147b8b36fa78a3254b88717c42263cbda48be` to HEAD. The reviewer checks the complete user requirements, security/privacy, terminal cleanup, crash recovery, CJK, Windows isolation, pack safety, TUI breakpoints and test validity. Fix every Critical/Important finding with focused regression tests and re-review.

- [ ] **Step 4: Run the complete fresh release gate**

```bash
uv sync
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run csbox --help
uv run csbox doctor
uv run csbox lab --help
uv run csbox check --plain
uv run csbox pack --help
```

Run the current-platform real lab smoke in a disposable temporary directory and export at least one CJK snapshot. Inspect the PNG dimensions/content and archive listing without committing outputs.

- [ ] **Step 5: Audit Git and commit docs/CI/final fixes**

Inspect `git diff --check`, `git status --short`, `git diff --stat`, tracked files for credential-like content, commit list, and ensure no `.csbox` session, ZIP, PNG, font, `.env`, cache or review package is staged. Then:

```bash
git add .github README.md docs pyproject.toml tests src
git commit -m "test: expand cross-platform coverage"
```

If docs and tests form clearly separate validated changes, use `docs: document terminal architecture` for the documentation commit instead.

- [ ] **Step 6: Push authorized main and verify remote**

The user explicitly authorized `push origin main` after all gates pass. Run `git push origin main`, then compare `git rev-parse HEAD` with `git rev-parse origin/main` and confirm `git status --short --branch` is clean. Never force push or rewrite history.
