from __future__ import annotations

from pathlib import Path

import pytest

from csbox.check.detectors import FileInventory
from csbox.check.models import CheckContext, CheckStatus
from csbox.check.rules import EnvRule, HardCodedSecretRule, PrivateKeyRule
from csbox.pack.service import PackService, PackServiceError


def _context(root: Path) -> CheckContext:
    return CheckContext(root=root, inventory=FileInventory.build(root))


def test_hard_coded_secret_rule_suppresses_only_known_placeholder_values(
    tmp_path: Path,
) -> None:
    (tmp_path / "settings.py").write_text(
        'password = "password"\ntoken = "replace-me-token"\napi_key = "example-token"\n',
        encoding="utf-8",
    )

    findings = HardCodedSecretRule().evaluate(_context(tmp_path))

    assert findings[0].status is CheckStatus.PASS
    assert all(finding.status is not CheckStatus.FAIL for finding in findings)


def test_stream_scan_applies_the_same_placeholder_contract(tmp_path: Path) -> None:
    path = tmp_path / "large-example.py"
    path.write_bytes(b"# padding\n" * 30_000 + b'token = "replace-me-token"\n')

    findings = HardCodedSecretRule().evaluate(_context(tmp_path))

    assert findings[0].status is CheckStatus.PASS


@pytest.mark.parametrize(
    "value",
    (
        "replace-me-token-123",
        "my-example-token",
        "test-token",
        "demo-token",
        "password123",
    ),
)
def test_hard_coded_secret_rule_keeps_near_placeholder_values_as_failures(
    tmp_path: Path,
    value: str,
) -> None:
    (tmp_path / "settings.py").write_text(
        f'token = "{value}"\n',
        encoding="utf-8",
    )

    findings = HardCodedSecretRule().evaluate(_context(tmp_path))

    assert findings[0].status is CheckStatus.FAIL
    assert findings[0].path == Path("settings.py")
    assert findings[0].line == 1


def test_real_looking_secret_and_private_key_fixture_still_fail(
    tmp_path: Path,
) -> None:
    (tmp_path / "settings.py").write_text(
        'token = "sk_live_51N3aB7xQ2mL9pR4vC8dE0fG"\nsecret = "aB7!cD9@eF2#hJ5$kL8%"\n',
        encoding="utf-8",
    )
    (tmp_path / "id_ed25519").write_text(
        "-----BEGIN OPENSSH PRIVATE KEY-----\nsanitized fixture only\n",
        encoding="utf-8",
    )
    context = _context(tmp_path)

    secret_findings = HardCodedSecretRule().evaluate(context)
    key_findings = PrivateKeyRule().evaluate(context)

    assert len([item for item in secret_findings if item.status is CheckStatus.FAIL]) == 2
    assert key_findings[0].status is CheckStatus.FAIL


def test_placeholder_example_does_not_block_pack_and_remains_included(
    tmp_path: Path,
) -> None:
    (tmp_path / "README.md").write_text("# 课程项目\n", encoding="utf-8")
    (tmp_path / ".env.example").write_text(
        'TOKEN="replace-me-token"\n',
        encoding="utf-8",
    )
    destination = tmp_path / "交付.zip"

    service = PackService()
    plan = service.plan(tmp_path, destination=destination)

    assert ".env.example" in plan.included
    assert ".env.example:hard-coded-secret" not in plan.rejected
    report = service.pack(tmp_path, destination=destination, verify=True)
    assert report.destination == destination


def test_placeholder_inside_real_env_still_blocks_pack(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("# 课程项目\n", encoding="utf-8")
    (tmp_path / ".env").write_text('TOKEN="replace-me-token"\n', encoding="utf-8")

    context = _context(tmp_path)
    findings = EnvRule().evaluate(context)
    plan = PackService().plan(tmp_path, destination=tmp_path / "交付.zip")

    assert findings[0].status is CheckStatus.FAIL
    assert ".env:env" in plan.rejected


def test_real_looking_secret_still_blocks_pack(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("# 课程项目\n", encoding="utf-8")
    (tmp_path / "settings.py").write_text(
        'token = "sk_live_51N3aB7xQ2mL9pR4vC8dE0fG"\n',
        encoding="utf-8",
    )
    destination = tmp_path / "交付.zip"

    plan = PackService().plan(tmp_path, destination=destination)

    assert "settings.py:hard-coded-secret" in plan.rejected
    with pytest.raises(PackServiceError):
        PackService().pack(tmp_path, destination=destination)
