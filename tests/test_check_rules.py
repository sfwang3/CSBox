from __future__ import annotations

from pathlib import Path

from csbox.check.detectors import FileInventory
from csbox.check.models import CheckContext, CheckStatus
from csbox.check.rules import DEFAULT_RULES


def test_rules_report_sensitive_locations_without_secret_content(tmp_path: Path) -> None:
    secret = "super-secret-value-123456"
    (tmp_path / "README.md").write_text("# demo\n")
    (tmp_path / ".env").write_text(f"TOKEN={secret}\n")
    (tmp_path / "private.pem").write_text("-----BEGIN OPENSSH PRIVATE KEY-----\nnot emitted\n")
    (tmp_path / "settings.py").write_text(f'api_key = "{secret}"\n')
    (tmp_path / "notes.txt").write_text(
        "Windows C:\\Users\\测试用户\\桌面\\实验一\nUnix /home/student/project\n"
    )
    inventory = FileInventory.build(tmp_path)
    context = CheckContext(root=tmp_path, inventory=inventory)

    findings = [finding for rule in DEFAULT_RULES for finding in rule.evaluate(context)]
    rendered = "\n".join(finding.model_dump_json() for finding in findings)

    assert any(finding.status is CheckStatus.FAIL for finding in findings)
    assert any(finding.category == "windows-absolute-path" for finding in findings)
    assert any(finding.category == "unix-absolute-path" for finding in findings)
    assert secret not in rendered
    assert all(
        finding.path is None or not str(finding.path).startswith(secret) for finding in findings
    )


def test_rules_return_pass_or_skip_when_project_is_clean(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("# clean\n")
    inventory = FileInventory.build(tmp_path)
    context = CheckContext(root=tmp_path, inventory=inventory)

    findings = [finding for rule in DEFAULT_RULES for finding in rule.evaluate(context)]

    assert findings
    assert all(finding.status in {CheckStatus.PASS, CheckStatus.SKIP} for finding in findings)
