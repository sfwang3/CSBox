### Task 4: Variables and TOML scenario parser

**Files:**
- Create: src/csbox/api/variables.py, src/csbox/api/scenario.py
- Modify: src/csbox/config/loader.py
- Test: tests/test_api_variables.py, tests/test_api_scenario.py

**Interfaces:**
- resolve_variables(name_set, project, scenario, environ, cli) -> VariableResolution with precedence project < environment < scenario < CLI.
- interpolate(value: str, variables: Mapping[str, str]) -> str supports only {{identifier}}; nested dict/list interpolation is recursive.
- ScenarioLoader.load(path: Path) -> ApiScenario returns source line context in ApiConfigError.

- [ ] **Step 1: Add failing tests for precedence, missing variable, {{TODO_password}}, non-string JSON values, invalid method, conflicting body declarations, duplicate assertions and malformed TOML.**
- [ ] **Step 2: Run uv run pytest tests/test_api_variables.py tests/test_api_scenario.py -q; confirm RED.**
- [ ] **Step 3: Implement typed TOML conversion. Accept [steps.json], [steps.form], [[steps.multipart]], headers, query, bearer, timeout, follow_redirects, verify_tls; reject unknown keys and script-like constructs. Preserve source path/step name in errors.**
- [ ] **Step 4: Run targeted tests plus uv run ruff check src/csbox/api.**
- [ ] **Step 5: Commit feat: add TOML API scenarios and variables.**

The parser must not import Textual or HTTPX. It must never log resolved variable values. Add a test proving a malformed scenario returns a Chinese “发生了什么/在哪里/怎么处理” message.

---

