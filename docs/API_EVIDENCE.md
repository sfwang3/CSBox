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
