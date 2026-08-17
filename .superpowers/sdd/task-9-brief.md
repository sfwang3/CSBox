### Task 9: OpenAPI 3.x import

**Files:**
- Create: src/csbox/api/openapi.py, tests/test_openapi.py
- Modify: pyproject.toml, uv.lock, src/csbox/api/cli.py

**Interfaces:**
- OpenApiImporter.load(path: Path) -> OpenApiDocument using JSON or yaml.safe_load only.
- OpenApiImporter.to_scenario(document, source_name) -> ApiScenario.
- write_scenario_templates(scenario, destination, force=False) -> tuple[Path, ...].

- [ ] **Step 1: Add RED fixtures for OpenAPI 3.0/3.1 JSON/YAML with operationId, path/query/header parameters, requestBody schema, multiple response statuses, missing defaults, invalid version and YAML unsafe tags.**
- [ ] **Step 2: Run uv run pytest tests/test_openapi.py -q; confirm RED.**
- [ ] **Step 3: Add PyYAML>=6,<7, implement safe load, method/path/operation extraction and deterministic operation ordering. Values without defaults become {{TODO_name}}; request body schema recursively produces structural placeholders only; assertions stay empty.**
- [ ] **Step 4: Add csbox api import <file> --output .csbox/api/scenarios --force, protect symlink/overwrite and test generated TOML can be parsed by ScenarioLoader.**
- [ ] **Step 5: Commit feat: import OpenAPI scenario templates.**

The generated template must contain no fabricated credentials, student IDs or business assertions. The test must assert those strings are absent and placeholder names are present.

---

