### Task 7: API CLI run/list and stable errors

**Files:**
- Create: src/csbox/api/cli.py, tests/test_api_cli.py
- Modify: src/csbox/cli/main.py, src/csbox/api/__init__.py

**Interfaces:**
- api_app = typer.Typer(help="接口测试场景、运行记录和证据导出。") registered as csbox api.
- render_run_plain(run) -> str and run_json_payload(run) -> dict[str, object] are pure functions with schema version 1.
- Exit code mapping is exactly 0/1/2/3 from the design.

- [ ] **Step 1: Add CliRunner RED tests for api run scenario.toml --var, --plain, --json, --fail-fast, invalid scenario and each exit code.**
- [ ] **Step 2: Run uv run pytest tests/test_api_cli.py -q; confirm RED because command is absent.**
- [ ] **Step 3: Wire ScenarioLoader, config variables, HttpxTransport, ApiRunner; catch only domain errors by default and show --verbose cause. Make plain output labels Chinese and technical method/URL/status English.**
- [ ] **Step 4: Run targeted tests plus uv run csbox api --help; verify help is readable at 80 columns.**
- [ ] **Step 5: Commit feat: add API run CLI.**

Do not make api run open TUI. csbox api with no subcommand opens the API TUI in Task 11.

---

