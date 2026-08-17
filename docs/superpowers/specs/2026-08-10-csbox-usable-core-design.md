# CSBox 首个可用实验记录核心设计

日期：2026-08-10

## 目标与边界

本轮把 CSBox 从 Fake Data Prototype 推进为可实际使用的实验记录与课程项目交付工具。核心闭环是：

```text
真实 Shell
  -> 透明 PTY/ConPTY 代理
  -> 统一 TerminalEvent
  -> asciicast v3 录制 + TerminalScreen
  -> 过程 Capture / 历史 Replay 补 Capture
  -> PNG + 中文 Markdown 导出
```

同一版本提供第一版项目检查、显式构建验证、无损 staging 打包和真实配置持久化。API 模块保持占位，不实现 HTTP/API 测试能力，也不在文档中把它宣传为已完成。

正式兼容目标是 Windows PowerShell 5.1、PowerShell 7、WSL2/Linux Bash；Zsh 尽可能兼容。当前开发机是 WSL2，可本机验证 Unix PTY 和 Bash；Windows ConPTY、PowerShell 5.1/7 的结论必须来自 Windows CI，不能用 WSL interop 冒充 Windows ConPTY 验证。

## 方案比较与选择

### 方案 A：在 CLI 循环中直接同时写控制台、录制文件和 pyte

实现最短，但 backend、文件格式、屏幕模型和 UI 会互相依赖。任何一个慢消费者都可能卡住 Shell，回放也难复用实时路径。该方案不采用。

### 方案 B：统一事件流与独立消费者

backend 只负责真实终端进程 I/O；代理服务把原始 I/O、resize、capture 和 exit 转成统一事件，再扇出给控制台、异步 Recorder、终端仿真器和 Capture 管理器。录制和 replay 使用同一事件语义，PNG 只消费领域快照。该方案边界清楚、可测试且可迁移，作为本轮方案。

### 方案 C：把 asciinema CLI 作为外部录制器

可快速获得 Unix 录制，但 Windows、历史补 Capture、内置 screen state 和统一配置都需要第二套集成，还会增加外部可执行文件依赖。该方案不采用；生成的 `session.cast` 仍尽量兼容 asciicast v3，便于未来与生态工具互通。

## 总体架构

```text
Typer CLI / Textual TUI
          |
          v
LabService / ReviewService / CheckService / PackService
          |
          v
Domain models + Protocols + configuration
          |
          v
Unix PTY / Windows ConPTY / pyte / Pillow / subprocess adapters
```

实验进行态的细化数据流为：

```text
stdin ----> CaptureKeyMatcher ----> TerminalBackend.write
                    |                       |
                    |                       v
                    |              child Shell / program
                    |                       |
                    v                       v
              CAPTURE event       TerminalBackend.read
                                            |
                                            v
                                    TerminalEvent stream
                                      |    |     |
                                      |    |     +--> TerminalEmulator
                                      |    +--------> async Recorder
                                      +-------------> stdout
```

TUI 不调用 subprocess，不直接读 session 文件。Screen 只消费服务返回的快照并调用明确的服务方法。backend 不导入 Recorder、pyte、Pillow、Textual 或 Evidence 类型。

## 模块边界

在保留现有 import 兼容性的前提下，新增以下聚合边界：

- `core/display_width.py`：全项目唯一的 display-cell 宽度、截断、补齐工具。
- `core/events.py`：`TerminalEventType`、`TerminalEvent`、`TerminalSize` 和事件时钟。
- `core/terminal.py`：`TerminalBackend` 公共 ABC、错误类型和兼容 re-export。
- `core/terminal_unix.py`：Unix/WSL/Linux PTY 实现，只在 Unix 导入 `pty/termios/fcntl`。
- `core/terminal_windows.py`：pywinpty/ConPTY 实现，只在 Windows 运行时导入 `winpty`。
- `core/shell.py`：四种 profile、显式/自动选择、能力探测和版本探测。
- `config/`：Pydantic 配置模型、用户/项目路径、TOML 读取与原子持久化。
- `lab/models.py`：session metadata、capture、命令上下文和列表摘要。
- `lab/recorder.py`：asciicast v3 追加写 Recorder 和容错 Reader。
- `lab/screen.py`：领域 `TerminalCell/TerminalSnapshot` 与私有 pyte adapter。
- `lab/captures.py`：captures 原子读写、快速 snapshot 保存、编辑和删除。
- `lab/replay.py`：确定性重放、checkpoint 索引和 seek。
- `lab/renderer.py`：系统字体发现和按 cell 布局的 Pillow PNG 渲染。
- `lab/exporter.py`：实验目录、中文 Markdown、commands 和 cast 导出。
- `lab/proxy.py`：透明终端代理、raw mode、resize、快捷键过滤和事件扇出。
- `lab/service.py`：session 生命周期、仓库查询和 CLI/TUI 用例编排。
- `check/`：领域模型、detector、rules、build adapters 与业务服务。
- `pack/`：过滤策略、临时 staging、ZIP 和统计服务。
- `runtime.py`：很薄的组合根，向 CLI/TUI 注入服务，不形成 DI 框架。

文件按职责拆分，但避免为只有几行的类创建独立文件；实现时可把紧密相关的小类型保留在同一模块。

## TerminalBackend 契约

`TerminalBackend` 保留 `spawn/read/write/resize/is_alive/close`，并补充初始尺寸、退出码与等待语义。`read()` 使用三态：

- `bytes` 且非空：本次输出；
- `None`：当前无数据，可继续轮询；
- `b""`：PTY 已 EOF。

`close()` 幂等；自然退出优先，只有代理异常清理时才终止仍存活的子进程。底层异常包装为稳定错误类型，CLI 默认显示中文操作建议，`--verbose` 才打印异常链。

Unix 后端使用 `pty.fork()` 建立真正的 controlling terminal，父进程 master fd 设为 non-blocking，`selectors` 负责轮询；`TIOCSWINSZ` 实现 resize，`waitpid(WNOHANG)` 获取退出状态。子进程继承受控的环境副本、cwd 和 `TERM=xterm-256color`，不使用 `subprocess.PIPE` 代替 PTY。

Windows 后端通过延迟导入 `winpty.PtyProcess` 创建 pseudoconsole，使用 `spawn/read/write/setwinsize/isalive/exitstatus/close`。pywinpty 未安装、Windows 版本不支持或 ConPTY 创建失败时抛出可翻译能力错误。Windows 与 Unix 模块不在对方平台的正常导入路径加载平台专属模块。

## Shell 选择策略

profile 是有行为的轻量 adapter，包含 kind、显示名、可执行文件、适用平台和启动参数。顺序如下：

1. `--shell` 显式值，支持 `powershell`、`pwsh`、`bash`、`zsh` 及规范别名；
2. 项目配置中的 shell；
3. 当前环境可识别且可执行的 shell；
4. Windows 按 `pwsh -> powershell`，Unix/WSL 按当前 `$SHELL -> bash -> zsh`；
5. 无可用项时返回包含安装/替代建议的中文错误。

版本探测使用短超时的非交互命令，失败只使 `shell_version` 为空，不阻止交互 Shell。高级 cwd/exit-code hook 是可选能力，本轮不以 hook 成功作为启动前提。

## TerminalEvent 模型

事件必须包含：

- 单调时钟绝对值 `monotonic_time`；
- 相对 session 起点的 `relative_time`；
- 单调递增 `sequence`；
- `type`；
- 类型化 `payload`。

类型固定为 `OUTPUT`、`INPUT`、`RESIZE`、`MARK`、`CAPTURE`、`EXIT`。OUTPUT/INPUT 在内存事件中保留 bytes，避免在 UTF-8 多字节边界提前破坏数据；Recorder 使用增量 UTF-8 decoder 转为 cast 字符串，Terminal emulator 直接消费 bytes。RESIZE 使用 `TerminalSize`，EXIT 使用整数或空退出码，MARK/CAPTURE 使用稳定字符串 id/label。

事件 dispatcher 是进程内同步扇出器，不做插件系统。Recorder 自己用有界 queue 和后台线程追加写，磁盘延迟不阻塞 Shell；屏幕模型和控制台输出保持轻量同步。队列接近上限时按块等待的时间有严格上限并报告写入故障，不静默丢失 session 事件。

## Session 格式与恢复

每个 session 目录为：

```text
.csbox/sessions/<session-id>/
  session.cast
  metadata.json
  captures.json
  checkpoints.json
```

`session.cast` 第一行是 asciicast v3 header，事件行使用 `[interval, code, data]`；映射为 `o/i/r/m/x`。interval 保留毫秒精度并携带舍入余数，避免长 session 累积漂移。header 只记录必要且非敏感的环境数据，默认不复制整个 environment。

`metadata.json` 是独立、可演进的 Pydantic 模型，至少包含 session id、实验名、UTC start/end time、platform、shell、shell version、初始 rows/cols、cwd 和 CSBox version。开始时先原子写入 `status=running`，结束时更新为 `completed/interrupted/failed`。代理或 Shell 异常也在 `finally` 尝试写 EXIT 和 metadata；已 flush 的 cast 行始终可读。

`captures.json` 用 `{version, captures: [...]}` 包装，写入采用同目录临时文件、flush/fsync、`os.replace`。读取时若主文件损坏，尝试 `.bak`；单个非法 capture 被隔离并返回警告，不让整个 session 不可用。

Reader 忽略 cast 注释和未知扩展事件；最后一行截断时保留此前完整事件并报告恢复警告。中间损坏不会被伪装为正常，而是带行号警告并继续处理可安全解析的独立 NDJSON 行。

## TerminalScreen 与 CJK

领域层只暴露：

- `TerminalCell`：character、display width、foreground/background、bold、italic、underline、strikethrough、reverse；
- `TerminalCursor`：row、column、visible；
- `TerminalSnapshot`：rows、cols、cells、cursor、relative timestamp。

pyte 只存在于 `lab.screen` 的 adapter 内。它处理 ANSI color、cursor、erase、carriage return、反复刷新和 resize；adapter 把 pyte buffer 转成不可变领域 snapshot，并集中容纳 pyte 兼容补丁。任何 UI、Capture、Renderer 和测试 fixture 都不依赖 pyte 类型。

`core.display_width` 统一封装 `wcwidth/wcswidth`，提供 display width、按 cell 截断、带省略号截断、左右补齐和组合字符分组。控制字符按明确策略忽略或替换，绝不把 `len(text)` 当显示宽度。默认 East Asian ambiguous width 为 1，后续可配置为 2；相同策略贯穿 TUI、screen adapter 和 PNG。

回归样例固定包含 `hello`、`项目检查`、`API 测试`、`CSBox 项目检查`、`abc中文123`、`计算机网络实验`、Windows 中文路径、Unix 中文路径、combining mark、full-width 字符、恰好宽度和多 cell 超限。

## Capture 快捷键

默认使用 F12，并允许在项目/用户配置中替换为受支持键序列。原因是它不属于 Bash readline 和 PSReadLine 的常用编辑/历史键，同时可被主流终端编码为可识别的功能键序列。它仍可能被宿主 Terminal 或编辑器抢占，所以启动前会：

- 解析配置并验证当前 input adapter 是否支持；
- 显示一次简短的可用/不可用提示；
- 在不支持或无法收到该键时保留 `csbox lab capture <session>`/review 补 Capture 的降级路径；
- 文档列出 Windows Terminal 与 VS Code Terminal 的 keybinding 排查方法。

`CaptureKeyMatcher` 支持跨 read chunk 的前缀匹配。完整 capture 序列只产生 CAPTURE/MARK，不写给 Shell；不匹配的前缀原样、按顺序转发。Capture 只复制当前 immutable snapshot 并把持久化交给短操作/后台队列，不现场渲染 PNG。

## Replay、checkpoint 与历史补 Capture

Replay 从相同 cast Reader 产生统一事件，再喂给新的 Terminal emulator，保证实时画面和历史画面走同一路径。`ReplayService.seek(t)` 返回目标时间之前最后一个完整事件对应的 snapshot。

checkpoint 每 5 秒或 500 个可回放事件建立一次，记录 event index、cast 字节偏移、relative time、尺寸和领域 snapshot。首次打开旧 session 时可惰性构建；索引损坏可删除式重建逻辑仅针对派生索引，不修改 cast。seek 从最近 checkpoint 恢复 emulator，再播放短尾段，不每次从 0 开始。

review 在任意时间点调用 `create_capture(snapshot, timestamp)`，与实验进行态 Capture 使用同一 `CaptureStore`。标题编辑和删除只改 captures metadata，不改原始 cast。

## PNG 与实验导出

Renderer 使用 Pillow 按固定 cell grid 画图，不截取桌面或宿主 Terminal。主题提供 dark/light 两套确定性 palette；每个 glyph 的 x 坐标由 column × cell width 决定，双宽字符占两个 cell，combining mark 与基字符同 origin。

字体发现依次检查显式配置、平台常见等宽字体和 CJK fallback。仓库不提交字体。若找不到能覆盖 CJK 的组合，抛出中文错误并列出配置方式；不生成乱码图片冒充成功。

`lab export` 输出：

```text
<experiment>/
  evidence/01-<safe-title>.png
  evidence/02-<safe-title>.png
  evidence.md
  commands.txt
  session.cast
```

Markdown 使用相对路径和中文标题。只有命令识别置信度足够时才写命令；否则省略命令字段，不猜测。文件名过滤 Windows 非法字符、保留中文并防止路径穿越。

## 实验代理与 CLI

`csbox lab start [name] [--shell ...] [--verbose]` 不进入 Textual。CLI 在当前终端切 raw mode、安装 resize 处理器、启动 backend 和 dispatcher，完整转发 bytes。退出和异常都通过 context manager 恢复 termios/Windows console mode 和 signal handler。

代理优先保证 full-screen program、ANSI 和控制键 fidelity，不绘制永久状态栏。只在启动前/退出后输出极简中文提示；进入 raw mode 后不插入业务文案。Ctrl+C、Ctrl+D、Ctrl+L、方向键、Home/End、Delete、Tab 等除 Capture 键外均原样传给 child。

`lab review [session]` 和 `lab export <session>` 接受 session id 或无歧义前缀；review 无参数选择最近 session。空/损坏 session 显示恢复警告而不是堆栈。

## Review TUI

ReviewScreen 使用统一 keymap：Space 播放/暂停，Left/Right seek，PageUp/PageDown 大步跳转，C Capture，E 编辑标题，Delete 删除 Capture，Tab 切换 pane，Escape 返回，Q 退出。

- `>=120` 列：Terminal replay 为左侧主区，右侧上下排列 timeline 与 captures，底部为 progress/time/key hints。
- `80-119` 列：只显示一个主 pane，Tab 在 Terminal/timeline/captures 循环；80×24 保留标题、内容和单行 footer。

播放 timer 只推进领域 `ReviewController` 时间并请求 snapshot；Screen 不解析 cast。编辑/删除通过 Modal 和 service 完成。Capture 列表变化后 controller 返回新 view model。

## 配置与真实 Home

配置优先级为 CLI > 项目 `.csbox/config.toml` > 用户配置 > 默认值。用户配置路径为 Windows `%APPDATA%/CSBox/config.toml`，Unix `${XDG_CONFIG_HOME:-~/.config}/csbox/config.toml`。模型至少覆盖 locale、shell、capture key、render theme/font、pack naming、学生/课程字段和大文件阈值。

TOML 解析错误包含文件路径、字段和中文修复提示，不把 Pydantic traceback 作为主错误。持久化只写已知 schema，使用原子替换，不修改未知项目文件。

Home 使用真实 `SessionRepository` 和当前 cwd 生成快照：当前平台、Shell、项目路径、最近 session、Capture 数量。没有历史时显示“暂无实验记录”，不再显示 Fake Data。API 入口继续明确标为未实现。

## check 与 Build Adapter

`CheckService` 与 TUI 分离，输出 `CheckReport`，每项状态为 PASS/WARN/FAIL/SKIP。Detector 识别 `package.json`、`pom.xml`、Gradle 两种文件、`pyproject.toml` 和 `requirements.txt`，允许一个仓库存在多个项目信号。

内置规则覆盖 README、`.env`/敏感文件、私钥、依赖/构建/IDE/cache/log 目录与文件、大文件、Git dirty/untracked、本机 Windows/Unix 绝对路径、轻量 hard-coded secret。文本扫描跳过二进制和超大文件；secret finding 只报告位置与类别，绝不输出匹配值。

`csbox check` 默认不构建；`--build` 才调用 adapter。Node/Maven/Gradle/Python adapter 优先 `mvnw/gradlew`（Windows 对应 `.cmd/.bat`），使用参数数组、cwd、超时、退出码和有界输出，不通过 shell 拼接。缺工具或项目没有可用 build target 时返回 SKIP/中文建议。

CLI 提供默认 TUI、`--plain` 和 `--json`；JSON 使用稳定 machine-readable schema，业务层不依赖 Rich/Textual。

## pack

`PackService` 的固定流程是 detect/check -> 建立临时 staging -> 按规则复制 -> 可选 verify -> ZIP。它永不删除、移动或原地改写项目源文件。

默认排除 `.git`、`.venv`、`node_modules`、`target`、`build`、`dist`、`__pycache__`、pytest/Ruff cache、IDE cache、log 和输出 ZIP 自身。真实 `.env` 与常见私钥默认拒绝进入包；`.env.example` 可保留。符号链接默认跳过并报告，避免逃逸 root；相对路径在复制和写 ZIP 前都验证没有 `..` 或绝对路径。

结果报告源文件总大小、ZIP 大小、包含文件数、排除数量与按规则摘要。文件名模板经 Pydantic 校验和平台安全化；空字段不会生成连续危险分隔符或绝对路径。

## 可靠性策略

- 所有会改变终端模式、signal handler 或进程资源的操作都由 context manager + `finally` 恢复。
- session 先建目录和初始 metadata，再启动 Shell；启动失败保留可诊断 metadata 或安全清理空临时目录，不留下伪完成 session。
- Recorder 写失败会通知代理并尽快安全结束录制，但不吞掉已 flush 数据。
- metadata/captures/checkpoint 使用原子写；cast 使用逐行 append+flush，适合 crash 恢复。
- replay/导出对空 session、最后一行截断、未知事件、中文路径和缺字体都有显式结果或中文错误。
- pack 不跟随逃逸 root 的 symlink，不覆盖已有 ZIP，除非用户显式 `--force`。

## 测试与 CI

新增 unit tests 覆盖 display width/CJK truncate、事件模型、shell 选择、配置合并、screen/snapshot、recorder reader/writer、capture store、replay checkpoint、renderer 结构、export、check rules/detectors、build command 选择和 pack filters/staging。

Linux/WSL PTY integration 使用真实 `bash --noprofile --norc` 验证 echo、中文 printf、ANSI、Ctrl+C、resize、长输出和持续命令中断。测试使用 timeout 和最终清理，避免挂住 CI。

Windows backend 使用依赖注入/mocked unit tests验证 pywinpty 调用契约；Windows GitHub Actions 另跑真实 PowerShell 5.1 和 PowerShell 7 PTY smoke。CI matrix 至少包含 Ubuntu/Windows 和 Python 3.11/3.14，统一执行 `uv sync --locked`、pytest、Ruff check、Ruff format check；平台 integration 通过 marker 精确选择。

Textual `run_test` 覆盖 HomeScreen、ReviewScreen、ProjectCheckScreen、Modal、80×24 和 >=120。Golden 测试优先固定 snapshot JSON/布局语义；PNG 只断言尺寸、cell 坐标和 palette 关系，不提交依赖特定系统字体的整图像素 golden。

## 依赖治理

`pyte`、Pillow、`wcwidth` 是 lab 核心运行依赖，从 `future` optional group 移入主依赖；`pywinpty` 保持 `sys_platform == 'win32'` marker。HTTPX 本轮无用途，删除。除非实现中出现标准库无法可靠解决的问题，不新增大型框架。

## 分阶段交付

1. display width、配置、事件、shell/backend 与真实 Unix PTY；
2. recorder、screen、capture、replay/checkpoint；
3. renderer、export、lab proxy CLI 与 Linux smoke；
4. Review/Home TUI；
5. check/build/pack；
6. Windows tests/CI、全量回归、文档与真实 push。

每阶段坚持测试先行并形成可测试的逻辑 commit。若 Windows API 或宿主快捷键存在环境差异，保留降级路径和明确限制，不阻塞 Linux、session/replay、check/pack 等独立能力。

## 规格自审结论

- 本设计不包含未定义占位行为；API 明确保持占位。
- backend、事件、录制、screen、replay、renderer 的依赖方向与总体数据流一致。
- 27 部分需求被分解到终端核心、session/evidence、TUI、check/pack、配置、可靠性、测试、CI 和文档阶段。
- 当前唯一无法本机直接验收的是原生 Windows ConPTY；以真实 Windows CI 作为证据来源，并在最终报告区分实现状态与验证状态。
