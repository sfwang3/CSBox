### Task 8: Run persistence and list

**Files:**
- Create: src/csbox/api/repository.py, tests/test_api_repository.py
- Modify: src/csbox/config/paths.py, src/csbox/api/cli.py

**Interfaces:**
- ApiRunRepository.from_cwd(cwd) -> ApiRunRepository rooted at .csbox/api/runs.
- save(run: ApiRun) -> ApiRunPaths, load(run_id_or_prefix: str) -> ApiRun, list() -> tuple[ApiRunSummary, ...].
- ApiRunPaths exposes only safe relative run directory and metadata/result paths.

- [ ] **Step 1: RED tests save/load/list, prefix ambiguity, corrupt result, symlink run directory, atomic partial write and absent runs.**
- [ ] **Step 2: Run uv run pytest tests/test_api_repository.py -q; confirm RED.**
- [ ] **Step 3: Implement schema-versioned metadata/result with atomic writes. Store scenario display name/relative source, UTC ISO timestamps, duration, counts, CSBox version and redacted step data; never store resolved variable map or raw bodies.**
- [ ] **Step 4: Add csbox api list --plain|--json and test schema_version, deterministic ordering, prefix truncation and no sentinel secret.**
- [ ] **Step 5: Commit feat: persist API runs safely.**

Corrupt derived result must be reported as an unavailable run, not destroy neighboring runs. No automatic deletion or repair is allowed.

---

