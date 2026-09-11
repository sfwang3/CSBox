from __future__ import annotations

import importlib
import json
from pathlib import Path

from typer.testing import CliRunner

from csbox.check.models import CheckStatus
from csbox.cli.main import app
from csbox.submission.models import (
    SubmissionArchiveSummary,
    SubmissionCheckSummary,
    SubmissionError,
    SubmissionPlan,
    SubmissionReadiness,
    SubmissionResult,
    SubmissionVerifyResult,
)

cli_module = importlib.import_module("csbox.cli.main")


def _plan(tmp_path: Path, readiness: SubmissionReadiness) -> SubmissionPlan:
    return SubmissionPlan(
        evidence_set_id="set-1",
        evidence_fingerprint="a" * 64,
        report_profile_fingerprint="b" * 64,
        pack_source_fingerprint="c" * 64,
        destination=tmp_path / "handoff",
        archive_filename="project.zip",
        check=SubmissionCheckSummary(
            status=CheckStatus.WARN
            if readiness is SubmissionReadiness.WARNING
            else CheckStatus.PASS,
            warning_count=1 if readiness is SubmissionReadiness.WARNING else 0,
            deep_requested=True,
        ),
        project_archive=SubmissionArchiveSummary(verified=True, contains_manifest=True),
        readiness=readiness,
        warnings=("有一项检查提示。",) if readiness is SubmissionReadiness.WARNING else (),
        blockers=("请先修复检查问题。",) if readiness is SubmissionReadiness.BLOCKED else (),
    )


class FakeSubmissionService:
    def __init__(self, project_dir: Path, plan: SubmissionPlan) -> None:
        self.project_dir = project_dir
        self.plan_result = plan
        self.plan_calls: list[dict[str, object]] = []
        self.prepare_calls: list[SubmissionPlan] = []
        self.prepare_force_calls: list[bool] = []
        self.prepare_error: Exception | None = None

    def plan(self, evidence_set_id: str, *, destination=None, deep=False):
        self.plan_calls.append(
            {"evidence_set_id": evidence_set_id, "destination": destination, "deep": deep}
        )
        return self.plan_result

    def prepare(self, plan, *, force=False):
        self.prepare_calls.append(plan)
        self.prepare_force_calls.append(force)
        if self.prepare_error:
            raise self.prepare_error
        return SubmissionResult(
            destination=plan.destination,
            report_path=plan.destination / plan.report_filename,
            archive_path=plan.destination / plan.archive_filename,
            manifest_path=plan.destination / "submission-manifest.json",
            check_status=plan.check.status,
            warning_count=plan.check.warning_count,
            verified=True,
            warnings=plan.warnings,
        )


def test_submit_help_lists_workflows() -> None:
    result = CliRunner().invoke(app, ["submit", "--help"])

    assert result.exit_code == 0
    assert "plan" in result.stdout
    assert "prepare" in result.stdout
    assert "verify" in result.stdout


def test_submit_plan_states_and_safe_json(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    service = FakeSubmissionService(tmp_path, _plan(tmp_path, SubmissionReadiness.WARNING))
    monkeypatch.setattr(cli_module, "create_submission_service", lambda root: service)

    result = CliRunner().invoke(
        app,
        ["submit", "plan", "set-1", "--output", "handoff", "--deep", "--json"],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["schema_version"] == 1
    assert payload["readiness"] == "WARNING"
    assert payload["source_root"] == "."
    assert "handoff" in payload["final_names"]["destination"]
    assert str(tmp_path) not in result.stdout
    assert "secret-like-test-value" not in result.stdout
    assert service.plan_calls[0]["deep"] is True


def test_submit_plan_blocked_exits_one(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    service = FakeSubmissionService(tmp_path, _plan(tmp_path, SubmissionReadiness.BLOCKED))
    monkeypatch.setattr(cli_module, "create_submission_service", lambda root: service)

    result = CliRunner().invoke(app, ["submit", "plan", "set-1", "--plain"])

    assert result.exit_code == 1
    assert "BLOCKED" in result.stdout
    assert str(tmp_path) not in result.stdout


def test_submit_prepare_warn_succeeds_and_forwards_options(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    service = FakeSubmissionService(tmp_path, _plan(tmp_path, SubmissionReadiness.WARNING))
    monkeypatch.setattr(cli_module, "create_submission_service", lambda root: service)

    result = CliRunner().invoke(
        app,
        ["submit", "prepare", "set-1", "--output", "out", "--force", "--deep", "--plain"],
    )

    assert result.exit_code == 0
    assert "提交材料已准备" in result.stdout or "准备提交材料" in result.stdout
    assert "提交成功" not in result.stdout
    assert service.plan_calls[0]["deep"] is True
    assert service.plan_calls[0]["destination"] == tmp_path / "out"
    assert service.prepare_force_calls == [True]


def test_submit_prepare_failure_is_actionable(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    service = FakeSubmissionService(tmp_path, _plan(tmp_path, SubmissionReadiness.READY))
    service.prepare_error = SubmissionError("提交预览未通过，请重新预览。", kind="blocked")
    monkeypatch.setattr(cli_module, "create_submission_service", lambda root: service)

    result = CliRunner().invoke(app, ["submit", "prepare", "set-1"])

    assert result.exit_code == 1
    assert "提交预览未通过" in result.stdout
    assert str(tmp_path) not in result.stdout


def test_submit_verify_pass_and_fail(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    verifier = type("Verifier", (), {})()
    verifier.result = SubmissionVerifyResult(status="PASS", verified=True, files=("report.docx",))
    verifier.verify = lambda path: verifier.result
    monkeypatch.setattr(cli_module, "SubmissionVerifier", lambda: verifier)

    passed = CliRunner().invoke(app, ["submit", "verify", str(tmp_path), "--json"])
    assert passed.exit_code == 0
    assert json.loads(passed.stdout)["status"] == "PASS"
    assert str(tmp_path) not in passed.stdout

    verifier.result = SubmissionVerifyResult(
        status="FAIL", verified=False, errors=("提交文件校验失败。",)
    )
    failed = CliRunner().invoke(app, ["submit", "verify", str(tmp_path), "--plain"])
    assert failed.exit_code == 1
    assert "FAIL" in failed.stdout


def test_submit_plain_and_json_conflict() -> None:
    result = CliRunner().invoke(app, ["submit", "plan", "set-1", "--plain", "--json"])

    assert result.exit_code != 0
    assert "--plain" in result.output
    assert "--json" in result.output
