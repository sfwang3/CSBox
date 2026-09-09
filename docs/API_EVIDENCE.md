# API Evidence

API Evidence 是已经完成脱敏的 API 场景运行视图。它由同一份 `ApiEvidence` 数据生成 PNG、Markdown 和 JSON，避免三个导出格式出现不同的请求、响应或断言内容。

## 工作流

```bash
csbox api import openapi.yaml --output .csbox/api/scenarios
csbox api run .csbox/api/scenarios/example.toml --plain
csbox api list --json
csbox api export <RUN_ID> --output evidence --theme dark
```

场景文件由用户提供静态请求结构和断言。`csbox api run` 解析 TOML、解析变量、执行请求并将脱敏结果写入 `.csbox/api/runs/`。`csbox api export` 只读取已保存的公共运行视图，不重新发送请求。

## 脱敏与输出

- Authorization、Cookie、password、token、secret 等敏感字段会使用统一 redaction policy。
- URL credentials 和敏感 query 参数不会出现在持久化结果中。
- PNG 与 Markdown 使用 CJK-aware cell wrapping；PNG 需要可用的系统 ASCII 等宽字体和 CJK fallback。
- JSON 输出使用 `schema_version: 1`，适合脚本读取；JSON 不包含原始 transport secret。

API Evidence 不是终端截图：终端实验仍使用 `csbox lab export` 生成基于 terminal cell 的证据 PNG。

## 进入统一 Evidence Set

API Evidence 进入 Evidence Set 时使用一个严格的 `ApiStepSource` 引用：

```json
{
  "source_type": "api_step",
  "run_id": "run-2026-09-01",
  "step_index": 1
}
```

`step_index` 与 `ApiEvidenceBuilder` 的既有约定一致，从 `1` 开始。Evidence Set
只保存这个引用以及用户填写的标题、图注和备注，不复制 request、response、断言或
PNG。Evidence 浏览和 Report 导出都只读取 `.csbox/api/runs/` 中已经保存、已经脱敏的
`ApiRun`；它们不会发送网络请求、重新执行场景或修改原运行记录。

解析失败时 API 步骤会保留在 Evidence Set 中，并显示受控的不可用状态。CSBox 不会
根据步骤标题猜测另一个步骤，也不会用瞬态的原始断言视图替代已保存的安全表示。
