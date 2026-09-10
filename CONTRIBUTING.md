# Contributing to CSBox

CSBox welcomes reproducible bug reports, focused fixes, documentation improvements, tests, and platform evidence that make local lab recording and course-project delivery easier to trust. Keep contributions within the product boundary: CSBox records, checks, organizes, and packages user-provided work; it does not generate coursework, conclusions, answers, or generic AI/cloud-platform features.

## Bug reports

Use the **Bug report** issue form when possible. Include the CSBox version, OS, relevant terminal or shell, Python version, command or workflow, expected and actual behavior, and minimal reproduction steps. Attach logs only after removing credentials and private project data. Do not put passwords, API tokens, private keys, cookies, or other secrets in an Issue.

## Feature requests

Use the **Feature request** form. Start with the real user problem and current workaround, then describe the desired behavior and why it belongs in CSBox. Proposals should support the local-first, evidence, checking, or safe-packaging scope; vague requests to “add AI” or turn CSBox into a general platform are out of scope.

## Development setup

CSBox supports Python 3.11 and newer. From a checkout:

```bash
git clone https://github.com/sfwang3/CSBox.git
cd CSBox
uv sync
uv run pytest tests/test_readme_contract.py tests/test_distribution.py -q
uv run ruff check .
uv run ruff format --check .
uv build
```

Use `uv run pytest <targeted tests>` while iterating, then run the full suite before opening a pull request. The supported user path remains `csbox` from a course or project directory; `uv run csbox` is useful while developing from the checkout.

## Repository layout

- `src/csbox/` — runtime package and user workflows.
- `tests/` — deterministic unit, integration, packaging, and platform-boundary tests.
- `docs/` — public architecture, format, terminal, and security-boundary documentation.
- `README.md` / `README.en.md` / `CHANGELOG.md` — user-facing product and release information.
- `.github/` — issue forms, pull request guidance, and CI/release workflows.

## Tests, style, and display width

Start with a targeted failing test for a new behavior or regression, implement the smallest fix, and then run the affected tests. Keep Ruff and `ruff format --check` clean. Simplified Chinese and other CJK text are first-class: terminal and TUI layout must use display-cell width (`wcwidth`) semantics rather than string length. Test mixed ASCII/CJK text, combining characters, truncation, padding, tables, and non-ASCII paths when a change affects layout or filenames.

## Windows and Linux

Both Windows and Unix-like systems are supported. Changes touching terminals, shells, PTYs/ConPTY, resize, keyboard handling, or process cleanup need platform-aware tests and a clear affected-boundary note. Linux or WSL simulation does not prove native Windows behavior; do not describe it as such in a pull request.

## Pull requests

Keep a pull request focused and explain what changed, why, how it was tested, and the risk or affected area. Add or update targeted tests when behavior changes, update public docs when user-facing behavior changes, and do not commit secrets or test credentials. Review the checklist in `.github/PULL_REQUEST_TEMPLATE.md` before submitting.

## Security reporting

[`docs/SECURITY.md`](docs/SECURITY.md) documents product security boundaries. This repository currently has no verified private vulnerability-reporting channel enabled. Do not disclose vulnerability details or secrets in a public Issue. If a maintainer later enables a verified private channel, this section and the issue configuration should be updated together.

## Academic-integrity boundary

Contributions must preserve the rule that students remain responsible for experiment procedures, evidence truthfulness, analysis, conclusions, and coursework text. CSBox may help record, check, organize, and export material supplied by the user; it must not generate answers, conclusions, or fabricated evidence.

## Release notes

Published releases should have a short curated body with truthful highlights, an explicit product-scope note, install and upgrade commands, PyPI/changelog links, and a comparison link. The release workflow may generate a starting draft; the maintainer finalizes the public body before treating the release as complete.
