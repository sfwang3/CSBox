## Task 6 evidence — 2026-08-11

- Approved base: `355f763` (`fix: close remaining Task 5 review findings`); work was
  performed directly on the current `main` checkout as requested, without a worktree.
- Initial RED: `uv run pytest tests/test_api_assertions.py tests/test_api_runner.py -q`
  stopped during collection with two expected `ModuleNotFoundError` failures because
  `csbox.api.assertions` and `csbox.api.runner` did not exist.
- Assertion GREEN: after adding `jsonpath-ng>=1.7,<2` and the evaluator,
  `uv run pytest tests/test_api_assertions.py -q` passed (`19 passed`). The evaluator
  supports exact/class/range status, case-insensitive header lookup, JSONPath
  exists/equals/type, body contains/not-contains, safe unsupported configurations, and
  batch evaluation with one JSON decode per response.
- Runner GREEN: the first complete focused Task 6 run passed (`28 passed`). `ApiRun` and
  `ApiRunResult` gained explicit `PASS`/`FAIL`/`CONFIG_ERROR`/`RUNTIME_ERROR` status so
  later CLI work can map stable exit semantics without inspecting localized messages.
  The async runner merges scenario variables with supplied overrides, executes file
  order through `ApiTransport`, continues by default, honors fail-fast, and stores only
  redacted scenario/request/response/assertion/error views.
- Security self-review RED: five additional regressions initially exposed four failures:
  sensitive header/JSONPath scalar values lost their field context before redaction,
  Python accepted non-standard JSON `NaN`, and sensitive variable values were not added
  to a run-scoped exact-value policy. A later direct-Authorization echo test also failed
  before request-derived step redaction was added. All cases were fixed at their source:
  comparisons still use transient raw values, display/storage values come from
  context-aware redacted views, strict JSON rejects non-standard constants, and
  run/step redactors derive exact values from sensitive variables and request fields.
- During review, `_run_redactor` briefly lost its return because an `apply_patch` context
  placed it after a later helper. Direct line-number inspection identified the patch
  placement error; the `return Redactor(...)` is now inside `_run_redactor`, and the
  focused runner regression proves the returned instance is used.
- Final Task 6 focused tests: `33 passed`. Full API focused regression (models,
  redaction, variables, scenario, transport, assertions, runner): `195 passed`.
- Full repository regression: `519 passed, 1 skipped`; the only skip is the existing
  Windows-native ConPTY smoke on this Linux environment.
- Dependency/scope audit: `jsonpath-ng==1.8.0` is locked; assertions use
  `jsonpath_ng.parse`; neither new domain module imports Textual, Typer, Rich, or HTTPX;
  runner depends on the transport protocol only. CLI, persistence, evidence export, and
  TUI were not implemented.
- Fresh pre-commit gate: Task 6 focused `33 passed`; full pytest `519 passed, 1
  skipped`; `uv lock --check`, `uv run ruff check .`, `uv run ruff format --check .`,
  and `git diff --check` all exited successfully. Ruff reported all 139 files formatted.

## Task 6 Critical review fix — 2026-08-11

- Review base: `db52967` (`feat: add API assertions and runner`); the fix was made
  directly in the current checkout without a worktree. CLI, persistence, evidence,
  and TUI remain out of scope.
- Root cause: Task 5 parsed JSON response bodies before recursive redaction, but
  `ApiResponse.redacted_copy()` independently called only `Redactor.text()`. The text
  assignment scanner sees literal spelling rather than decoded JSON keys, so a
  response-only value under `to\\u006ben` or a key/colon split across lines could enter
  the stored `ApiRun`, `model_dump_json()`, and `repr()` unchanged.
- RED first: seven focused cases failed for the intended reasons. Four direct helper
  tests reported missing `Redactor.response_body`, and the model/runner cases reproduced
  the escaped-key sentinel leak. Coverage includes valid escaped and multiline JSON,
  malformed JSON fallback, configured values in safe fields, non-finite JSON constants,
  and a runner assertion that consumes the raw response before storage redaction.
- GREEN implementation: `Redactor.response_body()` is now the single content-type-aware
  response-body policy. JSON and `+json` bodies use strict `json.loads` callbacks that
  reject non-standard constants and non-finite parsed floats, then recursively apply
  `json_value()` and serialize with `allow_nan=False`. Parse, validation, serialization,
  or recursion failures fall back to the existing iterative text masker; ordinary text
  continues to use `text()` unchanged.
- `ApiResponse.redacted_copy()` and `HttpxTransport` both call the shared helper; the
  transport's duplicate response JSON parser was removed. Assertions still receive the
  original transient response, while the runner test proves stored response, expected,
  actual, `model_dump_json()`, and `repr()` contain no raw sentinel.
- Focused API regression: `162 passed`. Full repository regression: `526 passed, 1
  skipped`; the only skip is the existing Windows-native ConPTY smoke on Linux.
- Pre-commit checks: `uv lock --check`, `uv run ruff check .`,
  `uv run ruff format --check .`, and `git diff --check` all exited successfully; Ruff
  reported all 139 files formatted.

## Task 6 review findings follow-up — 2026-08-11

- Review base: `c0fcf40` (`fix: harden response JSON redaction`); the findings were
  fixed directly in the current checkout without a worktree. CLI, persistence, evidence
  export, and TUI remain out of scope.
- Root causes: malformed JSON fell back to a literal quoted-key scanner whose separator
  excluded CR/LF and whose key normalization did not decode JSON-style escapes. Separately,
  `HttpxTransport` returned only a redacted `ApiResponse`, so the runner evaluated JSONPath
  assertions against redacted values while fake transports still happened to return raw
  responses.
- RED first: eight targeted regressions failed for the intended reasons. They covered
  Unicode-escaped sensitive keys, multiline key/colon whitespace, exact malformed-shape and
  safe-field preservation, a 2,000-layer iterative fallback, model/run serialization, the
  missing private assertion view, and an HTTPX adapter echo whose raw-token JSONPath equality
  incorrectly returned `FAIL`.
- GREEN implementation: the iterative assignment scanner now accepts CR/LF around separators
  and decodes bounded quoted-key escapes before field-name normalization without changing the
  original output slices or introducing recursion. `ApiResponse` now has a Pydantic
  `PrivateAttr` raw assertion view that is absent from `model_dump_json()` and `repr()`;
  ordinary responses default to themselves, while `redacted_copy()` constructs a fresh model
  with no private raw state.
- `HttpxTransport` keeps its public response fields redacted, attaches the raw response only to
  the transient private view, and the runner evaluates assertions through
  `response.assertion_view()` before storing only `response.redacted_copy(redactor)`. The HTTPX
  adapter regression now passes its raw-token equality assertion while the stored run contains
  no raw token; existing fake transports remain compatible.
- Focused regressions: the new RED set passed (`8 passed`), the four directly affected modules
  passed (`147 passed`), and the complete API suite passed (`209 passed`). Full repository
  regression passed (`533 passed, 1 skipped`); the skip is the existing Windows-native ConPTY
  smoke on Linux.
- Verification gate: `uv lock --check`, `uv run ruff check .`,
  `uv run ruff format --check .`, and `git diff --check` exited successfully after Ruff formatted
  the two changed files it identified.
