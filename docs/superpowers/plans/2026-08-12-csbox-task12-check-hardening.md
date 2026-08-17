# CSBox Task 12 Check Hardening Implementation Plan

> **For agentic workers:** This plan is scoped strictly to Task 12. Do not start Task 13 or any later task. Use TDD for every production behavior: write one failing test, run it, implement the smallest change, run it again, then refactor while green.

**Goal:** Make `csbox check` use one bounded file inventory, reliably detect common nested project layouts, optionally run a safe gitleaks deep scan, and emit strict versioned output without leaking secrets or machine paths.

**Architecture:** `FileInventory.build()` performs the only filesystem traversal and stores deterministic regular-file/directory entries. Its per-file text cache is bounded by both file size and a total byte budget; all text rules and built-in project detectors consume that cache/index. `CheckService` creates one report from that inventory, optionally appends a safe deep-scan finding, and exposes immutable scan statistics. CLI serializers convert internal absolute roots to project-relative display values and use strict JSON serialization.

**Tech Stack:** Python 3.11+, dataclasses, pathlib/os, subprocess argv execution, Pydantic 2, Typer/Rich, pytest, Ruff, uv.

## Global Constraints

- Keep Python `>=3.11` and the existing `schema_version = 1` contract.
- Do not use `os.walk`, `Path.rglob`, or another project-wide traversal from detectors or rules after inventory construction.
- Skip symlinks, broken links, special files, unreadable entries, pruned directories, NUL/binary data, and files over the text-read limit safely.
- Never place raw secret values, gitleaks stdout/stderr, exception text, or absolute project paths in user-facing finding/CLI output.
- `--deep` invokes `gitleaks detect --source <root> --no-banner --redact --exit-code 1` as an argv list with `shell=False`; missing gitleaks is `SKIP`.
- Do not download tools, mutate the project, run build commands unless `--build` is supplied, push Git, or implement Pack/Task 13 behavior.

---

### Task 1: Bounded FileInventory and shared text cache

**Files:**
- Modify: `src/csbox/check/detectors.py`, `src/csbox/check/models.py`, `src/csbox/check/rules.py`, `src/csbox/check/build.py`
- Test: `tests/test_check_inventory.py`, `tests/test_check_rules.py`

**Interfaces:**
- `FileEntry(relative: Path, absolute: Path, size: int, file_type: str = "regular", is_text_candidate: bool = ...)` represents only a regular file inside the resolved root.
- `FileInventory.build(root, *, scan_limit_bytes=256*1024, text_cache_limit_bytes=8*1024*1024)` performs one deterministic traversal.
- `FileInventory.text(entry) -> str | None` memoizes success and safe skip results; it never reads more than `scan_limit_bytes + 1` bytes and never stores a value above the total cache budget.
- `FileInventory.text_scan_stats` returns a snapshot exposing `requested`, `cache_hits`, `read_count`, `bytes_read`, `cached_entries`, `cached_bytes`, and stable skip counters for large, budget, NUL/binary, decode, sensitive, unreadable, and special files.

- [ ] Write RED tests for a custom rule calling `context.inventory.text(entry)` twice, large-file no-read behavior, NUL/invalid UTF-8 skips, cache statistics, symlink/broken-symlink/special-file exclusion, and a 10,000-file root fixture with one traversal and bounded reads.
- [ ] Run `uv run pytest tests/test_check_inventory.py tests/test_check_rules.py -q` and confirm the new expectations fail for the current uncached/unbounded implementation.
- [ ] Implement deterministic `os.scandir` traversal with per-entry exception handling, pruned-directory metadata, path containment checks, regular-file/no-follow reads, bounded cache accounting, and safe negative caching.
- [ ] Change `PrivateKeyRule` and all text rules to use inventory APIs; remove direct file reads from rules.
- [ ] Run the focused inventory/rule tests and existing check rule tests; refactor only while green.
- [ ] Commit the inventory/cache slice with a focused message.

### Task 2: Inventory-backed project detectors

**Files:**
- Modify: `src/csbox/check/detectors.py`, `src/csbox/check/models.py`, `src/csbox/check/service.py`
- Test: `tests/test_check_detectors.py`, `tests/test_check_service.py`

**Interfaces:**
- `detect_projects(root, detectors=DEFAULT_DETECTORS, *, inventory=None) -> tuple[DetectedProject, ...]` reuses the supplied inventory and otherwise builds exactly one.
- Built-in detectors expose inventory-backed detection while retaining root-level `detect(root)` compatibility for direct callers.
- Each actual marker directory produces at most one project per build system. Nested Maven/Gradle projects are sorted by project-relative path, then detector priority; no module/dependency parsing is added.
- `DetectedProject` retains `kind`, `root`, and `marker`; optional manager metadata is only populated from a real lockfile or manifest (`npm`, `pnpm`, `yarn`, `uv`, `poetry`) and is never inferred from a directory name.

- [ ] Write RED tests for pnpm/yarn/npm evidence, uv/Poetry evidence, nested Maven/Gradle markers, duplicate equivalent markers in one directory, deterministic CJK/ASCII mixed ordering, and service detector execution with a counting inventory/traversal.
- [ ] Run `uv run pytest tests/test_check_detectors.py tests/test_check_service.py -q` and confirm RED.
- [ ] Implement candidate grouping from inventory files only, manager evidence resolution, marker priority/deduplication, symlink/path containment filtering, and stable aggregate ordering.
- [ ] Pass the already-built inventory from `CheckService.run()` into `detect_projects()` and keep build adapter selection compatible with nested `DetectedProject.root` values.
- [ ] Run detector, build-adapter, and service focused tests; refactor only while green.
- [ ] Commit the detector slice with a focused message.

### Task 3: Deep gitleaks scan and report model

**Files:**
- Modify: `src/csbox/check/rules.py`, `src/csbox/check/models.py`, `src/csbox/check/service.py`
- Test: `tests/test_check_deep.py`, `tests/test_check_service.py`

**Interfaces:**
- `run_deep_secret_scan(root) -> CheckFinding` returns a safe `PASS`, `FAIL`, `SKIP`, or `WARN` finding with category `deep-secret-scan`.
- Exit code `0` means no findings (`PASS`); exit code `1` means gitleaks found a secret (`FAIL`); timeout, startup errors after discovery, unsupported/malformed tool behavior, or other exit codes become safe `WARN`; missing/raced-away command is `SKIP`.
- `CheckReport.deep_scan: CheckFinding | None` is `None` unless `deep=True`; `text_scan_stats` is a stable JSON-safe mapping copied from the inventory.
- All subprocess output is discarded or safely bounded and never copied to a finding or exception message.

- [ ] Write RED tests for installed gitleaks argv/flags and exit semantics, missing gitleaks as `SKIP`, execution error as safe `WARN`, and a fake gitleaks executable that emits `CSBOX_SECRET_SENTINEL_...` to both streams without leaking it.
- [ ] Run `uv run pytest tests/test_check_deep.py tests/test_check_service.py -q` and confirm RED.
- [ ] Implement `shutil.which` discovery, argv-only subprocess execution with timeout and `shell=False`, safe status mapping, and report integration.
- [ ] Run deep focused tests with both fake-installed and missing-tool paths; record the real environment's gitleaks availability without installing anything.
- [ ] Commit the deep-scan/report slice with a focused message.

### Task 4: Strict JSON and plain output safety

**Files:**
- Modify: `src/csbox/cli/main.py` and, only where required for safe display, `src/csbox/tui/screens/project_check.py`
- Test: `tests/test_check_cli.py`, `tests/test_check_deep.py`

**Interfaces:**
- `csbox check --deep` forwards `deep=True` to `CheckService.run()`.
- JSON continues to have `schema_version: 1`, includes explicit `deep_scan` and `text_scan_stats`, uses project-relative project/finding/build locations, and serializes with `allow_nan=False` and deterministic separators/order.
- Plain output remains concise and uses clear `PASS`, `WARN`, `FAIL`, and `SKIP` lines; missing deep scan is visibly `SKIP`, never `PASS`.

- [ ] Write RED CLI tests for `--deep` forwarding, relative paths with CJK names, strict JSON rejecting non-finite values, stable deep-scan fields, and absence of a secret sentinel from plain/JSON/error output.
- [ ] Run `uv run pytest tests/test_check_cli.py tests/test_check_deep.py -q` and confirm RED.
- [ ] Implement safe report payload conversion, strict `json.dumps`, deep/plain rendering, and relative TUI project locations without changing the TUI layout contract.
- [ ] Run all check CLI/TUI tests and inspect representative plain and JSON output in a temporary Chinese-path fixture.
- [ ] Commit the CLI/output slice with a focused message.

### Task 5: Integrated verification and independent read-only review

**Files:**
- Modify only files named by an observed Task 12 defect.
- Test: all Task 12 focused tests and the existing full suite.

- [ ] Run the complete Task 12 focused matrix, including the 10,000-file fixture, large/binary/NUL cases, detector matrix, gitleaks installed/missing/sentinel cases, and strict JSON.
- [ ] Run fresh `uv run pytest`, `uv run ruff check .`, `uv run ruff format --check .`, `uv lock --check`, and `git diff --check`.
- [ ] Perform a read-only review of the diff for repeated traversal, unbounded reads, symlink escape, false project confidence, raw subprocess output, path leakage, schema instability, and unrelated Task 13 changes.
- [ ] Fix every Critical/Important issue and every blocking correctness/security/performance/CJK issue, then rerun affected tests and the final verification commands.
- [ ] Confirm clean worktree and local-only commits; do not push or run publish scripts.
