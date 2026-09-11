# Submission Handoff

CSBox 的提交交接是一个本地、可复核的准备流程：

```text
Record → Evidence → Prepare Submission → Verify → 用户手动上传
```

CSBox 只负责准备和校验，永远不会替用户上传。界面和命令应说“已准备”或“已验证”，不说“提交成功”。课程结论、实验答案和报告正文仍由用户负责；AI 不生成这些内容。已有的 Check 和 Pack 仍可独立使用，提交交接只是复用它们来组织最后一步。

## Prepare Submission 做什么

准备提交是一次可见预检加一次协调事务。预检读取指定 Evidence Set、报告配置、项目 Check、Pack 计划和目标目录；只有预检结束后，才会在临时区域生成并验证材料，然后一次性发布到目标目录。成功的目录恰好包含：

```text
<handoff>\
├── report.docx
├── <configured-project-name>.zip
└── submission-manifest.json
```

ZIP 会经过重新打开校验，并且包含 Pack manifest。准备期间不会修改源项目、`.csbox/` 中的 Evidence 或 Profile；失败也不会留下半套公开材料。默认目标是项目目录旁的安全 sibling 目录，也可以指定自定义目标。

### 预检状态

- `READY`：没有阻塞项，可以准备。
- `WARNING`：存在需要用户复核的 WARN，但没有阻塞项；用户仍需读懂并决定是否继续。
- `BLOCKED`：有 FAIL 或其他阻塞项，必须先处理，准备不会开始。

`WARN` 不是 `PASS`，也不是 `FAIL`，不会静默消失。Check 的 `FAIL` 是阻塞事实；`SKIP` 表示该项没有执行或无法评估，不能当作通过。准备结果仍会保留警告数量和提示。

## 三个最终文件

`report.docx` 是使用用户提供的报告结构和 Evidence Set 生成的报告材料；它不是自动生成的课程结论。项目 ZIP 使用配置的输出名，由 Safe Pack 的过滤和校验规则产生。

`submission-manifest.json` 是一个小而严格的完整性收据。它记录 Evidence Set、Check 摘要、归档是否验证且包含 Pack manifest，以及两个最终文件的安全相对路径、字节大小和 SHA-256。路径不能是绝对路径、临时路径或 traversal 路径，也不包含 secrets。manifest 的 `manifest_sha256` 是对其自身排除 digest 字段后的规范内容作出的完整性承诺；它不是身份认证，也不能证明材料来自某个发布者。

## 独立 Verify

把最终目录复制或移动到另一台机器后，可以在没有源项目、Evidence、Profile 或网络的情况下独立校验：

```bash
csbox submit verify path/to/handoff --plain
```

Verify 只读取这个目录中的三个文件，重新计算文件大小和 SHA-256，并检查 manifest、相对路径、ZIP 和 Pack manifest。`PASS` 才表示这份目录通过了当前收据的完整性校验；失败时会明确输出错误，不会把 `SKIP` 或 `WARN` 当成 PASS。

## 目标目录和刷新

不存在的目标会安全创建。已有目标如果不是 CSBox 明确拥有、内容未被修改的 handoff，永远不会被盲目覆盖；应改用新的 `--output`。`--force` 只允许刷新可证明由 CSBox 拥有且仍未变化的 handoff，不能把它当成任意覆盖开关。预检和发布之间若源材料、Profile、Pack 输入或目标状态发生变化，准备会停止并要求重新预检。

## 本地优先边界

准备交接不会上传 LMS、云盘或其他服务，也不需要网络。完成后请用户自行打开课程要求的系统，按教师要求手动上传 `report.docx` 和项目 ZIP；`submission-manifest.json` 是校验收据，除非任课教师要求，否则不必上传。CSBox 说的是“prepared”，不是“submitted”。源项目、Evidence、Profile 和原有 Check/Pack 工作流保持可继续使用。
