from __future__ import annotations

from pathlib import Path

import pytest

from csbox.check.detectors import FileInventory
from csbox.check.models import CheckContext, CheckStatus
from csbox.check.rules import DEFAULT_RULES, PrivateKeyRule


def test_rules_report_sensitive_locations_without_secret_content(tmp_path: Path) -> None:
    fixture_value = "super-secret-value-123456"
    setting_name = "api" + "_key"
    (tmp_path / "README.md").write_text("# demo\n", encoding="utf-8")
    (tmp_path / ".env").write_text(f"TOKEN={fixture_value}\n", encoding="utf-8")
    (tmp_path / "private.pem").write_text(
        "-----BEGIN OPENSSH PRIVATE KEY-----\nnot emitted\n", encoding="utf-8"
    )
    (tmp_path / "settings.py").write_text(f'{setting_name} = "{fixture_value}"\n', encoding="utf-8")
    (tmp_path / "notes.txt").write_text(
        "Windows C:\\Users\\测试用户\\桌面\\实验一\nUnix /home/student/project\n",
        encoding="utf-8",
    )
    inventory = FileInventory.build(tmp_path)
    context = CheckContext(root=tmp_path, inventory=inventory)

    findings = [finding for rule in DEFAULT_RULES for finding in rule.evaluate(context)]
    rendered = "\n".join(finding.model_dump_json() for finding in findings)

    assert any(finding.status is CheckStatus.FAIL for finding in findings)
    assert any(finding.category == "windows-absolute-path" for finding in findings)
    assert any(finding.category == "unix-absolute-path" for finding in findings)
    assert fixture_value not in rendered
    assert all(
        finding.path is None or not str(finding.path).startswith(fixture_value)
        for finding in findings
    )


def test_rules_return_pass_or_skip_when_project_is_clean(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("# clean\n", encoding="utf-8")
    inventory = FileInventory.build(tmp_path)
    context = CheckContext(root=tmp_path, inventory=inventory)

    findings = [finding for rule in DEFAULT_RULES for finding in rule.evaluate(context)]

    assert findings
    assert all(finding.status in {CheckStatus.PASS, CheckStatus.SKIP} for finding in findings)


def test_private_key_rule_uses_inventory_cache_instead_of_opening_the_file_directly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    private_key = tmp_path / "private.pem"
    private_key.write_text("-----BEGIN OPENSSH PRIVATE KEY-----\nnot emitted\n", encoding="utf-8")
    inventory = FileInventory.build(tmp_path)
    context = CheckContext(root=tmp_path, inventory=inventory)

    original_open = Path.open

    def reject_direct_open(self: Path, *args, **kwargs):
        if self == private_key:
            raise AssertionError("private-key rule must use inventory")
        return original_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", reject_direct_open)

    findings = PrivateKeyRule().evaluate(context)

    assert findings[0].status is CheckStatus.FAIL
    assert inventory.text_scan_stats.read_count == 1
