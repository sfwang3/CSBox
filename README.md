面向计算机专业学生的实验记录与课程项目交付工具

# CSBox

CSBox 把终端实验记录、可回看的 Capture、证据 PNG/Markdown、项目检查和安全打包串成一条本地工作流。当前版本优先保证终端 fidelity：实验进行时保留宿主终端的输入输出，实验结束后再用 Review TUI 整理证据。

## 快速开始

需要 Python 3.11+ 和 [uv](https://docs.astral.sh/uv/)：

```bash
uv sync
uv run csbox doctor
uv run csbox
```

无参数运行会打开真实 Home TUI，展示当前项目最近的 session、Capture 数量、平台、Shell 和 cwd。TUI 只负责展示和调用 service；session、replay、check 与 pack 的领域逻辑不依赖 TUI。

## 实验记录与证据

在项目目录中启动实验：

```bash
uv run csbox lab start "计算机网络实验一"
uv run csbox lab list
uv run csbox lab review
uv run csbox lab export <SESSION> --output evidence --theme dark
```

`lab start` 会选择配置的 Shell，创建 `.csbox/sessions/<SESSION>/`，录制 asciicast v3，并同步维护终端 emulator、Capture dispatcher 和生命周期 metadata。默认 Capture 键是 `F12`，也可以在 `.csbox/config.toml` 中设置：

```toml
[lab]
capture_key = "ctrl-space"
```

VS Code、Windows Terminal 等宿主可能拦截 `F12`。启动时 CSBox 会给出 advisory，不修改宿主、VS Code、Bash 或 PSReadLine 配置；如果快捷键没有到达，可以在 Review 中补 Capture。

`lab export` 生成 PNG、`evidence.md` 和 `session.cast` 的副本；如果 Capture 中有可信的命令，还会生成 `commands.txt`。PNG 使用终端显示 cell 渲染，支持 ASCII/CJK 混排、ANSI 样式、dark/light theme 和字体 fallback；没有合适的 CJK 字体时会返回明确错误，不会静默生成错误证据。

## 项目检查与打包

默认检查只扫描，不执行构建：

```bash
uv run csbox check --plain
uv run csbox check --json
uv run csbox check --build
```

检查 README、`.env`、私钥、构建产物、缓存、日志、大文件、路径泄露、轻量 hard-coded secret 和 Git 状态，并按 Node、Maven、Gradle、Python 项目选择 BuildAdapter。敏感发现只显示文件、行号和类别，绝不输出 secret 内容。

安全打包使用临时 staging，永不清理或改写原项目：

```bash
uv run csbox pack --verify
uv run csbox pack --output ../deliverables --verify
uv run csbox pack --output deliverable.zip --verify --force
```

默认排除 `.git`、`.venv`、`.csbox`、`runtime`、`node_modules`、`target`、`build`、`dist`、`__pycache__`、pytest/Ruff/IDE cache 和日志；真实 `.env`、`.env.local` 等环境变体与私钥默认拒绝打包，`.env.example` 可以保留。没有 `--force` 时不会覆盖已有 ZIP。

## 支持范围与验证边界

- Windows PowerShell 5.1：`powershell.exe`
- PowerShell 7：`pwsh.exe`
- WSL/Linux Bash：`bash --noprofile --norc`
- Linux/WSL Zsh：尽量兼容，命令名为 `zsh`

Unix PTY、Bash、中文、ANSI、resize、Ctrl+C、长输出、录制、Capture、replay、PNG 和 export 在 Linux/WSL 开发环境中有真实 smoke 覆盖。Windows 使用真实 `WindowsConPTYBackend` 和 pywinpty，PowerShell 5.1/7 的原生启动、Unicode、resize、exit 由 Windows CI 执行；本地 Linux 不能替代 Windows 原生验证。CI 缺少某个 Shell 时，测试会以明确的 limitation skip，并不会被报告为已验证。

所有 TUI、terminal screen、replay、renderer、Capture、footer、路径和布局使用统一 `display_width`/`wcwidth` 语义。正式 UI 不使用 Emoji。

## 当前明确不包含的能力

`csbox.api` 仍是占位模块。本轮不实现 HTTP API testing、OpenAPI、Postman import、GraphQL、WebSocket、AI 或 plugin marketplace。

项目目前仍未发布到 PyPI，也未附带 LICENSE；请按仓库许可和课程要求使用。
