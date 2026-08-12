from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import csbox.check.rules as rules
from csbox.check.models import CheckStatus


def run_deep_secret_scan(root: Path):
    implementation = getattr(rules, "run_deep_secret_scan", None)
    assert callable(implementation), "run_deep_secret_scan is not implemented"
    return implementation(root)


def test_missing_gitleaks_is_an_explicit_skip(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(rules.shutil, "which", lambda name: None)

    finding = run_deep_secret_scan(tmp_path)

    assert finding.status is CheckStatus.SKIP
    assert finding.category == "deep-secret-scan"
    assert "PASS" not in finding.message
    assert "gitleaks" in finding.message


def test_gitleaks_is_invoked_with_controlled_argv_and_exit_semantics(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[tuple[list[str], dict[str, object]]] = []
    secret = "CSBOX_SECRET_SENTINEL_task17_gitleaks_env"
    monkeypatch.setenv("CSBOX_VAR_GITLEAKS_SECRET", secret)
    monkeypatch.setattr(rules.shutil, "which", lambda name: "/usr/bin/gitleaks")

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 1)

    monkeypatch.setattr(rules.subprocess, "run", fake_run)

    finding = run_deep_secret_scan(tmp_path)

    assert finding.status is CheckStatus.FAIL
    assert calls[0][0] == [
        "/usr/bin/gitleaks",
        "detect",
        "--source",
        str(tmp_path.resolve()),
        "--no-banner",
        "--redact",
        "--exit-code",
        "1",
    ]
    assert calls[0][1]["shell"] is False
    assert calls[0][1]["stdout"] is subprocess.DEVNULL
    assert calls[0][1]["stderr"] is subprocess.DEVNULL
    child_env = calls[0][1]["env"]
    assert isinstance(child_env, dict)
    assert "CSBOX_VAR_GITLEAKS_SECRET" not in child_env
    assert secret not in child_env.values()
    assert finding.path is None


@pytest.mark.parametrize(
    "error",
    [subprocess.TimeoutExpired(["gitleaks"], 1), OSError("permission denied")],
)
def test_gitleaks_execution_errors_are_safe_warnings(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, error: Exception
) -> None:
    monkeypatch.setattr(rules.shutil, "which", lambda name: "/usr/bin/gitleaks")

    def fail_run(*args, **kwargs):
        raise error

    monkeypatch.setattr(rules.subprocess, "run", fail_run)

    finding = run_deep_secret_scan(tmp_path)

    assert finding.status is CheckStatus.WARN
    assert finding.category == "deep-secret-scan"
    assert "permission denied" not in finding.message
