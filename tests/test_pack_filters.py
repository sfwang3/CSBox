from __future__ import annotations

from pathlib import Path

import pytest

from csbox.pack.filters import PackFilter, PackSafetyError


def test_filter_excludes_artifacts_but_keeps_source_named_build_and_target(
    tmp_path: Path,
) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "build.py").write_text("print('build')")
    (tmp_path / "src" / "target.py").write_text("print('target')")
    (tmp_path / "node_modules" / "pkg").mkdir(parents=True)
    (tmp_path / "node_modules" / "pkg" / "index.js").write_text("ignored")
    (tmp_path / "cache" / "nested").mkdir(parents=True)
    (tmp_path / "cache" / "nested" / "x.txt").write_text("ignored")
    (tmp_path / ".cache" / "nested").mkdir(parents=True)
    (tmp_path / ".cache" / "nested" / "x.txt").write_text("ignored")
    (tmp_path / ".vscode").mkdir()
    (tmp_path / ".vscode" / "settings.json").write_text("ignored")
    (tmp_path / ".env.example").write_text("TOKEN=change-me")

    candidates = PackFilter(tmp_path).candidates()

    names = {candidate.relative.as_posix() for candidate in candidates}
    assert "src/build.py" in names
    assert "src/target.py" in names
    assert ".env.example" in names
    assert "node_modules/pkg/index.js" not in names
    assert "cache/nested/x.txt" not in names
    assert ".cache/nested/x.txt" not in names
    assert ".vscode/settings.json" not in names


@pytest.mark.parametrize(
    "name",
    [".env", ".env.local", ".env.production", "private.pem", "notes.txt"],
)
def test_filter_refuses_real_env_and_private_key_headers(tmp_path: Path, name: str) -> None:
    path = tmp_path / name
    if name.startswith(".env"):
        path.write_text("TOKEN=secret-value")
    elif name == "private.pem":
        path.write_text("-----BEGIN PRIVATE KEY-----\nsecret\n")
    else:
        path.write_text("-----BEGIN OPENSSH PRIVATE KEY-----\nsecret\n")

    with pytest.raises(PackSafetyError):
        PackFilter(tmp_path).candidates()


def test_filter_keeps_env_example_but_excludes_runtime_state(tmp_path: Path) -> None:
    (tmp_path / ".env.example").write_text("TOKEN=change-me")
    (tmp_path / ".csbox" / "sessions" / "session-1").mkdir(parents=True)
    (tmp_path / ".csbox" / "sessions" / "session-1" / "session.cast").write_text("cast")

    names = {candidate.relative.as_posix() for candidate in PackFilter(tmp_path).candidates()}

    assert ".env.example" in names
    assert all(not name.startswith(".csbox/") for name in names)


def test_filter_excludes_old_archives_before_sensitive_content_scan(tmp_path: Path) -> None:
    (tmp_path / "old.zip").write_bytes(b"-----BEGIN PRIVATE KEY-----\narchive\n")

    selection = PackFilter(tmp_path).select()

    assert "old.zip:archive" in selection.excluded
    assert selection.rejected == ()


def test_filter_rejects_path_traversal_patterns_and_skips_symlink_escape(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside-pack.txt"
    outside.write_text("outside")
    link = tmp_path / "escape.txt"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symlink creation is unavailable")

    assert all(
        candidate.relative.as_posix() != "escape.txt"
        for candidate in PackFilter(tmp_path).candidates()
    )
    with pytest.raises(ValueError):
        PackFilter(tmp_path, include=("../outside-pack.txt",))
    with pytest.raises(ValueError):
        PackFilter(tmp_path, exclude=(r"..\outside-pack.txt",))
