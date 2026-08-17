### Task 6: Assertions and runner

**Files:**
- Create: src/csbox/api/assertions.py, src/csbox/api/runner.py
- Test: tests/test_api_assertions.py, tests/test_api_runner.py

**Interfaces:**
- evaluate_assertion(assertion: ApiAssertion, response: ApiResponse, redactor: Redactor) -> ApiAssertionResult.
- ApiRunner(transport: ApiTransport, redactor: Redactor).run(scenario, variables, fail_fast=False) -> ApiRun.
- JSON path uses jsonpath-ng.parse and accepts exactly one match for scalar assertions; missing/invalid JSON is a safe SKIP or FAIL with location, never a traceback.

- [ ] **Step 1: Write RED tests for status exact/range, header exists/equal case-insensitively, JSON path exists/equals/type, body contains/not-contains, PASS/FAIL/SKIP and secret-safe failure text.**
- [ ] **Step 2: Run uv run pytest tests/test_api_assertions.py tests/test_api_runner.py -q; confirm RED.**
- [ ] **Step 3: Add jsonpath-ng>=1.7,<2 and implement evaluator. Use json.loads once per response and jsonpath_ng.parse rather than a home-grown full JSONPath parser.**
- [ ] **Step 4: Implement runner step aggregation. Default continues after assertion/runtime failure; fail-fast stops; collect elapsed and response metadata; create evidence only after redaction.**
- [ ] **Step 5: Run targeted tests and uv run ruff check .; commit feat: add API assertions and runner.**

Failure output must include expected/actual/location after redaction. For a transport failure, assertions become SKIP with “未收到响应” and the run becomes runtime error.

---

