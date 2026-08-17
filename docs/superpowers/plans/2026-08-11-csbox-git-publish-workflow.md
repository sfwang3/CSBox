# CSBox Git 发布隔离机制 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 保留 CSBox 本地完整开发历史，同时以不含 Agent/Superpowers 过程资料的独立公共提交安全更新 GitHub `origin/main`。

**Architecture:** 以 `scripts/local-only-paths.sh` 维护唯一排除规则；checkpoint 只提交本地历史；`.githooks/pre-push` 默认拒绝直接 push。clean publish 使用临时 index 从当前 `HEAD` 构造公共 tree，再以 `origin/main` 为父提交通过临时 ref 普通快进推送，不改写本地 `main`。

**Tech Stack:** Bash、Git plumbing commands (`read-tree`/`write-tree`/`commit-tree`/`update-ref`)、Python `pytest` 临时仓库测试、现有 `uv` 工具链。

## Global Constraints

- local-only 路径固定为 `.superpowers/**`、`docs/superpowers/**`、`.githooks/**`、四个 Git 隔离脚本和 `tests/test_git_workflow.py`；其他公共源码、测试、文档和脚本不按字样猜测排除。
- 上述 local-only 文件必须能被普通 `git add -A` 和 `git commit` 追踪。
- checkpoint 绝不调用 push。
- 普通直接 push 默认由 pre-push guard 拒绝；clean publish 仅在脚本短生命周期授权下推送。
- 日常 clean publish 不使用 force；一次性远端净化只允许 `--force-with-lease`。
- 公开仓库使用 Apache License 2.0，SPDX identifier 为 `Apache-2.0`；没有实际第三方 attribution/NOTICE 内容时不创建 NOTICE。
- 修改后运行相关 pytest、Ruff、shell smoke 和 GitHub refs/tree/history 审计；无法验证的平台行为必须明确说明。

---

### Task 1: 公共许可、local-only 追踪规则与测试基线

**Files:**
- Create: `LICENSE`
- Modify: `README.md`
- Modify: `.gitignore`
- Modify: `.superpowers/sdd/.gitignore`
- Create: `tests/test_git_workflow.py`

**Interfaces:**
- Produces: 临时 Git 仓库测试辅助函数，以及 local-only 路径仍能被本地追踪的失败测试。

- [ ] **Step 1: Write the failing tests**

在 `tests/test_git_workflow.py` 中先写临时 bare remote/repository helpers，并测试当前 checkout 的规则、Apache license 和 local-only 追踪要求；测试应在实现前因为缺少 `LICENSE`、根 ignore 规则和脚本而失败。

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_git_workflow.py -q`

Expected: FAIL，失败原因包含缺少 Apache license 或 local-only 路径仍被 ignore。

- [ ] **Step 3: Add Apache-2.0 and fix tracking rules**

加入标准 Apache License 2.0 全文，README 改为说明项目按 Apache-2.0 发布并移除“未附带 LICENSE”表述；根 `.gitignore` 移除 `.superpowers/`，子目录 `.superpowers/sdd/.gitignore` 不再用 `*` 忽略过程资料。

- [ ] **Step 4: Run tests to verify the baseline passes**

Run: `uv run pytest tests/test_git_workflow.py -q`

Expected: 当前许可和追踪规则测试 PASS；脚本行为测试仍保持失败，等待后续任务实现。

### Task 2: checkpoint、hook 模板和安装器

**Files:**
- Create: `scripts/local-only-paths.sh`
- Create: `scripts/checkpoint.sh`
- Create: `.githooks/pre-push`
- Create: `scripts/install-git-hooks.sh`
- Modify: `tests/test_git_workflow.py`

**Interfaces:**
- `scripts/checkpoint.sh <message>`：返回 0；有变化时本地 commit，无变化时打印 skip。
- `scripts/install-git-hooks.sh`：将仓库 local `core.hooksPath` 设为 `.githooks`。
- `.githooks/pre-push`：未设置 `CSBOX_CLEAN_PUBLISH=1` 时返回非零并拒绝直接 push。

- [ ] **Step 1: Add failing checkpoint and guard tests**

覆盖有变化、无变化、提交包含 `.superpowers/**`/`docs/superpowers/**`、checkpoint 后 bare remote OID 不变、安装 hook 后普通 push 返回非零且 remote OID 不变。

- [ ] **Step 2: Run the focused tests**

Run: `uv run pytest tests/test_git_workflow.py -q -k 'checkpoint or push_guard'`

Expected: FAIL，因为脚本和 hook 尚未存在。

- [ ] **Step 3: Implement minimal scripts and hook**

所有脚本使用 `set -euo pipefail`，通过脚本自身目录解析仓库根；checkpoint 使用 `git add -A`/`git commit`，不出现 `git push`；hook 只允许 clean publish 环境变量；安装器只修改当前仓库的 local Git config。

- [ ] **Step 4: Run the focused tests**

Run: `uv run pytest tests/test_git_workflow.py -q -k 'checkpoint or push_guard'`

Expected: PASS。

### Task 3: clean publish 公共视图

**Files:**
- Create: `scripts/push_clean.sh`
- Modify: `tests/test_git_workflow.py`
- Modify: `README.md`

**Interfaces:**
- `scripts/push_clean.sh [message]`：在干净工作树上从当前 `HEAD` 构造不含 local-only 路径的公共提交，并以普通非 force push 更新 `origin/main`。

- [ ] **Step 1: Add failing clean publish tests**

使用临时 clone/bare remote 构造源码、tests、README、LICENSE、CI、pyproject、uv.lock、公共 docs 和两条 local-only 路径；断言发布后公共 tree 保留前者、排除后者、当前本地 `main` OID 不变，并在远端并发推进时失败而不改变本地 ref。

- [ ] **Step 2: Run the focused tests**

Run: `uv run pytest tests/test_git_workflow.py -q -k clean_publish`

Expected: FAIL，因为 `push_clean.sh` 尚未存在。

- [ ] **Step 3: Implement the temporary-index publisher**

脚本先要求工作树干净并 `git fetch origin main`，检查公共必需路径；设置临时 `GIT_INDEX_FILE`，`git read-tree HEAD` 后按 `scripts/local-only-paths.sh` 删除 local-only pathspec，`git write-tree` 写出公共 tree；以 `origin/main` 为父提交调用 `git commit-tree`，用临时 ref 让 `CSBOX_CLEAN_PUBLISH=1 git push <temp-ref>:refs/heads/main`，trap 清理 index/ref/目录。

- [ ] **Step 4: Run the focused tests**

Run: `uv run pytest tests/test_git_workflow.py -q -k clean_publish`

Expected: PASS。

### Task 4: 文档、安装与全量本地验证

**Files:**
- Modify: `README.md`
- Modify: `tests/test_git_workflow.py`

**Interfaces:**
- README 提供 checkpoint、hook 安装、clean publish 命令和日常禁止直接 push 的说明。

- [ ] **Step 1: Add documentation/smoke assertions**

测试脚本帮助输出、README 命令和 hook/template 路径存在，并确认仓库没有凭空新增 NOTICE。

- [ ] **Step 2: Run all relevant checks**

Run: `uv run pytest tests/test_git_workflow.py -q`; `uv run ruff check scripts tests/test_git_workflow.py`; `uv run ruff format --check tests/test_git_workflow.py`; `bash -n scripts/checkpoint.sh scripts/install-git-hooks.sh scripts/push_clean.sh .githooks/pre-push`。

Expected: 所有命令返回 0。

### Task 5: 本地完整 checkpoint 与备份

**Files:**
- Modify: all intended local tracked/untracked development files through `git add -A`

- [ ] **Step 1: 安装 hook 并确认现状**

Run: `./scripts/install-git-hooks.sh`; `git status --short --ignored`; `git rev-parse HEAD`。

- [ ] **Step 2: 创建完整备份**

保存 `git bundle create` 的 `--all` bundle、refs manifest、remote manifest 和工作区 local-only 文件清单到仓库外的任务备份目录；不得把备份文件加入公共发布 tree。

- [ ] **Step 3: 本地 checkpoint**

Run: `./scripts/checkpoint.sh "chore: establish local and public git history workflow"`；验证提交中包含 local-only 资料、没有执行 push，记录新的本地 `main` HEAD。

### Task 6: 一次性净化当前 GitHub 历史

**Files:**
- No source files; operate only on an isolated temporary clone and remote refs.

- [ ] **Step 1: Re-fetch and lock expected remote state**

Run `git fetch --prune --tags origin` immediately before rewrite and compare `git rev-parse origin/main` plus remote heads/tags against the audit manifest. 如果 OID、公开 branch 或 tags 发生变化，停止，不覆盖未知提交。

- [ ] **Step 2: Rewrite in an isolated temporary clone**

仅在临时 clone 中从 `origin/main` 构造重写 ref，使用 Git index filter 移除 `.superpowers` 和 `docs/superpowers` 并裁剪空提交；保留所有正常源码、测试、README、CI、公共文档和后续需要的历史。

- [ ] **Step 3: Push once with force-with-lease**

显示待更新的 `refs/heads/main` 旧 OID、新 OID 和变更摘要；使用 `git push --force-with-lease=refs/heads/main:<audited-old-oid> origin <rewritten>:refs/heads/main`，禁止 `--force`。

- [ ] **Step 4: Re-fetch and audit public refs/history**

重新 fetch，验证 `origin/main` 当前 tree、`git rev-list --objects origin/main`、每个公开 tag 的全部可达历史均无 local-only path；检查必要公共路径仍存在。

### Task 7: 发布当前本地状态并最终验收

**Files:**
- No additional feature files; use the clean publisher and verification commands.

- [ ] **Step 1: Publish current local state**

Run: `./scripts/push_clean.sh "publish: current CSBox development state"`；验证远端更新为普通快进，当前本地 `main` OID 不变。

- [ ] **Step 2: Run final verification**

Run full `uv run pytest`; full relevant Ruff/format/shell checks; local-only tracking checks; public tree/history checks; `git status --short --branch`。

- [ ] **Step 3: Review the diff and commit boundary**

检查独立逻辑提交只包含 Git 发布隔离、Apache-2.0、README、测试和必要本地过程资料；确认没有 Task 10 API Evidence 源码变更。
