# CSBox TUI 设计约束

## 两种工作状态

- `csbox lab start` 是透明 terminal workflow：不启动全屏 Textual，不绘制永久状态栏，把 Shell 的输入输出直接交给宿主终端。
- Home、Review、Check 和 Submission 是管理/回看界面，使用 Textual 全屏布局；它们通过 service/controller 获取数据，不解析 cast、不直接写 session 文件。

实验态只保留 `F12 Capture` 这一项正式输入拦截，序列是 `ESC [ 24 ~`。宿主终端可能抢占按键，界面和命令都会给出 advisory，并保留 Review 中补 Capture 的路径。

## 信息层级

Home 展示真实项目状态：最近 session、Capture 数量、当前平台、Shell 和 cwd。最近 session 可以直接进入 Review，项目检查入口进入 CheckScreen，准备提交入口进入完整的预检、准备和独立校验流程。

Submission 页面明确展示 `READY`、`WARNING`、`BLOCKED`，并保留 WARN、FAIL、SKIP 的原始语义和下一步。成功准备后展示恰好三个最终文件：`report.docx`、配置的项目 ZIP 和 `submission-manifest.json`；文案使用“已准备/已验证”，不把本地准备描述成上传或提交成功。Verify 可针对已复制或移动的 handoff 单独运行。

Review 的核心区域是 Terminal Replay；旁边或下方显示 timeline、Capture 列表、当前状态和操作提示。宽度策略如下：

- 120 列以上：左侧大 Replay，右侧 timeline/Capture，底部 progress 和 key hints。
- 80–119 列：单主视图，以 Tab 在 Terminal、Timeline、Captures 间切换。
- 80×24：保留可操作的主视图、状态和快捷键提示，不要求所有辅助信息同时出现。

Review 支持 play/pause、前后 seek、PageUp/PageDown 大步移动、跳到 Capture、新建/编辑/删除 Capture。删除需要确认。所有动作经过 `ReviewController` 和 service。

## 视觉与文字

- 默认是 charcoal/midnight 深色主题，export 同时支持 dark/light；颜色不是唯一状态表达，状态还必须有文字。
- 中文优先，技术名称保留英文；正式 UI 不使用 Emoji。
- 所有截断、padding、列宽、footer、路径和表格都使用 `display_width`/`wcwidth`，不能用 `len(text)` 代替终端显示宽度。
- CJK 双宽字符、combining mark、full-width character、reverse、underline 和 ANSI colors 必须在 screen snapshot 与 renderer 中保持一致。

## 分层

TUI 依赖领域 snapshot、`ReviewController`、`CheckService` 和 `LabService`。它不依赖 pyte 类型、不调用 subprocess、不直接读取或覆盖 session 文件。真实 Home 数据源读取 `SessionRepository` 与当前环境；Fake Data 仅保留为测试 fixture，不再作为首页数据。

## 安装态资源

Home、Review、API、Check 和 Submission app 共用 package 内的 `csbox.tcss`。locale JSON 和 TCSS 随 wheel 一起安装；TUI 启动不要求当前工作目录是源码 checkout。安装态 smoke 会从仓库外的临时中文路径启动 console entrypoint、加载中文 locale、启动 headless Textual app，并验证 CJK renderer 生成可解析的 PNG。
