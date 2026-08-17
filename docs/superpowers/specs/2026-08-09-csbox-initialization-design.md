# CSBox 初始化设计

日期：2026-08-09

## 目标

建立一个可以长期演进的 Python 3.11+ CLI/TUI 工程基线：使用 `uv` 管理环境，提供 Typer CLI、Textual/Rich TUI、Pydantic 领域模型、中文 i18n 资源、跨平台终端接口和最小环境检测。首版只实现可运行的 Fake Data TUI Prototype，不实现真实实验录制、PTY/ConPTY、截图、API 测试、项目检查规则或项目打包业务。

## 架构原则

CSBox 使用模块化分层架构，并以 Protocol/ABC 和轻量 Registry/Adapter 作为稳定扩展边界。CLI、TUI、领域模型、业务模块和平台适配之间通过明确接口通信，避免把未来能力直接耦合到 Textual App。

`core` 是最稳定的内核层，只放领域模型、公共协议、Registry 和环境/终端抽象，不依赖 Textual、Typer 或任何具体业务模块。`lab`、`api`、`check`、`pack` 是独立业务模块；本轮 `lab` 只提供主页使用的 Fake Data，其余模块仅保留可扩展边界。`cli` 负责命令编排，`tui` 负责显示和交互，平台实现由 Adapter 层提供。

不引入 Python entry point 插件发现、第三方插件安装管理、插件权限/生命周期、复杂 DI 容器、Event Bus 或微内核。Registry 只在进程内保存显式注册的实现，未来新增实现时只需新增实现类和一个注册位置。

## 目录与职责

```text
src/csbox/
  cli/                 Typer 命令入口（csbox、csbox doctor）
  core/                Pydantic 领域模型、Protocol/ABC、Registry、环境检测
  lab/                 实验领域边界和 Fake Home Data
  api/                 API 测试领域预留接口
  check/               项目检查领域预留接口
  pack/                打包提交领域预留接口
  locales/             zh_CN i18n JSON 资源和加载器
  tui/                 Textual App、HomeScreen、widgets、dialogs、theme CSS
tests/                 CLI、core、i18n、TUI smoke 和依赖隔离测试
docs/reference/        TUI 概念图
docs/TUI_DESIGN.md     长期 TUI 设计原则
```

业务包的预留接口不实现业务逻辑，只表达输入、输出和选择边界；不会为了填目录增加没有职责的文件。

## 稳定接口

### Registry

`core.registry.Registry[T]` 是轻量的进程内注册表，提供 `register(key, implementation)`、`get(key)`、`has(key)` 和 `items()`。重复 key、空 key 和未知 key 都给出明确异常。各业务模块创建自己的 Registry 实例，默认实现由代码显式注册，不扫描环境、不加载第三方代码。

### 平台终端

`TerminalBackend` 为 ABC，定义 `spawn`、`read`、`write`、`resize`、`is_alive` 和 `close`。`WindowsConPTYBackend` 与 `UnixPTYBackend` 作为未来实现位置存在，本轮方法明确抛出未实现异常，不创建真实 PTY，也不在 Linux 导入 `pywinpty`。

### Shell Adapter/Profile

Shell profile 是独立数据/适配边界，明确描述显示名称、可执行文件和平台条件：

- Windows PowerShell 5.1：`powershell.exe`
- PowerShell 7：`pwsh.exe`
- Bash：`bash`
- Zsh：`zsh`

检测逻辑只负责识别当前 Shell、可执行文件是否存在、WSL 标记和终端尺寸，不启动 Shell。正式兼容目标固定为 Windows PowerShell 5.1、PowerShell 7、WSL2/Linux Bash，Linux/WSL Zsh 尽量兼容。

### 业务扩展接口

在 `core.protocols` 中定义以下稳定 Protocol/ABC，业务包通过各自 Registry 选择实现：

- `EvidenceProvider`：采集或读取证据对象。
- `EvidenceRenderer`：把证据渲染成可展示/导出的结果。
- `CheckRule`：接收项目上下文并返回检查结果。
- `ProjectDetector`：识别项目类型和项目元数据。
- `BuildAdapter`：为一种构建系统生成干净构建结果。
- `Exporter`：把领域结果导出到目标格式。

本轮只定义最小方法签名、领域模型和默认空 Registry，不实现真实证据、规则、Detector、构建或导出。

## TUI 设计

`CSBoxApp` 只负责组装 `HomeScreen` 和注入 `HomeDataSource`；`HomeScreen` 不执行环境探测或业务操作。主页由品牌区、环境摘要、主要入口、最近实验和统一快捷键区组成。主要入口是可键盘聚焦的按钮，当前全部打开中文未实现 Modal；最近实验数据由 `lab` 提供并明确显示 `[DEMO]` 与“仅用于原型展示”。

视觉使用深色炭黑/午夜蓝背景、低发光蓝紫品牌色、青色和琥珀色语义状态色。颜色只辅助信息，状态同时带文字和符号；不使用 Emoji。≥120 列使用入口与最近实验双栏，80–119 列使用紧凑单栏，80×24 保持可读和可操作。所有交互提供可见 focus 状态和关闭 Modal 的 Escape/按钮路径。

## i18n

首版资源文件为 `src/csbox/locales/zh_CN.json`，通过 `importlib.resources` 加载并提供按 key 查询的 Translator。TUI 可见文案、CLI doctor 标签、快捷键提示和 Modal 文案均从资源层取得；技术名词如 Git、API、HTTP、JSON、OpenAPI、Docker、Maven、Gradle、npm、PowerShell、WSL、Shell 保留英文。缺失 key 立即抛出明确错误，避免静默显示错误文案。

## 验证策略

测试覆盖 CLI `--help`、`doctor` 输出、平台/Shell/WSL/终端尺寸检测、Registry 行为、i18n 中文资源、TerminalBackend 可导入性、Textual App 在测试尺寸下挂载 `HomeScreen`、未实现入口 Modal，以及 Linux/WSL 不要求安装 `pywinpty`。`uv sync`、`pytest`、`ruff check` 和 `ruff format --check` 是首次交付门槛。

## 后续边界

真实 PTY/ConPTY、终端代理、截图与图像处理、Evidence 链、API 测试证据、项目检测和 CheckRule、Maven/Gradle/npm/PowerShell/WSL 业务适配、干净打包、真实配置持久化和发布 PyPI 均留到后续阶段。首版的 `pyte`、`Pillow`、`wcwidth`、`HTTPX` 作为 future optional dependencies 预留；`pywinpty` 仅在 Windows 平台条件安装。
