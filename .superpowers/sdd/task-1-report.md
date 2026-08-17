# CSBox v0.2 Task 1 Report

## Status

完成。Task 1 的 CI Node 24 action pins、单一运行时版本来源、CLI `--version` 和 `schema_version` 基础均已实现；未实现后续 API、check 规则扩展或 pack 功能。

## Files

- `.github/workflows/ci.yml`: 更新 immutable action pins 到 `actions/checkout` v7.0.1 和 `astral-sh/setup-uv` v9.0.0，并注明 Node 24 所需 runner 最低版本；保留 2×2 OS/Python matrix。
- `pyproject.toml`: 项目版本更新为 `0.2.0`。
- `uv.lock`: 重新锁定项目 metadata 到 `0.2.0`。
- `src/csbox/__init__.py`: 使用 `importlib.metadata.version("csbox")`，metadata 不存在时使用明确的 `0.2.0.dev0` fallback。
- `src/csbox/core/schema.py`: 新增 `SCHEMA_VERSION = 1` 与 `with_schema_version(payload)`，保证 schema 字段位于 JSON payload 首位并覆盖输入中的旧值。
- `src/csbox/cli/main.py`: 新增 CLI `--version`；`check --json` 与 `pack --json` 使用 schema helper，原有字段保持不变。
- `tests/test_versioning.py`: 版本单一来源和 CLI 版本输出测试。
- `tests/test_schema.py`: schema 常量、插入位置和覆盖行为测试。
- `tests/test_check_cli.py`, `tests/test_pack_cli.py`: JSON 输出的 schema 字段回归断言。

## Commits

- `a71b816a7ad46b38c45a91968e2581ba36c0e5ef` — `chore: refresh CI actions and version source`
- 本报告随后单独提交。

## TDD evidence

### RED

Commands:

```text
uv run pytest tests/test_versioning.py tests/test_schema.py tests/test_check_cli.py tests/test_pack_cli.py -q
```

Output: collection failed with `ModuleNotFoundError: No module named 'csbox.core.schema'`。

```text
uv run pytest tests/test_versioning.py -q
```

Output: `2 failed`；版本仍为 `0.1.0`，CLI `--version` 尚未注册。

补充的 schema 顺序/覆盖测试在第一次实现后仍按预期失败：输入的 `schema_version: 99` 未被覆盖，结果为 `1 failed`。

### GREEN

Focused command:

```text
uv lock && uv run pytest tests/test_versioning.py tests/test_schema.py tests/test_cli.py tests/test_check_cli.py tests/test_pack_cli.py -q
```

Output:

```text
Resolved 29 packages in 1.11s
Updated csbox v0.1.0 -> v0.2.0
9 passed in 0.30s
```

修正 schema 字段顺序/覆盖行为后再次运行 focused suite：`9 passed in 0.26s`。

## Verification

```text
uv run pytest -q
```

`295 passed, 1 skipped in 5.21s`。唯一 skip 是 Linux 本地无法执行的 Windows 原生 ConPTY smoke。

```text
uv run ruff check .
```

`All checks passed!`

```text
uv run ruff format --check .
```

`119 files already formatted`

```text
uv lock --check && git diff --check
```

通过。

```text
uv run csbox --version
```

输出 `0.2.0`。

## Self-review

- 版本 release 值只由 `pyproject.toml` 提供；运行时从 installed metadata 读取，源码树 fallback 仅用于 metadata 不存在的开发场景。
- `with_schema_version` 返回新 dict，不修改调用方 payload；schema 字段固定为整数 `1`、位于首位，并覆盖输入中的同名字段。
- 只改变 `check --json` 和 `pack --json` 的机器输出前缀，未改动已有业务字段、plain/TUI 输出或 2×2 CI matrix。
- action 使用 brief 指定的 immutable SHA，并保留对应版本注释。
- 未读取、输出或修改 secrets；未执行破坏性 git 命令；未创建 worktree、远程分支或提交远程。

## Concerns

- 本地 Linux 无法替代 Windows 原生 ConPTY 验证；现有全量测试保持该测试为明确 skip，Windows CI 将覆盖它。
- 报告提交会在实现提交之后形成单独 commit，不影响 Task 1 实现 commit 的可审阅性。

## Fix

- Finding: `lab list --json` emitted a bare list, violating the global machine-readable JSON contract requiring `schema_version: 1`.
- Files: `src/csbox/cli/main.py`, `tests/test_lab_cli.py`.
- Change: JSON output is now `{"schema_version":1,"sessions":[...]}`; plain output and all session fields remain unchanged. Tests include a focused schema assertion and updated session access.

Focused command:

```text
uv run pytest tests/test_lab_cli.py -q
```

Exact passing output:

```text
....                                                                     [100%]
4 passed in 0.16s
```

Covering verification:

```text
uv run pytest -q
```

Exact passing output:

```text
........................................................................ [ 24%]
........................................................................ [ 48%]
........................................................................ [ 72%]
........................................................................ [ 97%]
.......s                                                                 [100%]
=========================== short test summary info ============================
SKIPPED [1] tests/test_windows_pty_integration.py:78: Windows 原生 ConPTY smoke 只在 Windows CI 执行。
295 passed, 1 skipped in 5.25s
```

```text
uv run ruff check .
All checks passed!

uv run ruff format --check .
119 files already formatted

git diff --check
```
