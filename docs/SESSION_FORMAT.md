# Session 与证据格式

每个 session 位于项目目录的 `.csbox/sessions/<safe-id>/`。目录名由仓库生成，不能包含 `/`、`\`、`.` 或 `..`。

```text
session.cast       asciicast v3 header + NDJSON events
metadata.json      session lifecycle and environment metadata
captures.json      ordered Capture records and terminal snapshots
checkpoints.json   ReplayService 的可重建派生索引
```

## session.cast

第一行是 asciicast v3 header，包含 terminal `cols`、`rows`、`type`，环境只允许安全的 `SHELL`/`TERM` 字段。事件使用标准 code：`o` output、`i` input、`r` resize、`m` mark/Capture、`x` exit。reader 对孤立坏行、未知 event 和 UTF-8 边界给出 warning，并尽量保留其余有效事件。

## metadata.json

metadata 记录 session id、实验名、`running`/`completed`/`interrupted`/`failed`、UTC started/ended 时间、平台、Shell、Shell version、初始 rows/columns、cwd 和 CSBox 版本。创建、完成和配置写入使用临时文件加 `os.replace`；运行中断也会尽力落下终态。

## captures.json

Capture 是不可变的领域记录：id、title、UTC createdAt、相对 timestamp、rows/columns、cwd、可选 command 和完整 `TerminalSnapshot`。snapshot 保存每个 terminal cell 的字符、display width、continuation、foreground/background、bold、underline、reverse 等属性。CaptureStore 以原子 JSON 写入，读取失败时不会伪造有效 Capture。

## checkpoints.json

checkpoint 不是原始 cast 的一部分。它保存 version、cast fingerprint、checksum，以及 event index、cast offset、relative time 和 snapshot。Replay 会验证来源 cast 和结构；指纹变化、损坏或不连续时丢弃索引并重建，绝不改写 `session.cast`。

## evidence export

导出目录包含按 Capture 时间顺序命名的 PNG、`evidence.md` 和复制的 `session.cast`；当 Capture 中存在可信 command 时还会有 `commands.txt`。Markdown 使用相对路径，标题经过安全文件名处理，Windows 非法字符、斜杠、反斜杠和 path traversal 都不会直接进入文件名。默认不覆盖已有输出；`--force` 才允许覆盖。PNG 尺寸来自 snapshot 的 rows/columns 与字体 cell advance，不使用跨平台整图 golden 作为契约。

session 与 evidence 是项目本地运行产物，默认在 `.gitignore` 中排除；不要将包含真实凭据的文件或私钥放进 session、evidence 或 pack 输入。

## 安装与版本

`metadata.json` 和 Pack manifest 中的 CSBox version 来自运行时 distribution metadata。通过源码环境、wheel 或 `uv tool install` 启动时，`csbox --version`、session metadata、manifest 和 wheel metadata 保持一致；安装态不依赖仓库中的 `tests/` 或文档资源。
