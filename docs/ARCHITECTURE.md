# CSBox 架构说明

## 依赖方向

```text
core (events, display_width, PTY ports, shell)
        ↓
lab (proxy, recorder, screen, captures, replay, renderer, export)
        ↓
tui / cli  ───────────────→  check / pack services
```

`core` 的终端协议层只描述事件、尺寸、显示宽度和 backend 边界，不依赖 Recorder、pyte、Pillow 或 Textual。`core.fonts` 是 API 与 Lab evidence 共用的兼容入口，它委托 `lab.fonts` 并使用 Pillow。`lab` 将真实 backend 的字节转换为统一 `TerminalEvent`，再分发给 recorder、emulator 和 Capture store。`check`、`pack` 是独立 service；`pack` 通过 `CheckService` 复用检查结果，但不修改源项目。CLI/TUI 负责编排和展示。

## Unified Evidence 与 Report

Evidence Set 使用闭合的带 discriminator 的 source union：`LabCaptureSource` 保存
`(source_type, session_id, capture_id)`，`ApiStepSource` 保存
`(source_type, run_id, step_index)`。Evidence Item 只在这两个来源引用与用户的
`title`、`caption`、`note` 之间建立顺序关系，不复制来源 payload。

旧的 v1 Lab-only Evidence Set 在读取时只做内存中的确定性规范化，不改写文件；新建或
显式保存的文档使用 Evidence Set schema v2。未知版本、未知 source type、未知字段和
损坏文档都 fail closed，单个损坏文件不会阻塞其他集合列出。

Report 先通过 `EvidenceSourceResolver` 将每一项路由到 `LabCaptureResolver` 或
`ApiStepResolver`，再把 Lab Capture 交给 `TerminalEvidenceRenderer`，把安全的
`ApiEvidence` 交给 `ApiEvidenceRenderer`。所有来源在进入既有 staging/publication
事务前按 Evidence Set 顺序完成解析；因此混合来源不会按类型分组，也不会发布部分报告。

## 实验事件流

```text
Shell ←→ UnixPTYBackend / WindowsConPTYBackend
              ↓ bytes
        TerminalProxy
          ├─ input/output adapter（原样转发）
          ├─ TerminalEventDispatcher → AsciicastV3Recorder
          ├─ TerminalEmulator → CaptureStore
          └─ resize / exit / error → session metadata
```

`TerminalProxy` 负责 raw terminal state、Lab alternate-screen enter/restore、SIGWINCH（Unix）、Capture key matching、短写重试、EOF 和 cleanup。Capture 快捷键本身不会转发给 Shell；其他输入和输出按字节传递，但新的 recorder 不持久化 raw input。recorder 关闭时 flush UTF-8 decoder tail，并将退出和失败状态写入 metadata。

## Replay 与导出

`ReplayService` 从 asciicast v3 reader 得到带相对时间和 cast byte offset 的事件。它以 checkpoint snapshot 为起点恢复 emulator，再向前 feed 到目标时间，因此 backward seek 不需要修改原始 `session.cast`。checkpoint 是可重建派生文件，包含版本、cast fingerprint、checksum、event index、offset、时间和 snapshot；cast 改变、文件损坏或几何不一致时会丢弃并重建。

`LabExporter` 从 CaptureStore 按 timestamp 顺序渲染 PNG，生成相对链接的 `evidence.md`，并复制 `session.cast`；存在可信 Capture command 时额外生成 `commands.txt`。输出路径和文件名经过安全化，禁止 traversal；已有 evidence 需要显式 `--force` 才覆盖。

## 项目检查与打包

`CheckService` 共享有边界的文件 inventory，分别运行 detector、规则和可选 BuildAdapter。没有 `--build` 时不启动外部 build。敏感规则只返回路径、行号和类别。

`PackService` 的顺序是 `check → TemporaryDirectory staging → safe copy → exclusions → optional ZIP verify`。符号链接不跟随逃逸 root，真实 `.env`/私钥拒绝进入 archive，输出 ZIP 会自排除，任何失败都保留原项目不动。

## CJK 与字体

领域 snapshot 按 terminal cell 保存 continuation cell 和属性；`display_width` 是 TUI、screen、Capture、renderer、footer、路径和表格的唯一宽度语义。PNG renderer 按列定位 cell，以 ASCII mono advance 推导单元格宽度，并使用 CJK fallback 绘制双宽字符；FontResolver 优先显式配置，其次搜索平台字体。字体缺失是可见错误，不会以错误的单宽中文截图冒充证据。

## 平台边界

Unix backend 使用真实 PTY，支持初始尺寸、resize、Ctrl+C、EOF 和 child reap。Windows backend 延迟导入 pywinpty，显式请求 ConPTY backend `0`，用 reader queue 将 high-level Unicode 输出转换为 UTF-8 bytes，并在 close 前尽量 drain 尾帧。PowerShell 5.1/7 的原生启动测试在 Windows CI 运行；没有 Windows runner 或 Shell 时只记录 limitation。

## API 响应脱敏边界

`ApiResponse` 可以作为 assertion 执行期间的瞬态内部表示，但不得直接进入 persistence、evidence、日志或 TUI。所有这些外部消费者必须先调用 `ApiResponse.redacted_copy(redactor)`，并且只保存或展示返回的副本。HTTPX transport 当前在构造返回值前已执行同一套 URL、header 和 body 脱敏；消费边界再次调用时保持幂等。

`ApiStepResolver` 只从 `ApiRunRepository.load()` 得到已持久化的安全 `ApiRun`，再复用
`ApiEvidenceBuilder` 和 `redact_evidence()` 构造报告所需的安全视图。Evidence Set、
Report exporter、Markdown、DOCX、PNG 和 TUI 都不接触瞬态 raw assertion view，也不触发
API transport。

`Redactor` 依据显式 policy、request-derived 精确值和敏感字段名工作。它不会假设能够推断任意未知 secret，也不会把普通 response body 全部遮蔽；无法通过 policy 或结构识别的未知内容属于调用方必须明确配置的剩余风险。

## Distribution 与安装态

项目使用 Hatchling 的 `src/csbox` package layout。wheel 只包含运行时 Python package、locale JSON、Textual TCSS 和 distribution metadata；sdist 只保留公开 README、许可证、用户文档、`src/csbox` 与构建所需配置，不包含测试、参考图片或内部开发资料。版本由 distribution metadata 提供给运行时，`csbox --version`、session/manifest 字段和 wheel metadata 使用同一个 `0.6.0rc1` 版本。公开文档以 Simplified Chinese `README.md` 为默认 README，完整英文文档为 `README.en.md`。

安装后的入口是 `csbox` console script。用户可以用普通 venv 或 `uv tool install <wheel>` 安装，再从项目目录之外运行 `csbox --help`、`doctor`、`check` 和 `pack`；locale、TCSS 与 renderer 通过 package/resource 或系统字体查找，不依赖当前 Git checkout。
