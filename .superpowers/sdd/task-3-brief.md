### Task 3: API models, errors and redaction

**Files:**
- Create: src/csbox/api/models.py, src/csbox/api/errors.py, src/csbox/api/redaction.py
- Modify: src/csbox/api/__init__.py
- Test: tests/test_api_models.py, tests/test_api_redaction.py

**Interfaces:**
- ApiMethod = Literal["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"].
- ApiRequest, ApiResponse, ApiScenario, ApiStep, ApiAssertion, ApiAssertionResult, ApiRun, ApiRunResult, ApiEvidence are strict models with extra="forbid".
- RedactionPolicy.default() -> RedactionPolicy; Redactor(policy).headers/json_value/text/url return only redacted copies.
- ApiConfigError, ApiTransportError, ApiPersistenceError expose user_message and optional debug_message; str(error) never includes raw values.

- [ ] **Step 1: Write tests using sentinel CSBOX_SECRET_SENTINEL_9f4d; assert it is absent from redacted headers, nested JSON/list, URL credentials/query, text, model_dump_json, and error strings.**
- [ ] **Step 2: Run uv run pytest tests/test_api_models.py tests/test_api_redaction.py -q; confirm RED.**
- [ ] **Step 3: Implement models and redactor. Header names are case-insensitive; Authorization: Bearer <value> becomes Bearer ••••••••; configured secret values are exact-replaced longest-first. Never mutate the input object.**
- [ ] **Step 4: Run targeted tests, then uv run ruff check src/csbox/api.**
- [ ] **Step 5: Commit feat: add API domain models and secret redaction.**

Add regression values for password, passwd, token, access_token, refresh_token, jwt, secret, api_key, apikey, client_secret, Cookie, Set-Cookie, Proxy-Authorization, and an extra configured field. Include a URL like https://user:CSBOX_SECRET_SENTINEL_9f4d@example.test/a?token=....

---

