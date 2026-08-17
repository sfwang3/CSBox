### Task 1: CI Node 24、版本单一来源与 schema 基础

**Files:**
- Modify: .github/workflows/ci.yml
- Modify: pyproject.toml, src/csbox/__init__.py, src/csbox/cli/main.py
- Create: src/csbox/core/schema.py, tests/test_versioning.py, tests/test_schema.py

**Interfaces:**
- csbox.__version__ 调用 importlib.metadata.version("csbox")，开发树 metadata 不存在时使用明确的 0.2.0.dev0 fallback；不能在第二处硬编码 release 版本。
- SCHEMA_VERSION = 1 与 with_schema_version(payload) -> dict[str, object] 为所有 CLI JSON 的基础。

- [ ] **Step 1: Write the failing tests.**

~~~
def test_version_is_single_runtime_source() -> None:
    from csbox import __version__
    from importlib.metadata import version
    assert __version__ == version("csbox")

def test_machine_payload_has_schema_version() -> None:
    from csbox.core.schema import with_schema_version
    assert with_schema_version({"status": "PASS"}) == {
        "schema_version": 1,
        "status": "PASS",
    }
~~~

- [ ] **Step 2: Run to verify RED.**

Run: uv run pytest tests/test_versioning.py tests/test_schema.py -q
Expected: FAIL because version is 0.1.0 and schema helper is absent.

- [ ] **Step 3: Implement minimal version/schema and CLI option.**

Set project.version = "0.2.0", implement metadata lookup, add --version to the Typer app, and prefix check --json/pack --json with schema_version without changing their existing field names.

- [ ] **Step 4: Run GREEN and existing regression.**

Run: uv lock && uv run pytest tests/test_versioning.py tests/test_schema.py tests/test_cli.py -q
Expected: all pass.

- [ ] **Step 5: Update CI action pins and commit.**

Use immutable official pins currently verified from upstream: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b # v7.0.1 and astral-sh/setup-uv@c771a70e6277c0a99b617c7a806ffedaca235ff9 # v9.0.0. Checkout v5+ and setup-uv v9 use Node 24; document runner minimum in CI comments. Keep the 2×2 OS/Python matrix.

Run: uv lock --check && uv run ruff check . && uv run ruff format --check .

Commit: git commit -am "chore: refresh CI actions and version source"

---

