from __future__ import annotations

import typer
from rich.console import Console

from csbox.core.environment import detect_environment
from csbox.locales import Translator, load_locale

_locale = load_locale()
app = typer.Typer(
    add_completion=False,
    help=_locale("cli.help"),
    invoke_without_command=True,
    name="csbox",
    no_args_is_help=False,
)


def _yes_no(translator: Translator, value: bool) -> str:
    return translator("doctor.yes" if value else "doctor.no")


@app.callback()
def _run_default(ctx: typer.Context) -> None:
    if ctx.invoked_subcommand is not None:
        return
    from csbox.lab.fake_data import HOME_DATA_SOURCES
    from csbox.tui.app import CSBoxApp

    environment = detect_environment()
    data_source = HOME_DATA_SOURCES.get("fake")
    CSBoxApp(data_source=data_source, environment=environment, locale=_locale).run()


@app.command(help=_locale("cli.doctor.help"))
def doctor() -> None:
    translator = _locale
    environment = detect_environment()
    console = Console(markup=False)
    console.print(translator("doctor.title"))
    console.print(f"{translator('doctor.os')}: {environment.os_name} {environment.os_version}")
    console.print(f"{translator('doctor.python')}: {environment.python_version}")
    shell_name = environment.shell or translator("doctor.unknown")
    console.print(f"{translator('doctor.shell')}: {shell_name}")
    console.print(
        f"{translator('doctor.powershell51')}: "
        f"{_yes_no(translator, environment.powershell_51_available)}"
    )
    console.print(
        f"{translator('doctor.powershell7')}: "
        f"{_yes_no(translator, environment.powershell_7_available)}"
    )
    console.print(f"{translator('doctor.wsl')}: {_yes_no(translator, environment.is_wsl)}")
    console.print(
        f"{translator('doctor.terminal')}: "
        f"{environment.terminal_columns}×{environment.terminal_rows}"
    )


def main() -> None:
    app()
