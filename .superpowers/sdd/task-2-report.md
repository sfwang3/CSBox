# CSBox v0.2 Task 2 实施报告

## 范围

仅实现 Task 2：共享安全路径校验、原子文本写入、按终端 cell 宽度换行，以及严格的 `ApiConfig`。未实现 API models 或后续任务，也未重构 Lab。

## RED / GREEN 证据

### RED

先新增 `tests/test_safe_paths.py`、`tests/test_text_layout.py`，并在 `tests/test_config.py` 增加 Task 2 配置测试。

运行：

```text
uv run pytest tests/test_safe_paths.py tests/test_text_layout.py -q
```

结果：收集阶段失败，两个目标模块尚不存在：`ModuleNotFoundError: csbox.core.safe_paths` 和 `ModuleNotFoundError: csbox.core.text_layout`。这是新增接口缺失导致的预期 RED。

另为 API 配置持久化补充了回环测试，先运行该测试得到失败：保存后读取的 `api.variables` 仍为空；随后补齐现有配置序列化逻辑。

### GREEN

运行：

```text
uv run pytest tests/test_safe_paths.py tests/test_text_layout.py tests/test_config.py -q
...................................                                      [100%]
35 passed in 0.11s

uv run ruff check src/csbox/core src/csbox/config tests/test_safe_paths.py tests/test_text_layout.py tests/test_config.py
All checks passed!

uv run ruff format --check src/csbox/core src/csbox/config tests/test_safe_paths.py tests/test_text_layout.py tests/test_config.py
21 files already formatted
```

完整回归：

```text
uv run pytest -q
317 passed, 1 skipped in 5.26s
SKIPPED: Windows 原生 ConPTY smoke 只在 Windows CI 执行
```

## 文件与行为

- `src/csbox/core/safe_paths.py`
  - `safe_relative_path` 拒绝绝对路径、盘符路径、UNC-like 路径、反斜杠混用、`~` 家目录和 `..` 路径段。
  - `atomic_write_text` 拒绝 symlink destination；使用目标目录中的临时文件、flush/fsync、`os.replace` 完成原子 UTF-8 文本写入，并清理失败临时文件。
- `src/csbox/core/text_layout.py`
  - `wrap_cells` 使用现有 wcwidth cluster 语义，支持 CJK、ASCII 与组合字符，保证返回行的 `display_width <= width`。
- `src/csbox/config/models.py`
  - 新增严格 `ApiConfig`：`variables`、默认 `response_max_bytes=262144`、默认 `timeout_seconds=10.0`，并加入 `CSBoxConfig.api`。
  - response 上限为 1 KiB 至 16 MiB，timeout 上限为 0.1 至 300 秒；所有配置模型继续 `extra="forbid", strict=True`。
- `src/csbox/config/__init__.py`
  - 导出 `ApiConfig`。
- `src/csbox/config/loader.py`
  - 配置校验错误改为仅输出配置路径、字段、错误类型和中文修复动作，不输出 Pydantic 的原始 input/context。
  - 保存配置时支持 `[api]` 与 `[api.variables]`，并验证回读。
- 测试覆盖要求中的：`计算机网络实验`、`abc中文123`、`C:\\Users\\测试用户\\桌面\\实验一`、`~/课程实验/计算机网络/实验一`、组合字符和宽度 1/2/3。

## 提交

实现提交：`519fa12b272497102b8db9d04be2d076117554a8`（`feat: add safe paths and cell layout primitives`）。

## 自审与 concerns

- 当前 `width=1` 无法容纳宽度为 2 的单个 CJK cluster；为满足硬性宽度保证，helper 会跳过不可表示的 oversized cluster，不会拆分 cluster 或生成超宽行。这是终端 cell 约束下的明确取舍。
- 原子写入检查的是 destination 本身的 symlink；未扩展为拒绝 symlink 父目录，因为 brief 只要求拒绝 symlink destinations。
- 未修改任何真实 secrets、环境文件、远程分支或远程仓库。

## Fix（review findings）

### Finding 1：symlink 父目录可重定向原子写入（Critical）

- 文件：`src/csbox/core/safe_paths.py`、`tests/test_safe_paths.py`
- 修复：`atomic_write_text` 在创建父目录前与后，逐一检查目标路径的所有父目录组件；任一组件为 symlink 时抛出 `ValueError`。保留嵌套目录创建及同目录 temporary file + `os.replace` 的原子替换行为。
- 回归：新增 symlink 父目录测试，确认不会在重定向目录创建输出文件。

### Finding 2：超宽 cluster 被静默丢弃（Important）

- 文件：`src/csbox/core/text_layout.py`、`tests/test_text_layout.py`
- 修复：当单个 wcwidth cluster 的显示宽度超过请求宽度时，`wrap_cells` 在产生部分结果前抛出明确的 `ValueError`，不再丢失文本。组合字符行为保持不变；可表示输入继续满足 `display_width(line) <= width`。
- 回归：新增 CJK `"中"` 在 width=1 时的拒绝测试；宽度不变量测试仅覆盖可表示输入，并保留 ASCII width=1 覆盖。

### 验证命令与准确输出

```text
uv run pytest tests/test_safe_paths.py tests/test_text_layout.py tests/test_config.py -q
....................................                                     [100%]
36 passed in 0.11s

uv run ruff check src/csbox/core src/csbox/config tests/test_safe_paths.py tests/test_text_layout.py
All checks passed!

uv run ruff format --check src/csbox/core src/csbox/config tests/test_safe_paths.py tests/test_text_layout.py
20 files already formatted
```

## Fix 2（review finding）

### Finding：canonicalization 后仍使用原始父目录（Critical）

父目录在创建和 symlink 检查后，`NamedTemporaryFile` 与 `os.replace` 仍使用原始的、可能含可重定向组件的 `destination.parent`，产生 TOCTOU redirect 窗口。

### 文件与修复

- `src/csbox/core/safe_paths.py`
  - 保留 destination symlink 拒绝和创建前/后的父目录 symlink 检查。
  - 第二次检查后，以 `resolve(strict=True)` 获取 canonical real parent；临时文件创建和最终 replace 的 destination 都从该 canonical parent 派生。
- `tests/test_safe_paths.py`
  - 新增直接不变量测试：使用包含 `staging/..` 的 destination，并 monkeypatch `NamedTemporaryFile`、`os.replace`，确认两者均接收 canonical parent；保留原有嵌套目录行为覆盖。

### RED / GREEN

聚焦测试在修复前失败，确认 `NamedTemporaryFile` 收到未规范化的 `.../staging/../nested`，而非 canonical `.../nested`。修复后：

```text
uv run pytest tests/test_safe_paths.py -q
...........                                                              [100%]
11 passed in 0.02s
```

### 验证命令与准确输出

```text
uv run pytest tests/test_safe_paths.py tests/test_text_layout.py tests/test_config.py -q
....................................                                     [100%]
36 passed in 0.10s

uv run ruff check src/csbox/core src/csbox/config tests/test_safe_paths.py tests/test_text_layout.py
All checks passed!

uv run ruff format --check src/csbox/core src/csbox/config tests/test_safe_paths.py tests/test_text_layout.py
20 files already formatted
```

## Fix 3（atomic-write review finding）

### Finding：resolve 前检查父链存在 TOCTOU redirect 窗口（Critical）

此前实现先检查原始父目录链、再执行 `resolve(strict=True)`。并发替换可在两步之间将原始链中的目录替换为 symlink，使 `resolve` 跟随新目标。

### 文件与修复

- `src/csbox/core/safe_paths.py`
  - `atomic_write_text` 先确保原始父目录存在，再严格解析为 `canonical_parent`。
  - 解析后才按原始、未折叠的父路径组件逐段复核 symlink；任一 symlink 都拒绝。
  - 若 `original_parent.absolute()` 与 `canonical_parent` 不同，拒绝非 canonical 父目录。
  - temporary file、`os.replace` 和异常清理仍且仅使用 `canonical_parent` 及从其派生的 destination。
- `tests/test_safe_paths.py`
  - 新增解析期间将 `staging` 替换为 symlink 的回归测试，确认会拒绝且不会在 canonical 或重定向目录写入文件。
  - 既有 canonical-dir 断言改为 canonical 输入，以符合新增的“原始绝对父目录必须等于 canonical 父目录”不变量；仍断言 temporary file 与 replace 只使用 canonical 目录。

### RED / GREEN

先新增并运行聚焦回归：

```text
uv run pytest tests/test_safe_paths.py::test_atomic_write_text_rechecks_parent_chain_after_canonicalization -q
F                                                                        [100%]
=================================== FAILURES ===================================
_____ test_atomic_write_text_rechecks_parent_chain_after_canonicalization ______

E       Failed: DID NOT RAISE ValueError

tests/test_safe_paths.py:81: Failed
=========================== short test summary info ============================
FAILED tests/test_safe_paths.py::test_atomic_write_text_rechecks_parent_chain_after_canonicalization
1 failed in 0.02s
```

最小修复后，同一聚焦测试：

```text
uv run pytest tests/test_safe_paths.py::test_atomic_write_text_rechecks_parent_chain_after_canonicalization -q
.                                                                        [100%]
1 passed in 0.01s
```

### 验证命令与准确输出

```text
uv run pytest tests/test_safe_paths.py tests/test_text_layout.py tests/test_config.py -q
.....................................                                    [100%]
37 passed in 0.10s

uv run ruff check src/csbox/core src/csbox/config tests/test_safe_paths.py tests/test_text_layout.py
All checks passed!

uv run ruff format --check src/csbox/core src/csbox/config tests/test_safe_paths.py tests/test_text_layout.py
20 files already formatted
```

## Fix 4（POSIX directory-handle security fix）

### Finding：canonical parent 仍是可重新解析的路径字符串（Critical）

Fix 3 的 temporary file 与 `os.replace` 虽然只使用 `canonical_parent`，但该值仍是
路径字符串。攻击者可以在检查后、I/O 前把父目录或祖先替换为 symlink，使后续路径解析
重定向写入。

### 文件与修复

- `src/csbox/core/safe_paths.py`
  - 在 POSIX 且 Python 提供完整 `dir_fd`、`O_DIRECTORY`、`O_NOFOLLOW` 能力时，从根目录
    FD 开始逐一用相对 component 与 `O_DIRECTORY | O_NOFOLLOW` 打开所有祖先；父目录创建
    也通过同一安全 walk 完成。
  - canonical parent 打开后，使用随机裸文件名和
    `os.open(O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW, 0o600, dir_fd=parent_fd)` 创建临时
    文件，通过 `os.fdopen` 写入并 flush/fsync。
  - replace 前使用 `os.stat(destination.name, dir_fd=parent_fd, follow_symlinks=False)` 检查
    目标；临时文件与目标均以同一个 parent FD 下的裸文件名传给 `os.replace`。
  - 异常时仅清理由本次成功创建的临时文件，并以 `os.unlink(name, dir_fd=parent_fd)` 清理；
    所有文件与目录 FD 在成功和失败路径都会关闭。
  - Windows 或其他缺少完整 openat-style 能力的平台继续使用 canonical path fallback；其
    docstring 明确说明静态 symlink 检查无法固定父目录、不能消除并发替换窗口。
- `tests/test_safe_paths.py`
  - POSIX-only 回归验证每个 ancestor component 都通过带 `O_NOFOLLOW` 的相对 `dir_fd`
    打开，临时创建与 replace 使用同一个 parent FD，并保持同目录原子替换。
  - 覆盖祖先在打开时变成 symlink、父目录在 fsync 后被换成 symlink、目标 symlink 在
    replace 前出现、随机临时名碰撞，以及无 redirect/temporary/secret leakage。
  - 增加强制 fallback 的正常嵌套写入测试，保留非 POSIX 路径的基础行为覆盖。

### TDD RED / GREEN

新增的 4 个核心目录句柄回归在实现前得到预期 RED：

```text
FFFF                                                                     [100%]
4 failed in 0.05s
```

最小实现后，同一组测试：

```text
....                                                                     [100%]
4 passed in 0.03s
```

安全自审新增随机名碰撞测试，先确认旧清理逻辑误删既有碰撞文件：

```text
F                                                                        [100%]
1 failed in 0.04s
```

加入“仅清理由本次成功创建的临时文件”状态后：

```text
.                                                                        [100%]
1 passed in 0.02s
```

### 实际执行的平台路径

```text
uv run python - <<'PY'
import platform

from csbox.core.safe_paths import _HAS_POSIX_DIRECTORY_FDS

print(f"system={platform.system()}")
print(f"release={platform.release()}")
print(f"posix_directory_fd_path={_HAS_POSIX_DIRECTORY_FDS}")
PY
system=Linux
release=6.18.33.2-microsoft-standard-WSL2
posix_directory_fd_path=True
```

本次真实平台执行的是 WSL2/Linux POSIX directory-FD 路径。fallback 通过强制能力开关的
单元测试执行；未把该测试表述为 Windows 原生验证。

### 指定验证命令与准确输出

```text
uv run pytest tests/test_safe_paths.py tests/test_text_layout.py tests/test_config.py -q
..........................................                               [100%]
42 passed in 0.12s

uv run ruff check src/csbox/core src/csbox/config tests/test_safe_paths.py tests/test_text_layout.py
All checks passed!

uv run ruff format --check src/csbox/core src/csbox/config tests/test_safe_paths.py tests/test_text_layout.py
20 files already formatted
```

附加完整回归：

```text
uv run pytest -q
........................................................................ [ 22%]
........................................................................ [ 44%]
........................................................................ [ 66%]
........................................................................ [ 88%]
....................................s                                    [100%]
=========================== short test summary info ============================
SKIPPED [1] tests/test_windows_pty_integration.py:78: Windows 原生 ConPTY smoke 只在 Windows CI 执行。
324 passed, 1 skipped in 5.24s
```
