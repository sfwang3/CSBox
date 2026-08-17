## Task 5 evidence — 2026-08-11

- Approved base: `325c47e` (`fix: harden API scenario boundaries`).
- RED observed: `uv run pytest tests/test_api_transport.py -q` failed during collection with `ModuleNotFoundError: No module named 'csbox.api.httpx_transport'` before adapter implementation.
- Additional boundary RED observed: `response_max_bytes=1.5` did not raise, then the constructor was restricted to positive integers.
- GREEN: `uv run pytest tests/test_api_transport.py -q` → `28 passed`.
- Full regression: `uv run pytest -q` → `435 passed, 1 skipped` (Windows-only ConPTY smoke).
- Static/dependency checks: `uv run ruff check .`, `uv run ruff format --check .`, `uv lock --check`, and `git diff --check` all passed.
- Scope audit: source-level `httpx` references exist only in `src/csbox/api/httpx_transport.py`; domain models and errors do not import HTTPX.

## Task 5 review fixes — 2026-08-11

- Review base: `f0b8a34` (`feat: add HTTPX API transport`). Changes remain limited to
  the API transport/domain model, their focused tests, and this report; runner,
  assertions, and CLI remain out of scope.
- Root causes:
  - `HttpxTransport.send()` copied raw response URL, headers, and decoded body into
    `ApiResponse`, while the transport had no request-derived redaction policy.
  - `response_size` always used captured bytes, so bounded truncation without a total
    length incorrectly looked exact and valid `Content-Length` was discarded.
  - transport timeout validation used only `<= 0`, allowing `NaN`/`Infinity` and raising
    `TypeError` for invalid runtime types; `ApiRequest.timeout_seconds` lacked a positive
    bound.
- Initial RED: `uv run pytest tests/test_api_transport.py tests/test_api_models.py -q`
  produced `20 failed, 47 passed`. Failures covered missing redacted response views,
  absent `response_size_exact`, wrong truncated size semantics, the missing optional
  redactor, and timeout boundary gaps.
- URL userinfo RED: the focused request-derived redaction test produced `1 failed` when
  the URL username was a separate sentinel, proving that password-only extraction was
  insufficient.
- GREEN implementation:
  - Each send builds a short-lived redactor by combining an optional configured
    `Redactor` policy with sensitive values derived from URL userinfo/query, auth and
    cookie headers, JSON/form/multipart fields, and JSON/form-like raw bodies.
  - Response URL/header/body values are redacted before `ApiResponse` construction.
    JSON and `+json` bodies use strict parse, recursive `json_value` redaction, and safe
    compact serialization; other or malformed content falls back to text redaction.
  - `response_size` is the valid original `Content-Length` when present; otherwise it is
    the observed captured byte count. `response_size_exact=False` explicitly marks the
    latter as a lower bound when bounded reading truncates the stream. The capture buffer
    remains capped and the client context/cleanup path is unchanged.
  - `ApiRequest` requires a finite positive optional timeout through the strict Pydantic
    model, and `HttpxTransport` safely validates its default timeout before constructing
    HTTPX connect/read/write/pool timeout values.
- Focused GREEN: `67 passed in 0.14s`.
- Full regression: `454 passed, 1 skipped in 5.60s`; the skip is the Windows-only native
  ConPTY smoke.
- Static/dependency checks: `uv lock --check`, `uv run ruff check .`,
  `uv run ruff format --check .`, and `git diff --check` all passed. Source-level
  `httpx` references remain isolated to `src/csbox/api/httpx_transport.py`.

## Task 5 remaining review fixes — 2026-08-11

- Review base: `129e601` (`fix: harden API response handling`). Changes remain limited
  to redaction/transport policy, their regression tests, and this report; bounded
  response capture, JSON redaction, URL/header redaction, HTTPX isolation/cleanup, and
  the runner/assertion/CLI boundary are unchanged.
- Root causes:
  - `Redactor.text()` only replaced configured/request-derived exact values, so an
    unknown response-only value under a sensitive key survived malformed JSON and
    non-JSON fallback paths, including ordinary response header values.
  - Request-derived redaction retained decoded/raw secret values only, while HTTP
    services can reflect the same value with URL or form percent encoding.
  - Response metadata trusted every valid `Content-Length`, even after a complete
    stream proved it inconsistent, and trusted inconsistent declarations after
    bounded truncation.
- RED first: `uv run pytest tests/test_api_redaction.py tests/test_api_transport.py -q`
  produced `10 failed, 64 passed`. Failures covered quoted/unquoted sensitive text,
  malformed JSON and text/plain fallbacks, safe response header values, URL/form
  encoded echoes, complete-stream length mismatch, and truncated-stream mismatch.
- GREEN implementation:
  - `Redactor.text()` remains the single fallback policy point. After longest-first
    exact replacement, it recognizes bounded JSON-ish quoted/unquoted `:`/`=`
    assignments and masks values only when the complete normalized key is sensitive;
    `tokenize`, `not_token`, `x-token`, and `safe.token` remain unchanged.
  - Each non-empty request-derived secret contributes de-duplicated raw,
    `quote(..., safe="")`, and `quote_plus(..., safe="")` forms. Encoding failures fall
    back to the raw value without exposing it through an error.
  - A completed stream reports the observed byte count with `response_size_exact=True`,
    regardless of a conflicting declaration. A truncated stream uses a valid declared
    total only when it is greater than the captured lower bound; inconsistent or
    invalid declarations return that lower bound with `response_size_exact=False`.
- Focused regression: `uv run pytest tests/test_api_redaction.py tests/test_api_models.py
  tests/test_api_transport.py -q` passed (`102 passed`).
- Full regression: `uv run pytest -q` passed (`466 passed, 1 skipped in 5.38s`); the
  skip is the Windows-only native ConPTY smoke.
- Static/dependency checks: `uv run ruff check .`, `uv run ruff format --check .`,
  `uv lock --check`, and `git diff --check` passed after Ruff reformatted one touched
  test signature.

## Task 5 final redaction fixes — 2026-08-11

- Review base: `700ea93` (`fix: close remaining API response review findings`). Changes
  remain limited to `Redactor.text()`, focused redaction/transport regressions, and this
  report; request-derived exact/`quote`/`quote_plus` replacement, valid JSON/key and
  URL/header redaction, bounded response capture, and response-size semantics are
  unchanged.
- Root causes:
  - The assignment regex consumed `context=token=...` as one safe outer assignment;
    returning that whole match unchanged prevented the non-overlapping substitution
    pass from seeing the sensitive assignment inside its value.
  - Percent-encoded separators such as `%3D` and `%3A` were outside the recognized
    `:`/`=` separator grammar, so unknown response-only values could survive text and
    malformed-JSON fallback paths.
- RED first: `uv run pytest tests/test_api_redaction.py tests/test_api_transport.py -q`
  produced `11 failed, 76 passed`. Failures covered safe outer assignments, closed and
  unclosed JSON-shaped text, nested body/header values, `%3D`/`%3A` case variants, and
  percent-encoded response-only body/header values. Existing ordinary-word boundary
  cases remained green.
- GREEN implementation:
  - A non-sensitive outer match is rebuilt from its original key and separator while
    scanning only its strictly smaller value (or quoted value content). This exposes
    nested assignments, does not scan key text, terminates without rescanning the same
    match, and preserves whitespace and quote formatting.
  - The separator grammar also recognizes case-insensitive percent-encoded equals and
    colon separators and masks their values without requiring configured request data.
    Complete-key normalization still excludes `tokenize`, `not_token`, `x-token`, and
    `safe.token`.
- Focused regression: `uv run pytest tests/test_api_redaction.py tests/test_api_models.py
  tests/test_api_transport.py -q` passed (`114 passed`).
- Full regression: `uv run pytest -q` passed (`478 passed, 1 skipped in 5.35s`); the
  skip is the Windows-only native ConPTY smoke.
- Static/dependency checks: `uv lock --check`, `uv run ruff check .`,
  `uv run ruff format --check .`, and `git diff --check` all passed.

## Task 5 post-review redaction boundary fixes — 2026-08-11

- Review base: `3343412` (`fix: close final Task 5 redaction gaps`). Changes remain
  limited to API redaction/response boundary code, focused tests, architecture
  documentation, and this report; runner, assertions, CLI, persistence, evidence, and
  TUI implementation remain out of scope.
- Root causes:
  - Safe nested assignment values recursively invoked the assignment regex callback,
    so sufficiently deep `context=...=token=secret` wrappers exhausted Python's call
    stack.
  - Request-derived `quote`/`quote_plus` values included only the uppercase `%XX`
    spelling emitted by `urllib`, missing equivalent echoes with lowercase escape hex.
  - `Redactor.url()` applied generic assignment masking to the entire reconstructed URL,
    allowing a path assignment to consume query text and masking legitimate paths such
    as `/evidence/token=public-reference`.
  - `ApiResponse` allowed a transient raw representation but exposed no explicit safe
    copy boundary for future persistence/evidence/log/TUI consumers.
- RED first: the five new focused regressions produced `4 failed, 1 passed`. Failures
  were the expected `RecursionError`, path corruption, lowercase percent-escape leak,
  and missing `ApiResponse.redacted_copy`; the already-supported exact configured
  replacement in a URL path remained green. A follow-up self-review added three RED
  cases for quoted values ending at a trailing backslash or backslash-newline and an
  exact configured port; all initially failed and then passed after position-boundary
  corrections.
- GREEN implementation:
  - `Redactor.text()` now uses an iterative position scanner over assignment prefixes.
    Safe keys are emitted once and only their following value position is scanned;
    sensitive quoted/unquoted value bounds are found iteratively and replaced. A
    2,000-wrapper regression passes, and an additional local bounded probe handled
    100,000 wrappers in about 0.10 seconds without recursion.
  - Request secret collection now adds lowercase-hex variants of both URL-quoted forms,
    changing only `%XX` hex letters and preserving unencoded character case.
  - URL redaction remains structured: sensitive query values and passwords are masked,
    configured/request-derived exact values are replaced per URL component, userinfo is
    safe, and malformed URL validation still fails closed. Generic assignment masking
    is no longer applied to paths or safe query values.
  - `ApiResponse.redacted_copy(redactor)` applies Redactor to URL, headers, body, and
    content type while retaining response metadata. `docs/ARCHITECTURE.md` declares that
    persistence, evidence, logs, and TUI must consume this view, while assertions may use
    a raw response only as a transient internal value. The policy explicitly does not
    claim to infer arbitrary unknown secrets or mask ordinary body text wholesale.
- Focused regression: `uv run pytest tests/test_api_redaction.py tests/test_api_models.py
  tests/test_api_transport.py -q` passed (`122 passed`).
- Full regression: `uv run pytest -q` passed (`486 passed, 1 skipped in 5.36s`); the
  skip is the Windows-only native ConPTY smoke.
- Static/dependency checks: `uv lock --check`, `uv run ruff check .`,
  `uv run ruff format --check .`, and `git diff --check` all passed after running the
  formatter on the final working tree.
