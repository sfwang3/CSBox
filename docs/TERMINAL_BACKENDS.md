# Terminal backend 与 Shell 说明

## 统一接口

`TerminalBackend` 只暴露 `spawn`、`read`、`write`、`resize`、`is_alive`、`wait`、`exit_code` 和 `close`。读取有三种状态：`bytes` 表示输出、`None` 表示暂时无数据、`b""` 表示 EOF。backend 不知道 recorder、screen、renderer 或 TUI。

## Unix / WSL

`UnixPTYBackend` 通过真实 PTY 启动 Shell，设置初始 rows/columns，采用非阻塞读写并处理短写、EAGAIN、EOF、Ctrl+C、resize 和 child reap。正式目标是 `bash --noprofile --norc`；Zsh 通过 `zsh -f` 尽量兼容。Linux/WSL 的集成测试实际启动 Bash，而不是 fake process。

## Windows

`WindowsConPTYBackend` 只在 `spawn` 时导入 pywinpty，调用 `PtyProcess.spawn(..., dimensions=(rows, columns), backend="0")`，其中 `0` 明确选择 ConPTY。pywinpty 的 high-level API 读写字符串，因此 backend 负责 UTF-8 边界、reader queue、partial write、resize、尾帧 drain 和可重试 close。`PtyProcess.read()` 的 `EOFError`/closed-handle 只有在确认 child 已退出后才是正常 EOF；空字符串 `0011Ignore` 是 no-output sentinel，不能结束 reader。

Shell profile：

- Windows PowerShell 5.1：`powershell.exe -NoLogo`
- PowerShell 7：`pwsh.exe -NoLogo`
- Bash：`bash --noprofile --norc`
- Zsh：`zsh -f`

Windows PowerShell 5.1 与 PowerShell 7 的 native smoke 都直接创建 `WindowsConPTYBackend`，发送中文命令，调用 resize，读取输出并检查 exit code。它们在 Windows CI 中执行；开发者在 Linux/WSL 上不能把 fake pywinpty contract tests 当作 Windows 原生验证。

## Proxy 与宿主终端

`TerminalProxy` 用 `FileInputAdapter`/`FileOutputAdapter` 隔离 stdin/stdout；Lab 使用标准 alternate screen，Unix raw termios 和 Windows console mode 都在终端状态退出时恢复。Unix 安装并恢复 `SIGWINCH`，Windows 不假定有该信号。实验态不启动 full-screen Textual，避免破坏用户自己的 terminal workflow。

`F12 Capture` 是唯一正式的 Lab Capture 快捷键。VS Code、Windows Terminal 或其他宿主可能抢占 F12；`CaptureBindingProbe` 只给 advisory，不改宿主、Windows Terminal、Bash 或 PSReadLine 设置。无法实时捕获时，可在 Review 中补 Capture。

## 安装态说明

安装 wheel 或通过 `uv tool install` 使用时，Lab 仍调用当前平台的 Shell 和 PTY backend。CSBox 不把字体文件放进 distribution；PNG renderer 使用系统等宽字体与 CJK fallback。安装后如果系统没有可用中文字体，renderer 会返回明确错误，需要安装字体或在项目配置中指定字体路径。
