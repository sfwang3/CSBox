from __future__ import annotations

from pathlib import Path

import pytest

import csbox.config.loader as loader_module
from csbox.config import (
    ApiConfig,
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
    assert config.api == ApiConfig()


@pytest.mark.parametrize(
    ("field", "value"),
    [("response_max_bytes", 0), ("timeout_seconds", 0.0)],
)
def test_load_config_reports_api_field_and_repair_without_exposing_value(
    tmp_path: Path, field: str, value: int | float
) -> None:
    paths = _paths(tmp_path)
    paths.user.write_text(f"[api]\n{field} = {value}\n", encoding="utf-8")

    with pytest.raises(ConfigurationError) as error:
        load_config(tmp_path, paths=paths)

    message = str(error.value)
    assert str(paths.user) in message
    assert field in message
    assert "修改" in message or "设置" in message
    assert f": {value}" not in message


def test_api_config_is_strict_and_has_bounded_defaults() -> None:
    config = ApiConfig(variables={"BASE_URL": "https://example.test"})

    assert config.response_max_bytes == 262144
    assert config.timeout_seconds == 10.0
    with pytest.raises(ValueError):
        ApiConfig(response_max_bytes=0)
    with pytest.raises(ValueError):
        ApiConfig(timeout_seconds=0.0)
    with pytest.raises(ValueError):
        ApiConfig.model_validate({"timeout_seconds": "10"})


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


def test_load_config_maps_deep_toml_recursion_to_a_safe_error(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    paths.user.write_text("x = " + ("[" * 10_000) + ("0" + "]" * 10_000), encoding="utf-8")

    with pytest.raises(ConfigurationError) as error:
        load_config(tmp_path, paths=paths)

    assert error.value.path == paths.user
    assert "安全读取或解析" in str(error.value)


def test_load_config_wraps_invalid_utf8_with_chinese_error(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    paths.user.write_bytes(b"[lab]\nshell = \xff\n")

    with pytest.raises(ConfigurationError) as error:
        load_config(tmp_path, paths=paths)

    assert error.value.path == paths.user
    assert "配置错误" in str(error.value)


def test_load_config_reads_toml_through_a_bounded_regular_file_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _paths(tmp_path)
    paths.user.write_text('[lab]\nshell = "bash"\n', encoding="utf-8")
    observed: list[tuple[Path, int]] = []
    from csbox.core.safe_paths import read_regular_text as real_read_regular_text

    def observed_read(path: Path, *, max_bytes: int, encoding: str = "utf-8") -> str:
        observed.append((Path(path), max_bytes))
        return real_read_regular_text(path, max_bytes=max_bytes, encoding=encoding)

    monkeypatch.setattr(loader_module, "read_regular_text", observed_read, raising=False)

    assert load_config(tmp_path, paths=paths).lab.shell == "bash"
    assert observed == [(paths.user, observed[0][1])]
    assert observed[0][1] > 0


def test_save_project_config_refuses_a_symlinked_private_parent(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    linked = tmp_path / ".csbox"
    try:
        linked.symlink_to(outside, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"symlinks are unavailable: {error}")

    with pytest.raises(ConfigurationError, match="配置错误"):
        save_project_config(CSBoxConfig(), tmp_path)

    assert list(outside.iterdir()) == []


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


def test_save_project_config_uses_the_shared_atomic_writer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[Path] = []
    real_write = loader.atomic_write_text

    def write(destination: Path, text: str) -> None:
        calls.append(Path(destination))
        real_write(destination, text)

    monkeypatch.setattr(loader, "atomic_write_text", write)
    config_path = save_project_config(CSBoxConfig(lab=LabConfig(shell="bash")), tmp_path)

    assert config_path == tmp_path / ".csbox/config.toml"
    assert calls == [config_path]
    loaded_config = load_config(
        tmp_path, paths=ConfigPaths(user=tmp_path / "user.toml", project=config_path)
    )
    assert loaded_config.lab.shell == "bash"


def test_save_project_config_round_trips_api_settings(tmp_path: Path) -> None:
    config = CSBoxConfig(api=ApiConfig(variables={"BASE_URL": "https://example.test"}))

    config_path = save_project_config(config, tmp_path)
    loaded = load_config(
        tmp_path, paths=ConfigPaths(user=tmp_path / "user.toml", project=config_path)
    )

    assert loaded.api.variables == {"BASE_URL": "https://example.test"}
