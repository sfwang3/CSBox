# Task 4 报告：Replay checkpoints、PNG renderer 与 lab export

## 结果

- 初始实现 commit：`3bfaf3f4728855e00f3d3f47a0a022ac09d345d4`（`feat: add terminal replay and evidence export`）
- 独立审查后追加修复 commit：
  - `d2c64c679210fb5bb089da839e9cd51c80222d48`（`fix: preserve terminal state across replay checkpoints`）
  - `152a141bd17360c560c5640eff7315a19a02b2ea`（`fix: harden lab evidence export boundaries`）
  - `118c65a703a5043edebaa2f543fb3999655d9375`（`fix: validate rendered evidence and refresh exports`）
  - `c8deedcc035c1de066acde0922c48ae22b59aa08`（`fix: reject uncovered custom glyphs`）
- 仅修改 Task 4 的 replay/renderer/export 相关代码、测试与固定 cast；未触碰 Task 5+ CLI/TUI/proxy。
- 未提交字体、PNG 或其他生成产物。

## RED 证据

### Replay 初始 RED

命令：

```text
uv run pytest tests/test_replay.py -q
```

原始关键结果：

```text
E   ModuleNotFoundError: No module named 'csbox.lab.replay'
ERROR tests/test_replay.py
1 error in 0.10s
```

### Renderer 初始 RED

命令：

```text
uv run pytest tests/test_renderer.py -q
```

原始关键结果：

```text
E   ModuleNotFoundError: No module named 'csbox.lab.fonts'
ERROR tests/test_renderer.py
1 error in 0.13s
```

### Exporter 初始 RED

命令：

```text
uv run pytest tests/test_lab_export.py -q
```

原始关键结果：

```text
E   ModuleNotFoundError: No module named 'csbox.lab.exporter'
ERROR tests/test_lab_export.py
1 error in 0.12s
```

### 无效 checkpoint 内容回归 RED

在有效 checkpoint JSON 中把最近 snapshot 的首个中文 cell 从“中”改成“坏”，再 seek 6 秒。

命令：

```text
uv run pytest tests/test_replay.py::test_invalid_or_stale_checkpoint_index_is_rebuilt_without_touching_cast -q
```

原始关键结果：

```text
E       AssertionError: assert False
E        +  where False = '坏文    '.startswith('中文')
1 failed in 0.11s
```

根因是旧实现只校验 checkpoint 结构与 cast 指纹，没有检测 checkpoint 内容的意外损坏。加入 canonical payload SHA-256 checksum，并校验完整 checkpoint 调度后，该用例转为 GREEN。checksum 仅是完整性/意外损坏检测，不是认证机制，也不抵御能重算 checksum 的本地写入者。

## 独立审查修复的 RED / GREEN 证据

### 1. Checkpoint continuation、parser 边界与 timeline

先新增 SGR 延续、insert mode、autowrap、saved cursor、margins、tabstop、charset、split CSI、INPUT/MARK/EXIT、事件间 seek、clamp 与 trailing unknown 用例。

RED 原始结果：

```text
F.FFFFFFFFF.............F                                               [100%]
11 failed, 14 passed
```

失败覆盖了 checkpoint restore 丢 continuation state、split CSI 在 parser 非安全边界落 checkpoint、非画面事件不推进时间，以及 seek 返回上一个画面事件时间等边界。

最小实现后聚焦 GREEN：

```text
.......................................................                  [100%]
55 passed in 0.57s
```

实现采用 versioned、纯领域 dataclass 保存 cursor attrs、modes、margins、tabstops、charset、savepoints 等 continuation state；仅在 pyte parser 和 UTF-8 decoder 均处于安全边界时建立 checkpoint。checkpoint 仍是可删除重建的派生索引，seek 不从零重放验证 checksum。

### 2. Export escaping、路径预算、commands 与 session source

先新增 raw HTML/Markdown、`C#`、括号/百分号链接、超长 emoji/CJK、控制序列命令、bidi、session sidecar symlink 用例。

RED 原始结果：

```text
...FFFF                                                                  [100%]
4 failed, 3 passed
```

GREEN 原始结果：

```text
.......                                                                  [100%]
7 passed
```

实现对标题做 Markdown/HTML escape，对图片 URL 的保留字符做 UTF-8 percent-encoding；文件名同时按 UTF-8 字节和 Windows UTF-16 component 预算截断并附短 hash。`commands.txt` 只接受可打印单行且不含 Unicode `C*` 控制/格式字符的命令；session root/cast/captures/backup/metadata/checkpoints 均拒绝 symlink 与越界 resolve。

### 3. Glyph coverage、原子 PNG、force 清理与历史时间

先新增实际 CJK/缺字 coverage、缺失 CJK 字体非 skip 错误路径、PNG 写失败保护旧文件、bold 结构、stale evidence 清理、metadata 时间及坏 metadata fallback 用例。

RED 原始结果：

```text
.....FFFF......FF                                                        [100%]
6 failed, 11 passed
```

GREEN 原始结果：

```text
.................                                                        [100%]
17 passed
```

实现按 snapshot 的实际文本逐 cell 选择主字体或 fallback；没有候选覆盖时抛中文 `FontResolutionError`，不会生成 tofu 作为成功结果。PNG 通过同目录临时文件、fsync、`os.replace` 原子替换；force 只从旧 `evidence.md` 识别并删除 CSBox 生成的 stale PNG，保留无关文件。有效 metadata 使用 `started_at + capture.timestamp`，缺失/坏 metadata 明确 warning 后回退 `created_at`。bold 使用同色 1px stroke，并以结构测试验证。

### 4. Private Use 自审边界

参数化 Extension-B 与 Private Use 字符后，Private Use 稳定 RED：

```text
.F                                                                       [100%]
1 failed, 1 passed in 0.12s
E       Failed: DID NOT RAISE FontResolutionError
```

根因是 `font_supports_text` 把所有 Unicode `C*` category 都视作无需 glyph。最小修复为只跳过非绘制的 `Cc`/`Cf`，`Co` 必须实际命中字形；GREEN：

```text
.........                                                                [100%]
9 passed in 0.16s
```

## 实现摘要

### Replay 与 checkpoint

- `AsciicastV3Reader` 现在提供 header 后的 `data_offset`，每个 `CastEvent` 提供其源行结束后的 `cast_offset`。
- `TerminalEmulator.restore(snapshot)` 在 pyte adapter 内恢复尺寸、cell、原始 cursor 与 SGR attrs、IRM/DECAWM/DECOM 等 modes、margins、tabstops、charset、saved cursor、标题、UTF-8 mode 和相对时间，并重建 byte stream；领域 snapshot 不泄露 pyte 类型。
- `ReplayService.seek(t)` 每次从不晚于目标时间的最近 checkpoint 建立新 emulator；向后 seek 不复用前一次 seek 的可变状态。
- checkpoint 达到 5 秒或 500 个事件阈值后，仅在 parser/UTF-8 decoder 安全边界建立，保存 event index、cast byte offset、relative time 和 versioned 领域 snapshot。
- `checkpoints.json` 使用 schema version、cast size/SHA-256、意外损坏 checksum、同目录临时文件、fsync 和 `os.replace`；损坏、陈旧、checksum 不匹配或调度/边界不完整时从 cast 重建，且不修改 `session.cast`。checksum 不作为恶意篡改认证边界。
- 所有完整 `TerminalEvent`（包括 INPUT/MARK/EXIT）都会推进 emulator relative time；seek 返回 clamp 后目标历史时刻。Reader 保留 trailing unknown interval，但不会把 unknown payload 送入 screen。
- mixed cast 覆盖 0 秒、事件间、resize 后、exit 后、backward seek、未知事件间隔与 late-seek checkpoint tail。

### 字体与 PNG renderer

- `FontResolver(explicit=None, candidates=None)` 按稳定顺序查找 Cascadia/Consolas、Microsoft YaHei/SimSun、DejaVu/Noto CJK，以及 WSL 的 `/mnt/c/Windows/Fonts`。
- 检查 ASCII 等宽 advance、基础中文与 snapshot 实际非 ASCII glyph 覆盖；逐字符 fallback 后仍缺字时给出中文配置错误，不生成 tofu/乱码证据。
- cell width 取 ASCII mono advance 与 CJK fallback 半 advance 的上界；glyph x 坐标只由 column 决定。
- renderer 支持 dark/light、ANSI/truecolor、CJK 双宽、combining 同 origin、reverse、underline、strikethrough 与可验证的 bold；PNG 使用同目录原子替换，失败不破坏旧文件。
- 测试注入当前系统字体，只断言尺寸、背景、cell origin 和绘制结构，不使用脆弱的全像素 golden。

### Lab export

- `LabExporter.export(session, destination, force=False)` 接受 `SessionPaths` 或 session 目录。
- capture 按已有稳定顺序导出为 `evidence/01-*.png`；slug 过滤非法/控制/穿越/Windows 保留名，并按 UTF-8/UTF-16 安全 component 预算截断、附 hash 防碰撞。
- 标题同时做 Markdown/HTML escape；相对图片链接对 `#`、`%`、括号等 reserved ASCII percent-encode，并保留可读中文。原样复制 `session.cast`。
- 仅当 CaptureStore 中存在可打印、单行、无 ESC/C0/C1/backspace/bidi/其他 Unicode category C 控制的可信 command 时生成 `commands.txt`；全未知时省略，不猜测命令。
- 默认拒绝已有导出目录；只有显式 `force=True` 才覆盖已知输出，并清理旧 CSBox evidence PNG、保留无关文件。拒绝 session 原目录、输出 symlink，以及 session source/sidecar symlink 或 resolve 越界。
- 历史时间优先使用有效 `metadata.started_at + capture.timestamp`；metadata 缺失/损坏时返回 warning 并明确回退到 `capture.created_at`。

## 最终验证原始结果

### Task 4 + recorder/screen/captures 聚焦测试

```text
$ uv run pytest tests/test_replay.py tests/test_renderer.py tests/test_lab_export.py tests/test_recorder.py tests/test_terminal_screen.py tests/test_captures.py -q
........................................................................ [ 98%]
.                                                                        [100%]
73 passed in 1.04s
```

### 全量 pytest

```text
$ uv run pytest -q
........................................................................ [ 33%]
........................................................................ [ 66%]
........................................................................ [ 99%]
.                                                                        [100%]
217 passed in 4.39s
```

### Ruff 全仓 check 与 scoped format

```text
$ uv run ruff check src tests
All checks passed!
$ uv run ruff format --check src/csbox/lab tests/test_replay.py tests/test_renderer.py tests/test_lab_export.py tests/test_recorder.py tests/test_terminal_screen.py tests/test_captures.py
18 files already formatted
```

### Diff 检查

`git diff --check 3bfaf3f..HEAD`：exit 0，无输出。

### Ruff 全仓 format（已知范围外差异）

```text
$ uv run ruff format --check src tests
unformatted: File would be reformatted
  --> src/csbox/config/paths.py:15:18
1 file would be reformatted, 67 files already formatted
```

## 未验证与限制

- 未在原生 Windows 上执行字体发现/渲染；Windows Cascadia/Consolas/微软雅黑/宋体路径只做了代码覆盖。本机 WSL 使用注入的 DejaVu Sans Mono 与 Noto Sans CJK 完成测试。
- 按 brief 不使用全像素 PNG golden；没有承诺不同 Pillow/FreeType 版本的 glyph 像素完全相同，只保证 cell grid、尺寸、主题和属性布局。
- italic 仍只保留 snapshot 属性，renderer 未做人工倾斜/独立 italic font 选择；bold 已实现并验证。
- checkpoint checksum 仅检测意外损坏；具备本地写权限且能重算 checksum 的参与者不在此信任边界内。实现不会每次从 0 重放校验，以保留 checkpoint 优化。
- 全仓 `uv run ruff format --check src tests` 报告一个 Task 4 之前已存在、且不在本次范围内的格式差异：`src/csbox/config/paths.py:15`。未做无关修改；Task 4 scoped format 全部通过。

---

# Task 4 报告补充：API variables 与 TOML scenario parser

## RED 证据

新增 `tests/test_api_variables.py` 与 `tests/test_api_scenario.py` 后执行：

```text
uv run pytest tests/test_api_variables.py tests/test_api_scenario.py -q
```

初始结果为预期 RED：`csbox.api.variables` 和 `csbox.api.scenario` 均不存在，pytest 在收集阶段报 `ModuleNotFoundError`（2 errors）。

后续补充严格大写 method、TOML 行号、整数 timeout、非字符串变量值和三段式诊断的回归断言；每一轮先观察失败，再完成最小实现。

## GREEN 证据

```text
uv run pytest tests/test_api_variables.py tests/test_api_scenario.py -q
14 passed in 0.08s
```

覆盖项目 < 环境 < scenario < CLI 的变量优先级、`CSBOX_VAR_` 环境后备、不可变 resolution、缺失/TODO/表达式占位符、递归容器插值与 JSON 非字符串值保留；以及 TOML JSON/form/multipart、bearer、超时、redirect/TLS、断言、重复/冲突/未知或脚本式配置、无效 method 和安全中文错误上下文。

## 最终验证

```text
uv run pytest -q
380 passed, 1 skipped in 5.28s

uv run ruff check src/csbox/api
All checks passed!

uv run ruff format --check src/csbox/api/variables.py src/csbox/api/scenario.py src/csbox/api/models.py tests/test_api_variables.py tests/test_api_scenario.py
5 files already formatted

git diff --check
exit 0，无输出
```

实现没有导入 Textual 或 HTTPX，也没有记录解析后的变量值或 secret。`ApiScenario` 仅保存未解析的 scenario variables；`ApiRequest` 增加 typed multipart 部件以保留 TOML 语义，未实现 transport、runner 或 CLI。

---

# Task 4 review fixes 补充

## RED 证据

先新增 positional public signature、scenario/environment/CLI 来源值、assertion allowlist 与直接构造 multipart 的回归测试，再运行生产代码改动前的 focused tests：

```text
$ uv run pytest tests/test_api_variables.py tests/test_api_scenario.py tests/test_api_models.py -q
ImportError: cannot import name 'ApiMultipartPart' from 'csbox.api.models'

$ uv run pytest tests/test_api_variables.py -q
17 failed, 10 passed in 0.12s

$ uv run pytest tests/test_api_scenario.py -q
4 failed, 9 passed in 0.10s
```

失败分别证明旧 resolver positional 顺序与计划不符、来源模板可残留在 resolved values/interpolation 中、非法 assertion type 被接受，以及 multipart 缺少严格公开模型。

## 实现与 GREEN

- `resolve_variables(name_set, project, scenario, environ, cli)` 使用精确计划签名，同时保持 project < environment < scenario < CLI。
- 最终胜出的来源值在进入 `VariableResolution.values` 前验证：`TODO*` 和仍含静态 placeholder 的值记为 missing；表达式或其他不支持模板抛出不含 raw value 的 `ApiConfigError`。`interpolate` 同样拒绝 replacement placeholder residue。
- `ScenarioLoader` 的第一版 assertion type 只允许精确小写 `status`、`header`、`json_path`、`body`；现有严格键集合继续拒绝脚本式额外字段，未实现 evaluator。
- 新增并公开 strict `ApiMultipartPart(name, value)`；`ApiRequest.multipart` 使用该模型，直接构造会拒绝缺键、额外键或错误类型，TOML 加载后的 JSON 表达仍是 `{name, value}`。

```text
$ uv run pytest tests/test_api_variables.py tests/test_api_scenario.py tests/test_api_models.py -q
60 passed in 0.11s
```

## 最终验证

```text
$ uv run pytest -q
407 passed, 1 skipped in 5.35s

$ uv run ruff check .
All checks passed!

$ uv run ruff format --check .
132 files already formatted

$ git diff --check
exit 0，无输出
```

唯一 skip 是只在 Windows CI 执行的原生 ConPTY smoke。此次 review fixes 未实现 transport、runner、assertion evaluator 或 CLI，也未创建 worktree。
