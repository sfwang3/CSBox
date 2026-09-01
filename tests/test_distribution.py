from __future__ import annotations

import email
import importlib.metadata
import os
import subprocess
import tarfile
import zipfile
from pathlib import Path

import pytest

from csbox import __version__

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SECRET_SENTINEL = b"CSBOX_SECRET_SENTINEL"
LOCAL_ONLY_MARKERS = (
    ".superpowers/",
    "docs/superpowers/",
    ".githooks/",
    "scripts/checkpoint.sh",
    "scripts/local-only-paths.sh",
    "scripts/push_clean.sh",
    "tests/",
    "docs/reference/",
)


def _build_distributions(tmp_path: Path) -> tuple[Path, Path]:
    output = tmp_path / "dist"
    subprocess.run(
        ["uv", "build", "--clear", "--out-dir", str(output)],
        cwd=PROJECT_ROOT,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return next(output.glob("*.whl")), next(output.glob("*.tar.gz"))


def _wheel_members(path: Path) -> set[str]:
    with zipfile.ZipFile(path) as archive:
        return set(archive.namelist())


def _sdist_members(path: Path) -> set[str]:
    with tarfile.open(path, "r:gz") as archive:
        members = {
            member.name.split("/", 1)[1] for member in archive.getmembers() if "/" in member.name
        }
    return {member.removeprefix("src/") for member in members}


def _runtime_requirements(tmp_path: Path) -> Path:
    result = subprocess.run(
        ["uv", "export", "--locked", "--no-dev", "--format", "requirements-txt"],
        cwd=PROJECT_ROOT,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    requirements = "\n".join(
        line for line in result.stdout.splitlines() if not line.startswith("-e .")
    )
    path = tmp_path / "runtime-requirements.txt"
    path.write_text(requirements + "\n", encoding="utf-8")
    return path


def _venv_python(venv: Path) -> Path:
    relative = Path("Scripts/python.exe") if os.name == "nt" else Path("bin/python")
    return venv / relative


def _venv_executable(venv: Path, name: str) -> Path:
    directory = venv / ("Scripts" if os.name == "nt" else "bin")
    return directory / (f"{name}.exe" if os.name == "nt" else name)


def _isolated_environment(root: Path) -> dict[str, str]:
    environment = dict(os.environ)
    for name in ("PYTHONPATH", "VIRTUAL_ENV", "CSBOX_HOME"):
        environment.pop(name, None)
    environment.update(
        {
            "APPDATA": str(root / "appdata"),
            "HOME": str(root / "home"),
            "PYTHONNOUSERSITE": "1",
            "USERPROFILE": str(root / "userprofile"),
            "XDG_CONFIG_HOME": str(root / "xdg-config"),
        }
    )
    return environment


def _install_runtime_dependencies(tmp_path: Path, python: Path) -> None:
    subprocess.run(
        [
            "uv",
            "pip",
            "install",
            "--python",
            str(python),
            "-r",
            str(_runtime_requirements(tmp_path)),
        ],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def _run_installed_probe(python: Path, cwd: Path, *lines: str) -> str:
    environment = _isolated_environment(cwd / ".csbox-smoke")
    result = subprocess.run(
        [str(python), "-c", "\n".join(lines)],
        cwd=cwd,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return result.stdout


def test_built_artifacts_contain_runtime_package_and_resources_without_local_material(
    tmp_path: Path,
) -> None:
    wheel, sdist = _build_distributions(tmp_path)

    for members in (_wheel_members(wheel), _sdist_members(sdist)):
        assert "csbox/__init__.py" in members
        assert "csbox/locales/zh_CN.json" in members
        assert "csbox/tui/themes/csbox.tcss" in members
        assert "LICENSE" in members or any(
            member.endswith(".dist-info/licenses/LICENSE") for member in members
        )
        assert not any(
            member.startswith(marker) for member in members for marker in LOCAL_ONLY_MARKERS
        )
        assert not any(
            member.endswith(
                (
                    ".env",
                    ".env.local",
                    ".png",
                    ".zip",
                    ".whl",
                    ".tar.gz",
                    ".ttf",
                    ".ttc",
                    ".otf",
                    ".woff",
                    ".woff2",
                )
            )
            for member in members
        )
    assert SECRET_SENTINEL not in wheel.read_bytes()
    assert SECRET_SENTINEL not in sdist.read_bytes()


def test_repeated_builds_have_stable_names_bytes_and_members(tmp_path: Path) -> None:
    first_wheel, first_sdist = _build_distributions(tmp_path / "first")
    second_wheel, second_sdist = _build_distributions(tmp_path / "second")

    assert first_wheel.name == second_wheel.name == f"csbox-{__version__}-py3-none-any.whl"
    assert first_sdist.name == second_sdist.name == f"csbox-{__version__}.tar.gz"
    assert first_wheel.read_bytes() == second_wheel.read_bytes()
    assert first_sdist.read_bytes() == second_sdist.read_bytes()
    assert _wheel_members(first_wheel) == _wheel_members(second_wheel)
    assert _sdist_members(first_sdist) == _sdist_members(second_sdist)


def test_wheel_metadata_declares_version_license_and_console_entrypoint(tmp_path: Path) -> None:
    wheel, _ = _build_distributions(tmp_path)

    with zipfile.ZipFile(wheel) as archive:
        metadata_name = next(
            name for name in archive.namelist() if name.endswith(".dist-info/METADATA")
        )
        metadata = email.message_from_bytes(archive.read(metadata_name))
        entry_points_name = next(
            name for name in archive.namelist() if name.endswith(".dist-info/entry_points.txt")
        )
        entry_points = archive.read(entry_points_name).decode("utf-8")

    assert metadata["Name"] == "csbox"
    assert metadata["Version"] == importlib.metadata.version("csbox") == __version__
    assert metadata["Requires-Python"] == ">=3.11"
    assert (
        metadata.get("License-Expression") == "Apache-2.0"
        or metadata.get("License") == "Apache-2.0"
    )
    assert "[console_scripts]" in entry_points
    assert "csbox = csbox.cli:main" in entry_points


@pytest.mark.integration
def test_wheel_install_runs_console_entrypoint_and_installed_resources_outside_repository(
    tmp_path: Path,
) -> None:
    wheel, _ = _build_distributions(tmp_path)
    venv = tmp_path / "venv"
    subprocess.run(
        ["uv", "venv", str(venv)],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    _install_runtime_dependencies(tmp_path, _venv_python(venv))
    subprocess.run(
        [
            "uv",
            "pip",
            "install",
            "--no-deps",
            "--python",
            str(_venv_python(venv)),
            str(wheel),
        ],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    outside = tmp_path / "仓库之外" / "示例项目"
    outside.mkdir(parents=True)
    environment = _isolated_environment(tmp_path / "wheel-smoke")
    command = _venv_executable(venv, "csbox")
    assert command.is_file()

    version_result = subprocess.run(
        [str(command), "--version"],
        cwd=outside,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    installed_version = subprocess.run(
        [
            str(_venv_python(venv)),
            "-c",
            "import importlib.metadata as m; print(m.version('csbox'))",
        ],
        cwd=outside,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout.strip()
    assert version_result.stdout.strip() == installed_version

    for args in (
        ("--help",),
        ("doctor",),
        ("lab", "--help"),
        ("api", "--help"),
        ("check", "--help"),
        ("pack", "--help"),
    ):
        result = subprocess.run(
            [str(command), *args],
            cwd=outside,
            env=environment,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        assert result.stdout

    probe = _run_installed_probe(
        _venv_python(venv),
        outside,
        "import asyncio",
        "from pathlib import Path",
        "from PIL import Image",
        "from csbox.core.events import TerminalEvent, TerminalEventType",
        "from csbox.core.models import EnvironmentSnapshot",
        "from csbox.api.models import ApiEvidence, ApiRequest",
        "from csbox.api.renderer import ApiEvidenceRenderer",
        "from csbox.lab.fake_data import FakeHomeDataSource",
        "from csbox.lab.renderer import TerminalEvidenceRenderer",
        "from csbox.lab.screen import TerminalCell, TerminalCursor, TerminalEmulator",
        "from csbox.locales import load_locale",
        "from csbox.tui.app import CSBoxApp",
        "assert load_locale('zh_CN')('brand.name') == 'CSBox'",
        "assert CSBoxApp.CSS_PATH.is_file()",
        "emulator = TerminalEmulator(columns=8, rows=1)",
        "emulator.apply(TerminalEvent(1, 0.0, 0.0, TerminalEventType.OUTPUT, '中文'.encode()))",
        "png = Path('证据.png')",
        "TerminalEvidenceRenderer().render(emulator.snapshot(), png)",
        "with Image.open(png) as image:",
        "    assert image.format == 'PNG'",
        "    assert image.width > 0 and image.height > 0",
        "api_png = Path('接口证据.png')",
        "api_evidence = ApiEvidence(",
        "    title='中文接口',",
        "    request=ApiRequest(method='GET', url='https://example.test/中文'),",
        ")",
        "ApiEvidenceRenderer().render(api_evidence, api_png)",
        "with Image.open(api_png) as image:",
        "    assert image.format == 'PNG'",
        "    assert image.width > 0 and image.height > 0",
        "sample = Path('临时项目')",
        "sample.mkdir()",
        "(sample / 'README.md').write_text('# 项目\\n', encoding='utf-8')",
        "(sample / '中文文件.txt').write_text('内容\\n', encoding='utf-8')",
        "async def tui_smoke():",
        "    environment = EnvironmentSnapshot(",
        "        os_name='Linux',",
        "        os_version='test',",
        "        python_version='3.12',",
        "        shell='Bash',",
        "        shell_executable='bash',",
        "        powershell_51_available=False,",
        "        powershell_7_available=False,",
        "        is_wsl=False,",
        "        terminal_columns=80,",
        "        terminal_rows=24,",
        "    )",
        "    app = CSBoxApp(",
        "        data_source=FakeHomeDataSource(),",
        "        environment=environment,",
        "        locale=load_locale(),",
        "    )",
        "    async with app.run_test(size=(80, 24)) as pilot:",
        "        await pilot.pause()",
        "        assert app.screen is not None",
        "asyncio.run(tui_smoke())",
        "print(CSBoxApp.CSS_PATH)",
    )
    assert Path(probe.strip()).as_posix().endswith("csbox/tui/themes/csbox.tcss")

    check = subprocess.run(
        [str(command), "check", str(outside / "临时项目"), "--plain"],
        cwd=outside,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert "README" in check.stdout
    pack = subprocess.run(
        [str(command), "pack", str(outside / "临时项目"), "--dry-run", "--plain"],
        cwd=outside,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert "中文文件.txt" in pack.stdout
    assert not any(outside.glob("*.zip"))


@pytest.mark.integration
def test_uv_tool_install_runs_built_wheel_outside_repository(tmp_path: Path) -> None:
    wheel, _ = _build_distributions(tmp_path)
    constraints = _runtime_requirements(tmp_path)
    dependency_venv = tmp_path / "dependency-cache"
    subprocess.run(
        ["uv", "venv", str(dependency_venv)],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    _install_runtime_dependencies(tmp_path, _venv_python(dependency_venv))

    tool_dir = tmp_path / "tool-env"
    tool_bin = tmp_path / "tool-bin"
    install_environment = dict(os.environ)
    install_environment.update({"UV_TOOL_DIR": str(tool_dir), "UV_TOOL_BIN_DIR": str(tool_bin)})
    subprocess.run(
        [
            "uv",
            "tool",
            "install",
            "--force",
            "-c",
            str(constraints),
            str(wheel),
        ],
        cwd=tmp_path,
        env=install_environment,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    command = tool_bin / ("csbox.exe" if os.name == "nt" else "csbox")
    assert command.is_file()
    outside = tmp_path / "工具安装态" / "项目"
    outside.mkdir(parents=True)
    environment = _isolated_environment(tmp_path / "tool-smoke")
    environment.update({"UV_TOOL_DIR": str(tool_dir), "UV_TOOL_BIN_DIR": str(tool_bin)})
    result = subprocess.run(
        [str(command), "--version"],
        cwd=outside,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert result.stdout.strip() == "0.5.0.dev0"
