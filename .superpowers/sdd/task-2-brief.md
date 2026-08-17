### Task 2: Shared config, safe paths and cell text layout

**Files:**
- Create: src/csbox/core/safe_paths.py, src/csbox/core/text_layout.py
- Modify: src/csbox/config/models.py, src/csbox/config/__init__.py
- Test: tests/test_safe_paths.py, tests/test_text_layout.py, tests/test_config.py

**Interfaces:**
- safe_relative_path(value: str) -> PurePosixPath rejects absolute, drive, UNC-like, mixed separator and .. paths.
- atomic_write_text(destination: Path, text: str) -> None refuses symlink destinations and uses temp + replace.
- wrap_cells(text: str, width: int) -> tuple[str, ...] preserves grapheme clusters enough for existing wcwidth semantics and guarantees display_width(line) <= width.
- Add ApiConfig(variables: dict[str, str] = {}, response_max_bytes=262144, timeout_seconds=10.0) and CSBoxConfig.api with strict bounds.

- [ ] **Step 1: Add red tests for dangerous paths, atomic symlink refusal, CJK wrapping and config validation.**
- [ ] **Step 2: Run uv run pytest tests/test_safe_paths.py tests/test_text_layout.py -q; observe RED.**
- [ ] **Step 3: Implement only the bounded helpers and config model; do not refactor Lab yet.**
- [ ] **Step 4: Run uv run pytest tests/test_safe_paths.py tests/test_text_layout.py tests/test_config.py -q and uv run ruff check src/csbox/core src/csbox/config.**
- [ ] **Step 5: Commit feat: add safe paths and cell layout primitives.**

The tests must include 计算机网络实验, abc中文123, C:\Users\测试用户\桌面\实验一, ~/课程实验/计算机网络/实验一, combining characters and widths 1/2/3. A malformed config error must name the config path, field and repair action without exposing any value.

---

