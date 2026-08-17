### Task 5: HTTPX transport and stable runtime errors

**Files:**
- Create: src/csbox/api/transport.py, src/csbox/api/httpx_transport.py
- Modify: pyproject.toml, uv.lock
- Test: tests/test_api_transport.py

**Interfaces:**
- class ApiTransport(Protocol): async def send(self, request: ApiRequest) -> ApiResponse: ...
- HttpxTransport(client_factory: Callable[..., httpx.AsyncClient] | None = None, response_max_bytes=...) implements the protocol and closes clients.
- map_httpx_error(error: Exception) -> ApiTransportError has stable Chinese user messages.

- [ ] **Step 1: Add fake-client tests for method/query/header/body mapping and connect/read timeout values; add exception parametrization for InvalidURL, DNS, ConnectError, ReadTimeout, TimeoutException, TLS and response read failures.**
- [ ] **Step 2: Run uv run pytest tests/test_api_transport.py -q; confirm RED.**
- [ ] **Step 3: Add httpx>=0.27,<1, implement async send with httpx.Timeout(connect=..., read=..., write=..., pool=...), follow_redirects=False, verify=True, and bounded body read. Do not call httpx.* outside this file.**
- [ ] **Step 4: Run targeted tests, uv lock --check, uv run ruff check .**
- [ ] **Step 5: Commit feat: add HTTPX API transport.**

Add a test that inspects the constructed request and proves TLS verify defaults to True, redirects default to False, and the raw exception text is not the user-facing message.

---

