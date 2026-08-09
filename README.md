# CSBox

CSBox 是一个面向计算机实验与项目交付的 CLI/TUI 工具。它计划把实验记录、API 测试证据、项目检查和干净打包整合到一条可追溯的工作流中。

当前项目处于早期开发阶段。V0.1 先建立可长期开发的 Python 工程骨架、跨平台终端接口和中文 TUI Prototype；真实实验录制、PTY/ConPTY、截图、API 测试、项目检查和打包业务会在后续阶段接入。

## 当前已完成

- `uv` 项目与依赖管理基线
- Typer CLI：`csbox` 和 `csbox doctor`
- Textual + Rich 深色中文首页 Prototype
- Pydantic 领域模型、Protocol/ABC 扩展接口和轻量 Registry/Adapter 边界
- Windows PowerShell 5.1、PowerShell 7、WSL/Linux Bash 的环境检测与 Shell profile 预留
- `TerminalBackend`、`WindowsConPTYBackend`、`UnixPTYBackend` 接口骨架
- 明确标记为 `[DEMO]` 的 Fake Data 首页

## 开始使用

需要 Python 3.11+ 和 `uv`：

```bash
uv sync
uv run csbox
uv run csbox doctor
```

无参数运行会打开 Textual 首页。`doctor` 只做基础跨平台检测：操作系统、Python 版本、当前 Shell、PowerShell 可执行文件、WSL 状态和终端尺寸。

## V0.1 兼容目标

- Windows PowerShell 5.1：`powershell.exe`
- PowerShell 7：`pwsh.exe`
- WSL2/Linux Bash：`bash`
- Linux/WSL Zsh：尽量兼容，命令名为 `zsh`

本项目暂不发布 PyPI，也不包含 LICENSE。后续预留依赖通过 optional dependency 管理，不会在首版 Fake Data Prototype 中伪造真实能力。
