"""Proxy CLI command surface and daemon file locations."""

from typer.testing import CliRunner

from castrel_proxy.cli.commands import app as root_app
from castrel_proxy.cli.proxy_commands import proxy_app
from castrel_proxy.core.daemon import get_proxy_daemon_manager

runner = CliRunner()


def test_proxy_cli_exposes_lifecycle_commands():
    names = {command.name for command in proxy_app.registered_commands}
    assert {"pair", "start", "status", "stop", "restart"} <= names


def test_proxy_group_help_lists_subcommand_options():
    """The group help must show parameters, not just command names."""
    result = runner.invoke(root_app, ["proxy", "--help"])

    assert result.exit_code == 0, result.output
    assert "Command options" in result.output
    # Flags from several different subcommands, not only the group itself.
    for expected in ("--lines", "--yes", "--keep-binary", "--key-file", "[CODE]"):
        assert expected in result.output, f"{expected} missing from proxy --help"


def test_proxy_group_help_covers_every_command_without_duplicates():
    """Every leaf command with parameters appears once, so a new one cannot be forgotten."""
    from typer.main import get_command

    group = get_command(proxy_app)
    result = runner.invoke(root_app, ["proxy", "--help"])

    assert result.exit_code == 0, result.output
    for name, command in group.commands.items():
        if not command.params:
            continue
        assert name in result.output, f"command {name} missing from help"


def test_proxy_group_help_hides_internal_help_flags():
    result = runner.invoke(root_app, ["proxy", "--help"])

    assert result.exit_code == 0, result.output
    # ``--help`` is already in the Options panel; repeating it per command is noise.
    options_section = result.output.split("Command options", 1)[1]
    assert "--help" not in options_section


def test_proxy_group_help_marks_required_and_optional():
    """Every parameter row carries a requirement label."""
    result = runner.invoke(root_app, ["proxy", "--help"])

    assert result.exit_code == 0, result.output
    options_section = result.output.split("Command options", 1)[1]
    header = next(
        (
            line
            for line in options_section.splitlines()
            if "Parameter" in line and "Command" in line
        ),
        "",
    )
    assert "Required" in header, header
    assert "optional" in options_section


def test_proxy_group_help_requirement_labels_match_the_command_tree():
    """Required and optional come from click, so a label cannot drift from the parser."""
    import click
    from typer.main import get_command

    from castrel_proxy.cli.help import _command_rows

    group = get_command(proxy_app)

    for name, command in group.commands.items():
        if isinstance(command, click.Group):
            continue
        rows = {parameter: requirement for _, parameter, requirement, _ in _command_rows(command)}
        for parameter in command.params:
            if isinstance(parameter, click.Option) and "--help" in parameter.opts:
                continue
            label = _label(parameter)
            assert label in rows, f"{name}: {label} missing from the options table"
            expected = "required" if parameter.required else "optional"
            assert rows[label] == expected, f"{name} {label}: {rows[label]} != {expected}"


def _label(parameter):
    from castrel_proxy.cli.help import _summarize

    return _summarize(parameter)[0]


def test_help_marks_a_required_argument_and_option():
    """The required branch is exercised with a synthetic group, not a live subcommand."""
    import typer

    from castrel_proxy.cli.help import OptionsInHelpGroup, install_options_help

    app = typer.Typer(cls=OptionsInHelpGroup)
    sub = typer.Typer()
    app.add_typer(sub, name="demo")
    install_options_help(sub)

    @sub.command("do")
    def do(
        name: str = typer.Argument(..., help="must supply"),
        opt: str = typer.Option("", "--opt", help="may omit"),
    ) -> None:
        pass

    result = runner.invoke(app, ["demo", "--help"])

    assert result.exit_code == 0, result.output
    table = result.output.split("Command options", 1)[1]
    name_row = next(line for line in table.splitlines() if "NAME" in line)
    assert "required" in name_row
    opt_row = next(line for line in table.splitlines() if "--opt" in line)
    assert "optional" in opt_row


def test_proxy_daemon_uses_separate_files():
    manager = get_proxy_daemon_manager()
    assert manager.pid_file.name == "proxy-node.pid"
    assert manager.log_file.name == "proxy-node.log"


def test_status_renders_received_policy_limits():
    from castrel_proxy.cli.proxy_commands import _policy_status_lines

    lines = _policy_status_lines(
        {
            "policy_version": 5,
            "received_at": 1_700_000_000_000,
            "policy": {
                "max_concurrency": 8,
                "max_timeout_seconds": 600,
                "max_result_bytes": 2_097_152,
                "allowed_connectors": ["*"],
                "exec": {"enabled": True, "mode": "allow_all"},
            },
        }
    )

    assert lines[0].startswith("  Policy: version 5 (received ")
    assert "Max concurrency: 8" in lines[1]
    assert "Max timeout: 600s" in lines[2]
    assert "Max result bytes: 2,097,152" in lines[3]
    assert "mode=allow_all" in lines[4]
    assert "Allowed connectors: *" in lines[5]


def test_status_reports_a_missing_policy_snapshot():
    from castrel_proxy.cli.proxy_commands import _policy_status_lines

    assert _policy_status_lines(None) == ["  Policy: not received yet"]
