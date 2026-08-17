# Task 3 Report: Session recording, screen model, and Capture persistence

## Status

完成。实现基于基线 `849d276`，提交：`b82bdf2`（`feat: add lab session recording and captures`）。

## RED evidence

1. Models / recorder
   - 命令：`uv run pytest tests/test_session_models.py tests/test_recorder.py -q`
   - 结果：2 个 collection errors；缺少 `csbox.lab.models` 和 `csbox.lab.recorder`，符合预期 RED。
2. Screen adapter
   - 命令：`uv run pytest tests/test_terminal_screen.py -q`
   - 结果：1 个 collection error；缺少 `csbox.lab.screen`，符合预期 RED。
3. CaptureStore / dispatcher
   - 命令：`uv run pytest tests/test_captures.py -q`
   - 结果：1 个 collection error；缺少 `csbox.lab.captures`，符合预期 RED。
4. 自审与独立 review 补充 RED
   - 命令：定向运行 UTC metadata、NaN/Infinity interval、CJK combining、末列宽字符、snapshot 尺寸一致性 5 个测试。
   - 结果：`5 failed`。旧实现接受 naive datetime 与非有限 interval；pyte 把 CJK combining mark 放进 continuation cell、把宽字符放入末列形成不完整几何；CaptureRecord 接受 rows/columns 与 snapshot 不一致。
   - 修正后同一命令：`5 passed in 0.09s`。

Recorder bounded-close/writer-failure 回归测试最初因 monkeypatch 未保留 `staticmethod` 绑定而错误失败；修正测试装配后验证 close 在 50ms 配置下有界返回，并在 writer 释放后通过 `RecorderError.__cause__` 传播底层 `OSError`。这不是生产 RED，未将测试装配错误计作功能故障。

## GREEN progression

- Models / recorder 初始 GREEN：`7 passed in 0.06s`。
- Screen 初始 GREEN：`5 passed in 0.06s`。
- CaptureStore / dispatcher 初始 GREEN：`5 passed in 0.18s`。
- 审查修正后的 Task 3 focused GREEN：`22 passed in 0.26s`。

## Implemented

- `src/csbox/lab/models.py`
  - `SessionMetadata`：稳定 JSON aliases、`running` 默认状态、必需 session/shell/terminal/cwd/version 元数据，UTC-aware 时间校验。
  - `SessionPaths`：固定 `session.cast`、`metadata.json`、`captures.json`、`checkpoints.json` sidecar 路径。
  - frozen `CaptureRecord`：UUID id、UTC created time、relative timestamp、rows/columns/cwd/optional command、immutable domain snapshot，并校验 snapshot 几何一致性。
- `src/csbox/lab/recorder.py`
  - `AsciicastV3Recorder`：bounded `queue.Queue`、单 writer thread、逐行 append+flush、有限 enqueue/close timeout、幂等 close 状态、显式 `RecorderError` 与 writer cause 传播。
  - v3 header 仅保留 `SHELL`/`TERM`；事件映射为 `o/i/r/m/x`，CAPTURE 与 MARK 都写 marker；EXIT `None` 规范为 `0`。
  - OUTPUT/INPUT 使用各自 incremental UTF-8 decoder；相邻 TerminalEvent interval 以毫秒误差扩散量化，避免累计漂移。
  - `AsciicastV3Reader` 保留损坏行前后所有有效 NDJSON；注释跳过；截断尾行、孤立 JSON/shape 损坏、未知 code、NaN/Infinity interval 均返回带行号 warning。
- `src/csbox/lab/screen.py`
  - frozen `TerminalCell`、`TerminalCursor`、`TerminalSnapshot`；外部只见 CSBox 类型。
  - `TerminalEmulator` 只处理 OUTPUT/RESIZE；ANSI 16/256/truecolor、style、cursor、erase、CR progress、resize 通过私有 pyte adapter。
  - pyte workaround 全部限制在本模块：宽字符 continuation 转 `width=0`；CJK 后 combining mark 合并到 lead cell；末列宽字符在 autowrap 下先换行；快照边界兜底禁止不完整宽字符越出固定 grid，并夹紧 pyte 的越界 cursor。
- `src/csbox/lab/captures.py`
  - `{ "version": 1, "captures": [...] }`，relative timestamp 排序，默认 UUID/UTC。
  - 同目录 temp、file flush/fsync、`os.replace`、directory fsync（平台不支持时 best effort）；主文件每次替换前维护可恢复 `.bak`。
  - 主文件损坏时恢复 backup 并返回 warning；单个 Capture validation 失败只隔离该 item；支持 title edit/delete。
  - JSON 仅由 domain dataclass/Pydantic 数据构成，不序列化 pyte 对象或类型信息。
- `src/csbox/lab/dispatcher.py`
  - sink 按显式注册顺序同步调用；失败时抛 `DispatchError`，保留 sink/index 与原始 `RecorderError` cause，不依赖 backend 类型。

## Final verification

- Focused: `22 passed in 0.26s`
  - `uv run pytest tests/test_session_models.py tests/test_recorder.py tests/test_terminal_screen.py tests/test_captures.py -q`
- Full suite: `163 passed in 2.96s`
  - `uv run pytest -q`
- Full Ruff: `All checks passed!`
  - `uv run ruff check .`
- Task 3 format: `12 files already formatted`
  - `uv run ruff format --check src/csbox/lab tests/test_session_models.py tests/test_recorder.py tests/test_terminal_screen.py tests/test_captures.py`
- `git diff --check` 与 `git diff --cached --check`：pass。

全仓 `ruff format --check .` 还会报告基线已有的 `docs/superpowers/plans/2026-08-10-csbox-usable-core.md` 与 `src/csbox/config/paths.py` 未格式化；Task 3 未修改这两个文件，按 scope 未做无关格式重写。Task 3 的全部 source/test 文件 format check 通过。

## Review and concerns

- 独立 reviewer 无 Critical，指出 4 个 Important。Task 3 范围内的非有限 interval 与 CJK/combining/末列宽字符几何均已增加 RED 并修复。
- reviewer 建议现在增加 `TerminalEmulator.restore(snapshot)` 与 `CastEvent` byte offset。父任务确认这两项属于 Task 4 checkpoint/replay 明确职责，Task 3 不提前实现；Task 4 应在 pyte adapter 内增加 restore，并在 reader/checkpoint 集成时决定 byte offset 表达，避免重复且不一致的 cast parsing。
- 未实现 replay/export/proxy；backend 也未引入 recorder 或 pyte 依赖。

## Reviewer hardening round

Status: complete. Fix commit: `1174495` (`fix: harden lab recording recovery`), base `b82bdf2`.

本轮处理 reviewer 的 4 个 Important 与 1 个 Minor；严格保持 Task 3 scope，未实现 Task 4 的 `TerminalEmulator.restore(snapshot)` 或 cast byte offset。

### 1. Recorder close state RED / GREEN

Root cause：原来的单一 `_closed` 在 STOP 成功排队或 writer 真正停止前就置位；因此首次 join 超时、queue full 无法排 STOP、并发 close 时，后续调用可能在 writer 仍存活时返回成功。

- RED command：`uv run pytest tests/test_recorder.py -k 'close_retry or concurrent_and_repeated_close' -q`
- RED result：`3 failed, 7 deselected in 0.17s`。
  - STOP 已排队但 join 超时后，第二次 `close()` 未继续等待。
  - queue full 导致 STOP 未排队后，重试返回但 writer 仍 alive。
  - 并发第二个 `close()` 在 gated writer 释放前提前成功。
- Implementation：在独立 close lock 下维护 `_accepting`、`_stop_enqueued`、`_fully_closed`；重试复用已排 STOP 或在 drain 后重新排 STOP，只有确认 writer thread 终止才成功返回；writer cause 在所有重复 close 中持续可见。
- GREEN result：`3 passed, 7 deselected in 0.18s`。

### 2. Incremental UTF-8 tail and EXIT ordering RED / GREEN

Root cause：decoder 只在 STOP 时 final flush，显式 EXIT 已先写入，导致尾字节事件出现在 `x` 之后；Recorder 也没有独立的 accepting 状态阻止 EXIT 后事件。

- RED command：`uv run pytest tests/test_recorder.py -k 'incomplete_output_and_input or stop_without_exit' -q`
- RED result：`1 failed, 1 passed, 10 deselected in 0.07s`；STOP-only 能 flush，但显式 EXIT 顺序是 `x,o,i`。
- Implementation：遇到 EXIT 时先按稳定 OUTPUT/INPUT 顺序 final flush decoder，再写 `x`；STOP 仅在 decoder 尚未 finalize 时 flush；EXIT 成功入队后立即关闭 accepting gate。
- GREEN result：`2 passed, 10 deselected in 0.05s`；精确尾部为 `[o replacement, i replacement, x]`，且 `x` 是最后 cast event。

### 3. Unknown event interval RED / GREEN

Root cause：合法 shape 的 unknown code 被 warning 后直接丢弃，连同 interval 一起消失，后续 replay 相对时间提前。

- RED command：`uv run pytest tests/test_recorder.py -k 'keeps_valid_events or carries_multiple_unknown' -q`
- RED result：`2 failed, 11 deselected in 0.07s`；`0.1 known + 2.0 unknown + 3.0 unknown + 0.2 known` 仍得到 `0.2`，而不是 carry 后的 `5.2`。
- Implementation：Reader 继续忽略 unknown extension payload 并返回 warning，但累积其 interval 到下一已知事件；unknown tail 只 warning，不改变此前事件。
- GREEN result：`2 passed, 11 deselected in 0.05s`；后一个 known 的累计 relative time 为 `5.3`。

### 4. Screen CJK grid invariant RED / GREEN

Root cause：pyte 覆盖宽字符 lead 会残留 orphan continuation，覆盖 continuation 会残留 orphan lead；4→3 resize 裁掉 continuation 后，3→4 会保留/复活无配对 lead。旧 snapshot 转换只处理末列越界，未验证所有 half-cell pairing。

- RED command：`uv run pytest tests/test_terminal_screen.py -k 'overwriting_either or resize_shrink' -q`
- RED result：`2 failed, 6 deselected in 0.08s`。
- Implementation：私有 `_CSBoxScreen` 在 draw/resize 后修复 raw pyte row，清 orphan half 且保留新写 glyph；snapshot conversion 再强制每个 width=2 紧跟空 character/width=0 continuation，每个 width=0 都有相邻 lead，所有 row 维持固定 columns。
- GREEN result：`2 passed, 6 deselected in 0.06s`；lead/continuation overwrite 与 4→3→4 均满足 grid invariant。

### 5. Strict asciicast v3 header RED / GREEN

Root cause：Reader 仅用 `value.get("version") == 3`，会接受 float `3.0`、缺失/错误 term、bool/非正尺寸和非字符串 terminal type；损坏的第一数据行还会被当作普通 event corruption 跳过。

- RED command：`uv run pytest tests/test_recorder.py -k 'non_strict_v3_headers or malformed_first_data_line' -q`
- RED result：`11 failed, 13 deselected in 0.11s`。
- Implementation：第一个非注释/非空数据行固定作为 header；version 必须是 exact int `3`，term 必须是 mapping，cols/rows 必须是 positive exact int（拒绝 bool），可选 type 必须是 str；malformed first data line 以带行号 `RecorderError` 拒绝。
- GREEN result：`11 passed, 13 deselected in 0.06s`。

### Hardening-round final verification

- Task 3 focused：`41 passed in 0.43s`
  - `uv run pytest tests/test_session_models.py tests/test_recorder.py tests/test_terminal_screen.py tests/test_captures.py -q`
- Full suite：`182 passed in 3.31s`
  - `uv run pytest -q`
- Full Ruff：`All checks passed!`
  - `uv run ruff check .`
- Task 3 format：`12 files already formatted`
  - `uv run ruff format --check src/csbox/lab tests/test_session_models.py tests/test_recorder.py tests/test_terminal_screen.py tests/test_captures.py`
- `git diff --check` 与 `git diff --cached --check`：pass。

全仓 format 的两个基线问题仍按父任务要求留给 Task 9；本轮 Task 3 source/test 全部 format clean。

## API Task 3 review security fixes

Base: `fe5970a`。本轮只修复 API domain models、errors 与 redaction 的评审问题；未实现 scenario/parser/transport/runner。

### RED / GREEN

1. Sensitive field-name normalization
   - RED：`uv run pytest tests/test_api_redaction.py::test_sensitive_field_names_normalize_separators_and_camel_case_across_surfaces -q`
   - 结果：`1 failed`；`Proxy_Authorization`、`setCookie`、`accessToken`、`api-key` 和配置字段的分隔符变体未命中。
   - GREEN：字段名统一 casefold，并移除常见非字母数字分隔符后做完整名称匹配；`token_count`、`Token-Count` 等普通字段不做子串误判。
2. Configured values in JSON mapping keys
   - RED：`uv run pytest tests/test_api_redaction.py::test_redactor_json_value_redacts_configured_secrets_in_mapping_keys -q`
   - 结果：`1 failed`；配置 secret 在 mapping key 中原样保留。
   - GREEN：递归复制 mapping 时也对字符串化 key 应用配置值替换；输入保持不变，嵌套 object/list 为深拷贝，`json.dumps` 不含 sentinel。
3. Invalid URL fallback
   - RED：`uv run pytest tests/test_api_redaction.py::test_redactor_url_returns_safe_marker_when_url_validation_raises -q`
   - 结果：`2 failed`；无效 port 和 IPv6 bracket 触发 `ValueError` 后回退到近似原 URL，泄露 credential 与 sensitive query value。
   - GREEN：URL 解析/hostname/port 校验失败时返回固定 redaction marker，不再返回原 URL。
4. Safe `ApiDomainError` formatting
   - RED：`uv run pytest tests/test_api_models.py::test_domain_error_default_formatting_exposes_only_user_message -q` 得到 `3 failed`；异常属性可被普通 `vars()` 序列化，隐式 context 可进入默认 traceback/log。
   - 补充 RED：`uv run pytest tests/test_api_models.py::test_domain_error_discards_raw_explicit_cause_from_default_traceback -q` 得到 `1 failed`；显式 cause 会被默认 traceback 输出。
   - GREEN：正常不变量为 `args`/`str`/`repr`/默认 traceback/logging 只暴露中文 `user_message`；`debug_message` 存于 slots，仅显式属性访问；隐式 context 被 suppress，显式 raw cause 不保留在公开格式化链。
5. Recursive JSON-facing values
   - RED：`uv run pytest tests/test_api_models.py::test_json_facing_fields_reject_non_json_values -q`
   - 结果：`12 failed`；`json_body`、assertion `expected`/`actual` 接受 object、tuple、嵌套 object 与 NaN。
   - GREEN：三个字段改用 Pydantic recursive `JsonValue`，并禁止非有限浮点；保留 null/bool/int/finite float/string/list/object，构造后可稳定 `model_dump_json()`。

### Verification

- Focused：`36 passed in 0.08s`
  - `uv run pytest tests/test_api_redaction.py tests/test_api_models.py -q`
- Full suite：`360 passed, 1 skipped in 5.28s`
  - `uv run pytest -q`
  - skip 为仅在 Windows CI 执行的原生 ConPTY smoke。
- Full Ruff：`All checks passed!`
  - `uv run ruff check .`
- Changed-file format：`5 files already formatted`
  - `uv run ruff format --check src/csbox/api/redaction.py src/csbox/api/errors.py src/csbox/api/models.py tests/test_api_redaction.py tests/test_api_models.py`

## API Task 3 remaining review fixes

Base: `ddf6d65`。本轮只修复 API header key redaction/collision 与
`ApiDomainError` 公开异常链泄露；未修改其他模块。

### RED / GREEN

1. Header key redaction、nested serialization 与 deterministic collision
   - RED：定向运行新增/扩展的 header key、collision 与 model serialization 测试。
   - 结果：`3 failed`；configured sentinel 在 header key 和嵌套
     `ApiRun.model_dump_json()` 中原样保留，两个输入名映射到同一脱敏名时没有定义保值策略。
   - GREEN：先预留所有无需改名的安全 header 名；其他名称应用 configured-value
     redaction，碰撞时按输入顺序稳定分配 `-2`、`-3` 后缀。普通安全名称和值保持不变，
     输出 JSON 不含 raw sentinel。
   - 定向结果：`3 passed in 0.06s`。
2. Explicit cause 与 implicit context 公开属性
   - RED：定向运行隐式 context 参数化测试与显式 cause 测试。
   - 结果：`4 failed`；`error.__context__` 可直接取回带 raw sentinel 的
     `RuntimeError`。
   - GREEN：为 `ApiDomainError.__context__` 增加与现有 `__cause__` 对称的屏蔽
     property/setter；公开属性均返回 `None`，默认 traceback/logging 继续安全，
     `debug_message` 仅保留显式访问支持。
   - 定向结果：`4 passed in 0.06s`。

新增/扩展回归整体首次 RED：`7 failed in 0.11s`。

### Verification

- Focused：`38 passed in 0.08s`
  - `uv run pytest tests/test_api_redaction.py tests/test_api_models.py -q`
- Full suite：`362 passed, 1 skipped in 5.26s`
  - `uv run pytest -q`
  - skip 为仅在 Windows CI 执行的原生 ConPTY smoke。
- Full Ruff：`All checks passed!`
  - `uv run ruff check .`
- Full format：`128 files already formatted`
  - `uv run ruff format --check .`
- `git diff --check`：pass。

## API Task 3 final redaction review fixes

Base: `00923e8`。本轮只修复 malformed URL credential fallback 与 JSON mapping
key collision 两个 remaining finding；未实现 Task 4，也未修改其他模块。

### Root cause and RED

1. Malformed URL credential fallback
   - Root cause：`_redacted_netloc()` 在 `parsed.hostname is None` 时原样返回
     `parsed.netloc`，因此 `user:password@` authority 会进入返回值。
   - RED/coverage command：
     `uv run pytest tests/test_api_redaction.py -k 'preserves_safe_key_when_redacted_key_collides or suffixes_converging_redacted_keys or credentials_without_hostname or url_validation_raises' -q`
   - RED result：`3 failed, 3 passed, 17 deselected in 0.09s`。无 hostname
     credential case 泄露 raw credential；invalid port、malformed IPv6 bracket 与
     NFKC netloc validation 三条既有/fallback 路径保持安全 marker。
2. JSON mapping key collision
   - Root cause：dict comprehension 直接以 redacted key 写入新 mapping；existing marker
     key 或多个 configured replacement 收敛时，后写 entry 覆盖前写 entry。
   - 同一 RED command 中两个 collision 测试均失败：marker collision 丢失 1 个 entry，
     两个 configured replacement 收敛也只保留 1 个 entry。

### GREEN

- 无 hostname 且 authority 含 userinfo 分隔符时进入既有 `ValueError` 安全 fallback，
  整体返回固定 redaction marker；invalid port、IPv6 与其他 `urlsplit` validation
  路径继续由同一 fallback 覆盖。
- JSON mapping 先预留无需改名的 safe keys，再按输入顺序为 redacted collisions 稳定
  分配 `-2`、`-3` 后缀，与 headers 的 safety/data-integrity 策略一致。所有 entry、
  JSON shape 与 nested deep-copy 保留，序列化输出不含 raw sentinel。
- 新增回归 GREEN：`6 passed, 17 deselected in 0.06s`。

### Final verification

- Focused：`42 passed in 0.08s`
  - `uv run pytest tests/test_api_redaction.py tests/test_api_models.py -q`
- Full suite：`366 passed, 1 skipped in 5.27s`
  - `uv run pytest -q`
  - skip 为仅在 Windows CI 执行的原生 ConPTY smoke。
- Full Ruff：`All checks passed!`
  - `uv run ruff check .`
- Full format：`128 files already formatted`
  - `uv run ruff format --check .`
- `git diff --check`：pass。
