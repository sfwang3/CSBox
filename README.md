# CSBox

CSBox 是面向计算机专业学生的本地实验记录与课程项目交付工具。需要记录终端操作、保留关键画面、检查项目或整理提交材料时，就使用 CSBox；数据保存在本地，不需要云端账号或远程服务。

当前 0.4.0.dev0 开发版尚未发布到 PyPI，支持 Python 3.11+。现在请从源码同步环境，或从本地构建的 wheel 安装；不要使用尚未提供的公开索引安装命令。

## 安装

从源码运行（当前开发版）：

```bash
git clone https://github.com/sfwang3/CSBox.git
cd CSBox
uv sync
uv run csbox
uv run csbox --help
```

从构建产物安装：

```bash
uv build
uv tool install dist/csbox-0.4.0.dev0-py3-none-any.whl
csbox
csbox --help
```

也可以安装到普通虚拟环境：

```bash
uv venv .venv
uv pip install .
```

CSBox 的运行时依赖会由 distribution metadata 自动安装。PNG 证据使用系统等宽字体和 CJK fallback；项目不会捆绑大型字体文件。若系统缺少支持中文的字体，安装 Noto CJK、微软雅黑或在项目配置中指定字体文件。

## 快速开始

先完成安装，再按这条路径开始第一次实验：

```text
安装 → csbox → 开始实验 → F12 保存关键画面 → 输入 exit → 实验记录 → 导出材料
```

从源码运行时，最短命令是：

```bash
uv sync
uv run csbox
```

安装到本地工具后，直接运行：

```bash
csbox
```

### Lab：终端实验记录

在项目目录中启动实验、查看记录并导出证据：

```bash
csbox lab start "计算机网络实验一"
csbox lab list
csbox lab review
csbox lab export <SESSION> --output evidence --theme dark
```

`lab start` 选择配置的 Shell，录制 asciicast v3，并维护 terminal emulator、Capture 和 session metadata。`F12 Capture` 是唯一正式的 Lab Capture 快捷键；CSBox 只报告实际观察到的匹配输入，不修改宿主、Shell 或编辑器配置。无法实时 Capture 时，可以在 Review 中补录。Lab export 会生成 PNG、`evidence.md` 和 `session.cast`；有可信命令时还会生成 `commands.txt`。

### API：场景、运行和 Evidence

API 场景是项目 `.csbox/api/scenarios/` 下的 TOML 文件。CLI 支持列出运行、导入 OpenAPI 3.x 模板、运行场景以及导出脱敏 PNG/Markdown/JSON Evidence：

```bash
csbox api --help
csbox api import openapi.yaml --output .csbox/api/scenarios
csbox api run .csbox/api/scenarios/example.toml --plain
csbox api list --plain
csbox api export <RUN_ID> --output evidence --theme dark
```

请求、响应、变量和断言结果在保存与导出前经过脱敏；`--json` 输出使用稳定的 `schema_version: 1` 接口。API 运行需要场景文件中配置可用的 URL、变量和请求字段；README 不提供真实凭据示例。

### Check：检查项目

Check 默认只扫描，不执行构建：

```bash
csbox check --plain
csbox check --json
csbox check --build
```

它检查 README、环境文件、私钥、构建产物、缓存、日志、大文件、路径泄露、轻量 hard-coded secret 和 Git 状态，并识别常见 Node、Maven、Gradle、Python 项目。`--deep` 仅在 PATH 中存在 `gitleaks` 时运行深度扫描；缺少工具会明确显示 `SKIP` 和安装提示。

### Pack：安全打包

Pack 使用临时 staging，不改写源项目：

```bash
csbox pack --dry-run --plain
csbox pack --verify
csbox pack --output ../deliverables --verify
csbox pack --output deliverable.zip --manifest --verify
```

默认排除 `.git`、`.venv`、`.csbox`、依赖目录、构建产物、缓存、日志和已有 ZIP；真实 `.env`、私钥和不安全路径会被拒绝。`--dry-run` 不创建 ZIP；没有 `--force` 时不会覆盖已有 ZIP。

## 平台范围

- Windows PowerShell 5.1：`powershell.exe`
- PowerShell 7：`pwsh.exe`
- Linux/WSL Bash：`bash --noprofile --norc`
- Linux/WSL Zsh：`zsh -f`（系统存在时）

Linux/WSL 开发环境覆盖 Unix PTY、Bash、中文、ANSI、resize、Ctrl+C、录制、Capture、replay、PNG 和 export。Windows 的原生 ConPTY、PowerShell 5.1/7、Unicode、resize 和退出状态在 Windows CI 验证；Windows Terminal、VS Code 等宿主对快捷键的行为仍取决于本机配置。WSL 相关结论只适用于真实 WSL 环境。

## 明确边界

当前版本聚焦本地终端实验、API 场景、项目 Check、证据导出和 Pack；安装后即可从任意项目目录使用这些入口。

## License

CSBox is licensed under the Apache License 2.0 (SPDX identifier: `Apache-2.0`). See [LICENSE](LICENSE) for details.
