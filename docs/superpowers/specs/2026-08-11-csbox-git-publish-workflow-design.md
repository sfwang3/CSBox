# CSBox 本地完整历史与 GitHub 公共发布机制设计

## 目标

CSBox 的本地 `main` 保留完整开发历史，包含 Superpowers/Agent 过程资料；GitHub `origin/main` 只保留可公开的项目树和公共发布历史。两套历史通过可重复的 clean publish 流程连接，日常发布不依赖 force push。

## 边界与规则

当前确认的 local-only 路径为：

- `.superpowers/**`
- `docs/superpowers/**`
- `.githooks/**`
- `scripts/checkpoint.sh`
- `scripts/install-git-hooks.sh`
- `scripts/local-only-paths.sh`
- `scripts/push_clean.sh`
- `tests/test_git_workflow.py`

这些路径继续由本地 Git 追踪，不能通过根 `.gitignore` 或子目录 ignore 取消追踪。其他文件不按文件名中的 `AI`、`agent` 或类似字样机械排除；只要它是源码、测试、README、LICENSE、CI、正常用户/开发者/架构文档或项目构建/测试/发布脚本，就属于公共项目。

## 组件

### Local checkpoint

`scripts/checkpoint.sh "message"` 在仓库根目录运行 `git add -A` 和本地 `git commit`。它先识别工作树、暂存区和未忽略未跟踪文件是否有变化；没有变化时返回 0 并跳过。脚本不调用 `git push`，也不接受隐式发布行为。

### Hook 安装与 pre-push guard

`.githooks/pre-push` 是可追踪的 hook 模板；`scripts/install-git-hooks.sh` 将当前仓库的 `core.hooksPath` 设置为 `.githooks`。hook 默认拒绝所有直接 push，并提示使用 clean publish。只有 `push_clean.sh` 在受控 push 时设置短生命周期环境变量 `CSBOX_CLEAN_PUBLISH=1` 才允许本次 push；`--no-verify` 仍属于 Git 用户显式绕过保护的行为，不由 hook 假装不可绕过。

### Clean publish

`scripts/push_clean.sh [message]` 要求本地工作树干净，并先刷新 `origin/main`。它使用临时 `GIT_INDEX_FILE` 从当前 `HEAD` 读取完整 tree，再从临时 index 中移除两条 local-only 路径，写出公共 tree。它检查 README、Apache-2.0 `LICENSE`、`pyproject.toml`、`uv.lock`、CI、`src/` 和 `tests/` 等公共交付物存在，然后以当前 `origin/main` 为唯一父提交，通过 `git commit-tree` 生成一个公共发布提交。发布提交保存在临时 ref 中，使用 `CSBOX_CLEAN_PUBLISH=1 git push <ref>:refs/heads/main` 普通快进更新远端，最后删除临时 ref。工作区、当前 `main`、local-only 文件和本地开发历史均不被改写。

如果远端在 fetch 后发生变化，普通 push 会因非快进而失败；脚本不使用 force，因此不会覆盖并发提交。失败路径使用 trap 清理临时 index、临时 ref 和临时目录，不删除用户文件。

### 一次性公共历史净化

净化操作在完整本地备份和 refs 清单之后进行：重新 fetch，记录并锁定远端 `origin/main` 的预期旧 OID，使用独立临时 clone 将 `origin/main` 的每个公共可达提交移除两条 local-only 路径并裁剪空提交，然后只在远端旧 OID 仍未变化时执行一次 `git push --force-with-lease origin <rewritten>:main`。本地 `main` 不参与改写，也不被过滤。净化完成后重新 fetch，验证远端当前 tree、全部可达提交和公开 tags。

## 错误处理与安全性

- 缺少 commit message、未在 Git 仓库中、工作树不干净、公共必需文件缺失、远端分支缺失或 fetch/push 失败都会返回非零状态。
- clean publish 不执行 `git reset`、`git checkout`、`git clean` 或任何 force push。
- 一次性净化只接受 `--force-with-lease`，不使用无保护的 `--force`。
- local-only 路径由单一规则文件 `scripts/local-only-paths.sh` 声明，checkpoint、clean publish、测试和审计均复用该规则。

## 验证策略

自动化测试在临时 Git 仓库和本地 bare remote 中运行，覆盖 checkpoint 无变化/有变化且不 push、pre-push guard 拒绝直接 push、clean publish 的公共 tree 过滤、必需公共文件保留、发布失败对本地 `main` 无影响，以及 local-only 文件仍可本地追踪。真实 GitHub 净化完成后，额外运行 refs/tree/history 审计命令，验证 `origin/main` 和所有公开 tags 的全部可达历史没有 local-only 路径。
