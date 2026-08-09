# CSBox TUI Design Principles

## 语言与文案

- 中文优先，默认使用简体中文。
- Git、API、HTTP、JSON、OpenAPI、Docker、Maven、Gradle、npm、PowerShell、WSL、Shell 等常用技术名词保留英文。
- 从第一天使用 i18n 资源层，界面文字不散落硬编码。
- 不使用 Emoji 作为图标或状态表达。

## 信息与布局

- 实验进行时未来采用非全屏工作模式，让用户继续使用自己的终端工作流。
- 管理和回看使用全屏 TUI，集中呈现实验、证据和项目交付状态。
- CJK 双宽字符是一等公民，布局和截断必须以终端显示宽度为准。
- 80×24 必须可用；≥120 列可以使用双栏布局，80–119 列使用紧凑单栏。
- 状态同时使用文字和符号表达，不能只依赖颜色。
- 快捷键在各页面保持统一，并始终显示底部提示。

## 架构与迭代

- 业务逻辑与 TUI 分离，TUI 只消费注入的领域快照和调用明确的端口。
- 先通过标记为 `[DEMO]` 的 Fake Data TUI Prototype，再接入真实 PTY。
- 平台能力通过 Adapter，业务能力通过 Protocol/ABC 与 Registry 扩展。
- V0.1 正式兼容目标是 Windows PowerShell 5.1、PowerShell 7、WSL2/Linux Bash；Linux/WSL Zsh 尽量兼容。
