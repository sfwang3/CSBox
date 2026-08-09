from __future__ import annotations

import os
from pathlib import Path

import pytest

from csbox.config import (
    ConfigPaths,
    ConfigurationError,
    CSBoxConfig,
    LabConfig,
    load_config,
    loader,
    save_project_config,
)


def _paths(tmp_path: Path) -> ConfigPaths:
    return ConfigPaths(user=tmp_path / "user.toml", project=tmp_path / ".csbox/config.toml")


def test_load_config_uses_schema_defaults(tmp_path: Path) -> None:
    config = load_config(tmp_path, paths=_paths(tmp_path))

    assert config.locale == "zh_CN"
    assert config.lab.capture_key == "f12"
    assert config.render.theme == "dark"
    assert config.pack.filename == "{id}-{name}-{course}.zip"
    assert config.check.large_file_threshold_mb == 50


def test_project_config_overrides_user_config(tmp_path: Path) -> None:
    paths = ConfigPaths(user=tmp_path / "user.toml", project=tmp_path / ".csbox/config.toml")
    paths.user.write_text('[lab]\nshell = "bash"\n', encoding="utf-8")
    paths.project.parent.mkdir()
    paths.project.write_text('[lab]\nshell = "zsh"\n', encoding="utf-8")

    assert load_config(tmp_path, paths=paths).lab.shell == "zsh"


def test_cli_overrides_have_highest_precedence(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    paths.user.write_text('[lab]\nshell = "bash"\n', encoding="utf-8")
    paths.project.parent.mkdir()
    paths.project.write_text('[lab]\nshell = "zsh"\n', encoding="utf-8")

    config = load_config(tmp_path, paths=paths, overrides={"lab": {"shell": "pwsh"}})

    assert config.lab.shell == "pwsh"


def test_load_config_wraps_bad_toml_with_chinese_error(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    paths.user.write_text("[lab\nshell = 'bash'\n", encoding="utf-8")

    with pytest.raises(ConfigurationError) as error:
        load_config(tmp_path, paths=paths)

    assert error.value.path == paths.user
    assert "配置错误" in str(error.value)


def test_load_config_wraps_invalid_utf8_with_chinese_error(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    paths.user.write_bytes(b"[lab]\nshell = \xff\n")

    with pytest.raises(ConfigurationError) as error:
        load_config(tmp_path, paths=paths)

    assert error.value.path == paths.user
    assert "配置错误" in str(error.value)


@pytest.mark.parametrize(
    ("section", "setting"),
    [("lab", 'capture_key = "ctrl-c"'), ("render", 'theme = "solarized"')],
)
def test_load_config_rejects_invalid_enum_values(
    tmp_path: Path, section: str, setting: str
) -> None:
    paths = _paths(tmp_path)
    paths.user.write_text(f"[{section}]\n{setting}\n", encoding="utf-8")

    with pytest.raises(ConfigurationError) as error:
        load_config(tmp_path, paths=paths)

    assert error.value.path == paths.user
    assert section in error.value.message


@pytest.mark.parametrize("content", ["unexpected = true\n", "[lab]\nunknown = true\n"])
def test_load_config_rejects_unknown_fields(tmp_path: Path, content: str) -> None:
    paths = _paths(tmp_path)
    paths.user.write_text(content, encoding="utf-8")

    with pytest.raises(ConfigurationError) as error:
        load_config(tmp_path, paths=paths)

    assert error.value.path == paths.user
    assert "extra_forbidden" in error.value.message


def test_config_paths_use_windows_appdata_when_injected(tmp_path: Path) -> None:
    paths = ConfigPaths.from_cwd(
        tmp_path,
        environ={"APPDATA": str(tmp_path / "AppData/Roaming"), "HOME": str(tmp_path / "home")},
    )

    assert paths.user == tmp_path / "AppData/Roaming/CSBox/config.toml"
    assert paths.project == tmp_path / ".csbox/config.toml"


def test_config_paths_use_xdg_config_home_when_injected(tmp_path: Path) -> None:
    paths = ConfigPaths.from_cwd(
        tmp_path,
        environ={"XDG_CONFIG_HOME": str(tmp_path / "xdg"), "HOME": str(tmp_path / "home")},
    )

    assert paths.user == tmp_path / "xdg/csbox/config.toml"


def test_config_paths_do_not_call_home_when_home_is_injected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_home() -> Path:
        raise AssertionError("Path.home() must not be called")

    monkeypatch.setattr(Path, "home", fail_home)

    paths = ConfigPaths.from_cwd(tmp_path, environ={"HOME": str(tmp_path / "home")})

    assert paths.user == tmp_path / "home/.config/csbox/config.toml"


def test_save_project_config_replaces_same_directory_temporary_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[Path, Path]] = []
    real_replace = os.replace

    def replace(source: str | Path, destination: str | Path) -> None:
        calls.append((Path(source), Path(destination)))
        real_replace(source, destination)

    monkeypatch.setattr(loader.os, "replace", replace)
    config_path = save_project_config(CSBoxConfig(lab=LabConfig(shell="bash")), tmp_path)

    assert config_path == tmp_path / ".csbox/config.toml"
    assert calls == [(calls[0][0], config_path)]
    assert calls[0][0].parent == config_path.parent
    loaded_config = load_config(
        tmp_path, paths=ConfigPaths(user=tmp_path / "user.toml", project=config_path)
    )
    assert loaded_config.lab.shell == "bash"
