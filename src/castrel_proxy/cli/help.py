"""Group help that lists the parameters of every direct subcommand.

Typer's group help only prints the command names, so ``castrel-proxy proxy --help`` never
shows ``--lines``, ``--yes`` or ``--key-file``. The tables below are derived from the click
command tree rather than written by hand, so a new option or command appears automatically.
"""

from __future__ import annotations

from typing import Any

import click
import typer

try:  # Rich is a Typer dependency; keep the module importable without it.
    from rich.console import Console
    from rich.table import Table

    HAS_RICH = True
except ImportError:  # pragma: no cover - Typer installs rich unconditionally
    HAS_RICH = False

# Longest option column rendered before the description; keeps one wide row from pushing the
# table past a normal terminal width.
MAX_OPTION_WIDTH = 34


def _option_label(option: click.Option) -> str:
    """Render ``--foreground, -f`` with a metavar only when the option takes a value."""
    names = ", ".join(option.opts + option.secondary_opts)
    if option.is_flag:
        return names
    # Click reports ranged integers as "integer range"; the range is in the type, and the
    # description already says what the number means.
    metavar = (option.metavar or option.type.name).upper()
    metavar = metavar.removesuffix(" RANGE")
    return f"{names} {metavar}"


def _argument_label(argument: click.Argument) -> str:
    label = (argument.name or "").upper()
    return label if argument.required else f"[{label}]"


def _summarize(parameter: click.Parameter) -> tuple[str, str, str]:
    # ``help`` lives on the concrete subclasses; click.Parameter only declares the common base.
    help_text = getattr(parameter, "help", "") or ""
    if isinstance(parameter, click.Option):
        return _option_label(parameter), help_text, _requirement(parameter)
    return _argument_label(parameter), help_text, _requirement(parameter)  # type: ignore[arg-type]


def _requirement(parameter: click.Parameter) -> str:
    """``required`` or ``optional`` for one parameter."""
    return "required" if parameter.required else "optional"


def _command_rows(command: click.Command) -> list[tuple[str, str, str, str]]:
    """Rows of ``(command, parameter, requirement, help)``, arguments before options."""
    rows: list[tuple[str, str, str, str]] = []
    for parameter in command.params:
        if isinstance(parameter, click.Option) and "--help" in parameter.opts:
            continue
        label, help_text, requirement = _summarize(parameter)
        if not label:
            continue
        rows.append((command.name or "", label, requirement, help_text))
    return rows


def _shorten(text: str, limit: int = MAX_OPTION_WIDTH) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def print_command_options(group: click.Group) -> None:
    """Print one row per parameter of every leaf subcommand of ``group``."""
    if not HAS_RICH:  # pragma: no cover
        return
    rows: list[tuple[str, str, str, str]] = []
    groups: list[str] = []
    for name, command in group.commands.items():
        if isinstance(command, click.Group):
            groups.append(name)
            continue
        rows.extend(_command_rows(command))
    if not rows:
        return

    table = Table(
        title="Command options",
        title_justify="left",
        title_style="bold",
        show_header=True,
        header_style="bold",
        box=None,
        pad_edge=False,
        show_edge=False,
    )
    table.add_column("Command", style="cyan", no_wrap=True)
    table.add_column("Parameter", style="green", no_wrap=True)
    table.add_column("Required", no_wrap=True)
    table.add_column("Description", overflow="fold")
    previous = ""
    for command_name, parameter, requirement, help_text in rows:
        # Repeat the command only on its first row so the table stays readable at 80 columns.
        required = "[bold red]required[/]" if requirement == "required" else "[dim]optional[/]"
        table.add_row(
            command_name if command_name != previous else "",
            _shorten(parameter),
            required,
            help_text,
        )
        previous = command_name

    console = Console()
    console.print()
    console.print(table)
    if groups:
        console.print(f"  Nested commands: {', '.join(groups)}", soft_wrap=True)


class OptionsInHelpGroup(typer.core.TyperGroup):
    """Typer group whose ``--help`` also lists each subcommand's parameters."""

    def format_help(self, ctx: click.Context, formatter: click.HelpFormatter) -> None:
        super().format_help(ctx, formatter)
        print_command_options(self)


def install_options_help(app: Any) -> None:
    """Switch an existing ``typer.Typer`` to :class:`OptionsInHelpGroup`."""
    app.info.cls = OptionsInHelpGroup


__all__ = ["OptionsInHelpGroup", "install_options_help", "print_command_options"]
