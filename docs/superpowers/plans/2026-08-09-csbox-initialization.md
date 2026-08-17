# CSBox Project Initialization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 建立一个 Python 3.11+、由 `uv` 管理、可运行中文 Typer/Textual CLI/TUI 的 CSBox V0.1 工程基线，并以 Protocol/ABC、Registry 和 Adapter 留出长期扩展边界。

**Architecture:** `core` 只依赖标准库和 Pydantic，承载领域模型、公共协议、轻量 Registry、Shell/环境检测与 TerminalBackend ABC。`lab`、`api`、`check`、`pack` 是独立业务边界；CLI 是组合根，TUI 通过注入 `HomeDataSource` 消费快照，不直接调用业务实现。平台能力和未来业务实现通过显式注册的 Adapter 选择，不扫描第三方插件。

**Tech Stack:** Python 3.11+, uv, Typer, Textual, Rich, Pydantic, pytest, pytest-asyncio, Ruff, Hatchling。

## Global Constraints

- CSBox 默认语言为简体中文；技术名词 Git、API、HTTP、JSON、OpenAPI、Docker、Maven、Gradle、npm、PowerShell、WSL、Shell 保留英文。
- TUI 不使用 Emoji；状态同时显示符号和文字，不能只靠颜色。
- `≥120` 列使用双栏，`80–119` 列使用紧凑单栏，80×24 必须可用。
- 本轮不实现真实实验录制、PTY/ConPTY、截图、API 测试、项目检查业务、项目打包业务或发布 PyPI。
- `pywinpty` 必须使用 `sys_platform == "win32"` 条件依赖；Linux/WSL 默认安装不得要求它。
- Fake Data 必须在数据和界面文案中明确标记为 `[DEMO]`/原型数据。
- 所有业务可见文案从 `src/csbox/locales/zh_CN.json` 加载，不能散落在 TUI/CLI 中。
- 不新增 LICENSE；不提交 `.venv`、缓存、临时文件、真实凭据；不 force push。
- 每个新增行为先写一个会按预期失败的测试，确认 RED 后再写最小实现并确认 GREEN。

---

### Task 1: Project metadata, repository hygiene, reference asset, and docs

**Files:**
- Create: `pyproject.toml`
- Create: `.gitignore`
- Create: `README.md`
- Create: `docs/TUI_DESIGN.md`
- Create: `docs/reference/csbox-home-concept.png` (copy of the supplied PNG)
- Modify: move the supplied root PNG into `docs/reference/csbox-home-concept.png`

**Interfaces:**
- Produces the `csbox` console script: `csbox.cli:main`.
- Produces project dependencies and the `dev` dependency group consumed by later tasks.

- [ ] **Step 1: Define project metadata and dependency markers**

Write `pyproject.toml` with these exact project properties:

```toml
[build-system]
requires = ["hatchling>=1.25"]
build-backend = "hatchling.build"

[project]
name = "csbox"
version = "0.1.0"
description = "计算机实验与项目交付工具"
readme = "README.md"
requires-python = ">=3.11"
dependencies = [
  "pydantic>=2.7,<3",
  "rich>=13.7,<15",
  "textual>=0.58,<1",
  "typer>=0.12,<1",
  "pywinpty>=2.0; sys_platform == 'win32'",
]

[project.optional-dependencies]
future = [
  "pyte>=0.8",
  "Pillow>=10",
  "wcwidth>=0.2",
  "httpx>=0.27",
]

[project.scripts]
csbox = "csbox.cli:main"

[dependency-groups]
dev = [
  "pytest>=8.0",
  "pytest-asyncio>=0.23",
  "ruff>=0.6",
]

[tool.uv]
default-groups = ["dev"]

[tool.hatch.build.targets.wheel]
packages = ["src/csbox"]

[tool.ruff]
target-version = "py311"
line-length = 100
src = ["src", "tests"]

[tool.ruff.lint]
select = ["E", "F", "I", "UP", "B", "SIM"]

[tool.ruff.format]
quote-style = "double"
indent-style = "space"

[tool.pytest.ini_options]
testpaths = ["tests"]
addopts = "-ra"
```

- [ ] **Step 2: Add repository hygiene rules**

Create `.gitignore` covering `.venv/`, `__pycache__/`, `*.py[cod]`, `.pytest_cache/`, `.ruff_cache/`, `.mypy_cache/`, `dist/`, `build/`, `*.egg-info/`, `.env`, `.env.*` except `.env.example`, local IDE files, OS metadata, and `*.Zone.Identifier`.

- [ ] **Step 3: Preserve the supplied concept image**

Create `docs/reference/`, move the supplied `be60e41c-2182-4c8a-8904-790842de3f72.png` to `docs/reference/csbox-home-concept.png`, and leave the unrelated Zone.Identifier metadata ignored. Do not copy any data values from the image into Fake Data.

- [ ] **Step 4: Write the user-facing README and TUI design principles**

`README.md` must cover what CSBox is, early-development status, the four core capabilities (实验记录、API 测试证据、项目检查、干净打包), what V0.1 actually implements, `uv sync`, `uv run csbox`, `uv run csbox doctor`, and the Windows PowerShell 5.1 / PowerShell 7 / WSL compatibility targets. `docs/TUI_DESIGN.md` must record Chinese-first copy, retained English technical terms, future non-full-screen experiment mode, full-screen management/review mode, CJK double-width support, 80×24, unified shortcuts, no Emoji, business/TUI separation, and Fake Data before real PTY.

- [ ] **Step 5: Validate metadata without implementation code**

Run `uv lock --offline` if the cache permits; otherwise run `uv lock` with the configured network. Expected: a `uv.lock` is created, the conditional `pywinpty` dependency is represented with a Windows marker, and no source import is required yet. Do not claim the environment is ready until the later `uv sync` verification.

### Task 2: Core models, protocols, and explicit registries

**Files:**
- Create: `src/csbox/__init__.py`
- Create: `src/csbox/core/__init__.py`
- Create: `src/csbox/core/models.py`
- Create: `src/csbox/core/protocols.py`
- Create: `src/csbox/core/registry.py`
- Create: `src/csbox/api/__init__.py`
- Create: `src/csbox/api/registry.py`
- Create: `src/csbox/check/__init__.py`
- Create: `src/csbox/check/registry.py`
- Create: `src/csbox/pack/__init__.py`
- Create: `src/csbox/pack/registry.py`
- Create: `tests/test_core_registry.py`
- Create: `tests/test_core_protocols.py`

**Interfaces:**
- `Registry[T]` provides `register`, `get`, `has`, and `items`.
- `core.models` provides Pydantic `EnvironmentSnapshot`, `RecentExperiment`, `HomeSnapshot`, `Evidence`, `RenderedEvidence`, `CheckContext`, `CheckResult`, `ProjectInfo`, `BuildResult`, and `ExportResult`.
- `core.protocols` provides `EvidenceProvider`, `EvidenceRenderer`, `CheckRule`, `ProjectDetector`, `BuildAdapter`, and `Exporter` Protocols.
- `api`, `check`, and `pack` expose empty typed registries without importing TUI or CLI.

- [ ] **Step 1: Write the failing Registry tests**

Test that a new `Registry` is empty, a valid key can be registered and retrieved, duplicate registration raises `DuplicateRegistrationError`, blank keys raise `ValueError`, unknown keys raise `UnknownRegistrationError`, and `items()` returns an immutable tuple of key/value pairs.

- [ ] **Step 2: Run the focused tests and confirm RED**

Run `uv run pytest tests/test_core_registry.py -q`. Expected: collection fails because `csbox.core.registry` and `Registry` do not yet exist. Fix only test/import typos if necessary; do not implement production code before this RED result.

- [ ] **Step 3: Implement the minimal Registry**

Implement `Registry[T]` with an internal `dict[str, T]`, explicit `RegistryError` subclasses, whitespace validation, duplicate detection, and tuple snapshots. Do not add decorators, entry point discovery, or lazy loading.

- [ ] **Step 4: Run the focused tests and confirm GREEN**

Run `uv run pytest tests/test_core_registry.py -q`; expected: all Registry tests pass.

- [ ] **Step 5: Write failing model/protocol tests**

Test that valid Pydantic snapshots validate, invalid terminal dimensions are rejected, every named Protocol can be imported from `csbox.core.protocols`, and each business registry is an instance of `Registry` with no registered implementations.

- [ ] **Step 6: Run the model/protocol tests and confirm RED**

Run `uv run pytest tests/test_core_protocols.py -q`; expected: imports fail because models, protocols, and registries are absent.

- [ ] **Step 7: Implement models, Protocols, and business registries**

Use Pydantic v2 models with strict positive terminal dimensions and these minimal method shapes:

```python
class EvidenceProvider(Protocol):
    provider_id: ClassVar[str]

    def collect(self, source: Path) -> Evidence: ...


class EvidenceRenderer(Protocol):
    renderer_id: ClassVar[str]

    def render(self, evidence: Evidence) -> RenderedEvidence: ...


class CheckRule(Protocol):
    rule_id: ClassVar[str]

    def evaluate(self, context: CheckContext) -> CheckResult: ...


class ProjectDetector(Protocol):
    detector_id: ClassVar[str]

    def detect(self, root: Path) -> ProjectInfo | None: ...


class BuildAdapter(Protocol):
    adapter_id: ClassVar[str]

    def build(self, project: ProjectInfo, output_dir: Path) -> BuildResult: ...


class Exporter(Protocol):
    format_id: ClassVar[str]

    def export(self, result: object, destination: Path) -> ExportResult: ...
```

Create `EVIDENCE_PROVIDERS`, `EVIDENCE_RENDERERS`, `CHECK_RULES`, `PROJECT_DETECTORS`, `BUILD_ADAPTERS`, and `EXPORTERS` as explicit empty registries. Keep `core` free of Textual/Typer imports.

- [ ] **Step 8: Run focused tests and Ruff**

Run `uv run pytest tests/test_core_registry.py tests/test_core_protocols.py -q` and `uv run ruff check src/csbox/core src/csbox/api src/csbox/check src/csbox/pack tests/test_core_registry.py tests/test_core_protocols.py`; expected: tests and lint pass.

### Task 3: Shell profiles, environment detection, and TerminalBackend adapters

**Files:**
- Create: `src/csbox/core/shell.py`
- Create: `src/csbox/core/environment.py`
- Create: `src/csbox/core/terminal.py`
- Create: `tests/test_environment.py`
- Create: `tests/test_terminal.py`
- Modify: `src/csbox/core/__init__.py`

**Interfaces:**
- `ShellKind`, `ShellProfile`, `supported_shell_profiles()`, and `detect_shell(...)` represent PowerShell 5.1, PowerShell 7, Bash, Zsh, and unknown shells.
- `detect_environment(...)` returns `EnvironmentSnapshot` with OS, Python, current Shell, command availability, WSL flag, and terminal dimensions.
- `TerminalBackend` ABC defines `spawn`, `read`, `write`, `resize`, `is_alive`, `close`; `WindowsConPTYBackend` and `UnixPTYBackend` are explicit reserved adapters that raise `NotImplementedError` on operations.

- [ ] **Step 1: Write failing shell/environment tests**

Cover: `CSBOX_SHELL=pwsh.exe` maps to PowerShell 7; `CSBOX_SHELL=powershell.exe` maps to Windows PowerShell 5.1; `SHELL=/bin/zsh` maps to Zsh; `SHELL=/bin/bash` plus `WSL_DISTRO_NAME` maps to Bash; `WSL_INTEROP` or a `microsoft-standard` release marks WSL; injected `which` results control PowerShell availability; injected terminal size returns columns/rows; and the default fallback is 80×24.

- [ ] **Step 2: Run tests and confirm RED**

Run `uv run pytest tests/test_environment.py -q`; expected: imports fail because the detection modules do not exist.

- [ ] **Step 3: Implement Shell profiles and pure detection**

Use injected environment/system/release/`which`/terminal-size callables so tests never depend on the host shell. Prefer explicit `CSBOX_SHELL`, then executable names from `SHELL`/`ComSpec`, then PowerShell environment hints; never spawn a process. Use `shutil.which("powershell.exe")` and `shutil.which("pwsh.exe")` for doctor availability.

- [ ] **Step 4: Run tests and confirm GREEN**

Run `uv run pytest tests/test_environment.py -q`; expected: all detection tests pass.

- [ ] **Step 5: Write failing TerminalBackend tests**

Test that `TerminalBackend` is abstract, both reserved adapters can be imported on Linux, and each operation raises a clear `NotImplementedError` without importing `pywinpty`.

- [ ] **Step 6: Run tests and confirm RED**

Run `uv run pytest tests/test_terminal.py -q`; expected: the import/abstract behavior fails because the terminal module is absent.

- [ ] **Step 7: Implement the ABC and reserved adapters**

Define methods with `Sequence[str]`, optional `Path`/environment mapping for `spawn`, `bytes` from `read`, `int` from `write`, and integer columns/rows for `resize`. Put the raising implementation in a private reserved base so the two platform classes remain concrete placeholders. Do not add a `pywinpty` import.

- [ ] **Step 8: Run focused tests and Ruff**

Run `uv run pytest tests/test_environment.py tests/test_terminal.py -q` and `uv run ruff check src/csbox/core tests/test_environment.py tests/test_terminal.py`; expected: pass.

### Task 4: i18n resource layer and Fake Home Data source

**Files:**
- Create: `src/csbox/locales/__init__.py`
- Create: `src/csbox/locales/zh_CN.json`
- Create: `src/csbox/lab/__init__.py`
- Create: `src/csbox/lab/ports.py`
- Create: `src/csbox/lab/fake_data.py`
- Create: `tests/test_i18n.py`
- Create: `tests/test_fake_data.py`

**Interfaces:**
- `Translator` and `load_locale("zh_CN")` load package resources with `importlib.resources` and raise `MissingLocaleKey` for missing keys.
- `HomeDataSource` Protocol exposes `get_home_snapshot(environment: EnvironmentSnapshot) -> HomeSnapshot`.
- `FakeHomeDataSource` returns stable, explicitly demo-marked Pydantic data and does not pretend to record experiments.

- [ ] **Step 1: Write failing i18n tests**

Test that `load_locale("zh_CN")("brand.name") == "CSBox"`, the subtitle is Chinese, format placeholders work, and a missing key raises `MissingLocaleKey`.

- [ ] **Step 2: Run tests and confirm RED**

Run `uv run pytest tests/test_i18n.py -q`; expected: imports/resources are missing.

- [ ] **Step 3: Implement the JSON resource loader**

Load `zh_CN.json` through `importlib.resources.files("csbox.locales")`, parse UTF-8 JSON, and provide a callable translator that formats only known placeholders. Include labels for brand, environment fields, five entrances, demo status, modal, shortcuts, and doctor output; no Emoji.

- [ ] **Step 4: Run tests and confirm GREEN**

Run `uv run pytest tests/test_i18n.py -q`; expected: all resource tests pass.

- [ ] **Step 5: Write failing Fake Data tests**

Test that `FakeHomeDataSource().get_home_snapshot(environment)` preserves the injected environment, returns at least three recent experiments, and every item has `demo is True`.

- [ ] **Step 6: Run tests and confirm RED**

Run `uv run pytest tests/test_fake_data.py -q`; expected: the source and models are absent.

- [ ] **Step 7: Implement the source and lab registry**

Add a `HOME_DATA_SOURCES = Registry[HomeDataSource]("home-data-source")` and explicitly register the fake source under `"fake"` in `lab/fake_data.py`. Use neutral demo names and no copied dates, counts, or statuses from the reference image.

- [ ] **Step 8: Run focused tests and Ruff**

Run `uv run pytest tests/test_i18n.py tests/test_fake_data.py -q` and `uv run ruff check src/csbox/locales src/csbox/lab tests/test_i18n.py tests/test_fake_data.py`; expected: pass.

### Task 5: Typer CLI and doctor command

**Files:**
- Create: `src/csbox/cli/__init__.py`
- Create: `src/csbox/cli/main.py`
- Create: `tests/test_cli.py`

**Interfaces:**
- `app` is the Typer command object used by tests and the console script.
- `main()` invokes `app()` for `uv run csbox`.
- No arguments call the TUI composition root; `doctor` prints Chinese environment information and exits zero.

- [ ] **Step 1: Write failing CLI tests**

Use `typer.testing.CliRunner` to assert `app` with `--help` exits 0 and names `doctor`, and `app doctor` exits 0 while output contains Chinese labels for OS, Python, Shell, PowerShell 5.1, PowerShell 7, WSL, and terminal size. Do not assert host-specific true/false values.

- [ ] **Step 2: Run tests and confirm RED**

Run `uv run pytest tests/test_cli.py -q`; expected: import/command failures because the CLI module is absent.

- [ ] **Step 3: Implement the command object and doctor output**

Create a Typer app with `invoke_without_command=True`; its callback launches the TUI only when `ctx.invoked_subcommand is None`. `doctor` calls `detect_environment()` and prints a Rich `Table` or line-based report with `markup=False`, using the translator for every visible label. Keep TUI imports inside the no-command branch so `doctor` stays independent of Textual startup.

- [ ] **Step 4: Run focused tests and Ruff**

Run `uv run pytest tests/test_cli.py -q` and `uv run ruff check src/csbox/cli tests/test_cli.py`; expected: pass.

### Task 6: Textual HomeScreen, widgets, modal, and theme

**Files:**
- Create: `src/csbox/tui/__init__.py`
- Create: `src/csbox/tui/app.py`
- Create: `src/csbox/tui/screens/__init__.py`
- Create: `src/csbox/tui/screens/home.py`
- Create: `src/csbox/tui/widgets/__init__.py`
- Create: `src/csbox/tui/widgets/home.py`
- Create: `src/csbox/tui/dialogs/__init__.py`
- Create: `src/csbox/tui/dialogs/unavailable.py`
- Create: `src/csbox/tui/themes/__init__.py`
- Create: `src/csbox/tui/themes/csbox.tcss`
- Create: `tests/test_tui.py`

**Interfaces:**
- `CSBoxApp(data_source: HomeDataSource, environment: EnvironmentSnapshot, locale: Translator)` owns composition and actions.
- `HomeScreen(snapshot: HomeSnapshot, locale: Translator)` renders only injected data and raises no business calls.
- `UnavailableDialog(title: str, locale: Translator)` provides a localized message and an Escape/close button.

- [ ] **Step 1: Write failing TUI smoke tests**

Use Textual `run_test(size=(80, 24))` with `FakeHomeDataSource`, a deterministic `EnvironmentSnapshot`, and `load_locale()`. Assert the mounted screen is `HomeScreen`, the brand/subtitle are present, exactly five main entry buttons are present, the recent panel contains `[DEMO]`, and pressing the focused entry opens a dialog containing the localized unavailable message. Add a second run at `(120, 30)` and assert the wide layout class/state is available without exceptions.

- [ ] **Step 2: Run tests and confirm RED**

Run `uv run pytest tests/test_tui.py -q`; expected: imports fail because the TUI modules do not exist.

- [ ] **Step 3: Implement the composition root**

Define `CSBoxApp` with `q`, `f1`, and `f5` bindings, no user-facing hardcoded labels, and a `compose()` that yields `HomeScreen` with the injected `HomeSnapshot`. `f1` opens a localized help/unavailable dialog; `f5` refreshes the snapshot by calling the injected data source; `q` exits. Keep the default concrete Fake source construction in the CLI composition path, not in `HomeScreen`.

- [ ] **Step 4: Implement HomeScreen and focused widgets**

Create small widget classes for brand, environment summary, action list, recent demo list, and shortcut bar. Use localized keys for all labels. Five actions use `Button` controls with stable IDs; `on_button_pressed` maps IDs to localized titles and opens `UnavailableDialog`. Render status as symbol plus translated text and include an explicit demo note.

- [ ] **Step 5: Implement the unavailable Modal**

Use a `ModalScreen` with a visible title, message “该功能将在后续版本实现。” from i18n, a localized close button, and an Escape binding. Ensure focus moves to the close button when mounted and returns through Textual’s normal screen dismissal path.

- [ ] **Step 6: Implement the Textual CSS theme**

Use semantic tokens for midnight background, raised surface, primary text, muted text, blue-violet brand, cyan action, amber warning, and visible focus border. Use compact vertical rhythm so `(80, 24)` remains usable. At `min-width: 120`, set the action/recent region to horizontal two-column layout; below it, use a single vertical flow. Do not use Emoji, decorative charts, or meaningless stat cards.

- [ ] **Step 7: Run focused TUI tests and Ruff**

Run `uv run pytest tests/test_tui.py -q`, `uv run ruff check src/csbox/tui tests/test_tui.py`, and `uv run ruff format --check src/csbox/tui tests/test_tui.py`; expected: pass with no warnings.

### Task 7: Full verification and cross-platform dependency guard

**Files:**
- Create: `tests/test_platform_dependency.py`
- Modify: any implementation files required by fresh verification only

**Interfaces:**
- The test suite proves the conditional marker exists and importing core terminal abstractions on Linux does not require `pywinpty`.

- [ ] **Step 1: Write the dependency guard test**

Parse `pyproject.toml` with `tomllib`, assert a dependency contains both `pywinpty` and `sys_platform == 'win32'`, and import `csbox.core.terminal` in the test process. Do not install or fake a `pywinpty` module.

- [ ] **Step 2: Run the guard test and confirm RED**

Run `uv run pytest tests/test_platform_dependency.py -q`; expected: it fails until the metadata and terminal module are present. If the test already passes because earlier tasks supplied both, record that the test still validates the intended behavior and continue.

- [ ] **Step 3: Run the guard test GREEN and inspect installed tree**

Run `uv run pytest tests/test_platform_dependency.py -q`, `uv sync`, and `uv tree --depth 1`. On Linux/WSL, the tree must not contain `pywinpty`; the default environment must install the core and dev dependencies successfully.

- [ ] **Step 4: Run the complete verification set**

Run, in order:

```bash
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
uv run csbox --help
uv run csbox doctor
```

Expected: all tests pass, both Ruff commands exit 0, help names `doctor`, doctor prints all requested Chinese checks, and no command reads or prints credentials.

### Task 8: Git initialization, commit, and safe push

**Files:**
- Create: `.git/` through `git init` if absent
- Modify: Git index only

**Interfaces:**
- `origin` must equal `https://github.com/sfwang3/CSBox.git`.
- Local branch must be `main`.
- Commit message must be exactly `chore: initialize CSBox project`.

- [ ] **Step 1: Inspect final files and Git status**

Run `git status --short --branch` (initializing first if needed), `git remote -v`, `git diff --stat`, and `git diff --check`. Confirm no `.venv`, cache, `.env`, Zone.Identifier, or other credential-like files are staged.

- [ ] **Step 2: Initialize `main` and configure `origin`**

Run `git init -b main` when `.git` is absent. Add or update only `origin` to `https://github.com/sfwang3/CSBox.git`; do not add a second remote and do not force any history operation.

- [ ] **Step 3: Stage and inspect the exact index**

Run `git add .`, then `git status --short` and `git diff --cached --check`. If any prohibited file appears, unstage it explicitly and fix `.gitignore` before continuing.

- [ ] **Step 4: Commit the project baseline**

Run `git commit -m "chore: initialize CSBox project"`. Record the resulting full commit hash with `git rev-parse HEAD`.

- [ ] **Step 5: Push safely to `origin/main`**

Run `git push -u origin main` without `--force`. Verify with `git status --short --branch` and `git ls-remote --heads origin main`. If authentication, protection, or unexpected remote history blocks the push, do not rewrite or force; report the exact blocker and keep the commit hash.
