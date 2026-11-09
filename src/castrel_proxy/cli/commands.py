"""
CLI Commands Module

Defines all command-line interface commands for Castrel Bridge Proxy
"""

import asyncio
import base64
import json
import logging
import shutil
import sys
from pathlib import Path
from typing import Any, Dict

import typer

from ..core.client_id import get_client_id
from ..core.config import ConfigError, get_config
from ..core.daemon import get_daemon_manager
from ..mcp.manager import get_mcp_manager
from ..network.api_client import APIError, NetworkError, PairingError, get_api_client
from ..network.websocket_client import WebSocketClient
from .proxy_commands import proxy_app
from .skill_commands import skill_app

# from ..security.whitelist import init_whitelist_file  # DISABLED: Whitelist mechanism disabled

# Configure logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")

app = typer.Typer(add_completion=False, pretty_exceptions_show_locals=False)
app.add_typer(skill_app, name="skill")
app.add_typer(proxy_app, name="proxy")


def decode_verification_code(verification_code: str) -> Dict[str, Any]:
    """
    Decode verification code, extracting timestamp, workspace_id, and random code

    Args:
        verification_code: Encoded verification code

    Returns:
        Dict[str, Any]: Dictionary containing ts (timestamp), wid (workspace_id), rand (random_code)

    Raises:
        ValueError: Invalid verification code format
    """
    try:
        # Add possibly missing padding characters
        padding = 4 - (len(verification_code) % 4)
        if padding != 4:
            verification_code += "=" * padding

        # Base64 decode
        decoded_bytes = base64.urlsafe_b64decode(verification_code)
        json_str = decoded_bytes.decode("utf-8")

        # Parse JSON
        code_data = json.loads(json_str)

        # Validate required fields
        if not all(key in code_data for key in ["ts", "wid", "rand"]):
            raise ValueError("Missing required fields in verification code")

        return code_data

    except Exception as e:
        raise ValueError(f"Invalid verification code format: {str(e)}")


@app.command()
def pair(
    code: str = typer.Argument(..., help="Verification code provided by server"),
    server_url: str = typer.Argument(..., help="Server URL address"),
):
    """
    Pair with server

    Pair local bridge with server using verification code. The code contains workspace ID and other
    information without needing manual input.

    Usage:
      castrel-proxy pair <verification_code> <server_url>

    Example:
      castrel-proxy pair eyJ0cyI6MTczNTA4ODQwMCwid2lkIjoiZGVmYXVsdCIsInJhbmQiOiIxMjM0NTYifQ https://server.example.com
    """
    config = get_config()
    api_client = get_api_client()

    try:
        # Decode verification code to get workspace_id
        typer.echo("Parsing verification code...")
        try:
            code_info = decode_verification_code(code)
            workspace_id = code_info["wid"]

            typer.secho("✓ Verification code parsed successfully", fg=typer.colors.GREEN)
            typer.echo(f"  Workspace ID: {workspace_id}")
        except ValueError as e:
            typer.secho(f"✗ Invalid verification code format: {e}", fg=typer.colors.RED, err=True)
            typer.echo("Hint: Please ensure you use the complete verification code from the server", err=True)
            raise typer.Exit(1)

        # Generate client ID
        typer.echo("\nGenerating client identifier...")
        client_id = get_client_id()
        typer.echo(f"Client ID: {client_id}")

        # Connect to server and verify
        typer.echo(f"\nConnecting to server: {server_url}")
        typer.echo(f"Using verification code: {code}")
        typer.echo(f"Workspace ID: {workspace_id}")

        # Call server verification endpoint
        api_client.verify_pairing(server_url, code, client_id, workspace_id)

        # Verification successful, save configuration
        config.save(server_url, code, client_id, workspace_id)

        # Initialize whitelist configuration file - DISABLED
        # whitelist_path = init_whitelist_file()
        # typer.echo(f"Whitelist configuration initialized: {whitelist_path}")

        typer.secho("✓ Pairing successful!", fg=typer.colors.GREEN)
        typer.echo(f"Configuration saved to: {config.config_file}")

        # Try to load and send MCP tools + skills information
        typer.echo("\nLoading MCP services and skills...")
        try:
            mcp_manager = get_mcp_manager()
            from ..skills.manager import get_skills_manager
            skills_manager = get_skills_manager()

            async def sync_client_capabilities():
                skills = skills_manager.scan_skills()
                if skills:
                    typer.echo(f"Discovered {len(skills)} skill(s)")
                else:
                    typer.echo("No skills found")

                raw_configs = mcp_manager.get_raw_configs()
                if not raw_configs:
                    typer.echo("No MCP services configured")
                    await api_client._send_client_info(
                        server_url, client_id, code, workspace_id, {}, skills=skills or None,
                    )
                    typer.secho("✓ Client capabilities synchronized", fg=typer.colors.GREEN)
                    return

                typer.echo(f"Discovered {len(raw_configs)} MCP server(s)")

                typer.echo("\nConnecting to MCP servers to fetch tool schemas...")
                try:
                    mcp_tools = await mcp_manager.get_tools_schema_resilient(raw_configs, timeout=15)
                    tool_count = sum(len(v) for v in mcp_tools.values())
                    synced_server_count = len(mcp_tools)
                    failed_server_count = len(raw_configs) - synced_server_count

                    if synced_server_count > 0:
                        typer.secho(
                            f"✓ Retrieved {tool_count} tool(s) from {synced_server_count} server(s)",
                            fg=typer.colors.GREEN,
                        )
                    if failed_server_count > 0:
                        typer.secho(
                            f"⚠ Skipped {failed_server_count} MCP server(s) due to timeout or fetch failure",
                            fg=typer.colors.YELLOW,
                        )
                    if synced_server_count == 0:
                        mcp_tools = {name: [] for name in raw_configs.keys()}
                        typer.secho("⚠ No MCP tool schemas retrieved, sending empty tool lists", fg=typer.colors.YELLOW)
                except SystemExit:
                    raise
                except Exception as e:
                    typer.secho(f"⚠ Failed to fetch tool schemas: {e}", fg=typer.colors.YELLOW)
                    typer.echo("Falling back to empty tool lists")
                    mcp_tools = {name: [] for name in raw_configs.keys()}

                typer.echo("Sending client capabilities to server...")
                await api_client._send_client_info(
                    server_url, client_id, code, workspace_id, mcp_tools, skills=skills or None,
                )
                typer.secho("✓ Client capabilities synchronized", fg=typer.colors.GREEN)

            asyncio.run(sync_client_capabilities())

        except Exception as e:
            typer.secho(f"⚠ Capabilities synchronization failed: {e}", fg=typer.colors.YELLOW)
            typer.echo("Hint: You can manually synchronize later with 'castrel-proxy mcp-sync' or 'castrel-proxy skills-sync'")

        typer.echo("\nHint: Use 'castrel-proxy start' to start bridge service")

    except PairingError as e:
        typer.secho(f"✗ Pairing failed: {e}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1)
    except NetworkError as e:
        typer.secho(f"✗ Network error: {e}", fg=typer.colors.RED, err=True)
        typer.echo("Please check if server address is correct and network connection is normal", err=True)
        raise typer.Exit(1)
    except ConfigError as e:
        typer.secho(f"✗ Configuration error: {e}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1)
    except APIError as e:
        typer.secho(f"✗ API error: {e}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1)
    except Exception as e:
        typer.secho(f"✗ Unknown error: {e}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1)


@app.command()
def start(
    daemon: bool = typer.Option(
        False, "--daemon/--foreground", "-d/-f", help="Run in background or foreground (default)"
    ),
):
    """
    Start bridge service

    Start bridge and connect to paired server.

    Run in foreground (default):
      castrel-proxy start
      castrel-proxy start --foreground
      castrel-proxy start -f

    Run in background:
      castrel-proxy start --daemon
      castrel-proxy start -d
    """
    config = get_config()

    try:
        # Load configuration
        config_data = config.load()
        server_url = config_data["server_url"]
        client_id = config_data["client_id"]
        verification_code = config_data["verification_code"]
        workspace_id = config_data["workspace_id"]

        typer.secho("=== Starting Bridge Service ===", bold=True)
        typer.echo(f"Server: {server_url}")
        typer.echo(f"Client ID: {client_id}")
        typer.echo(f"Workspace ID: {workspace_id}")

        if daemon:
            # Check if Windows
            if sys.platform == "win32":
                typer.secho("✗ Background mode is not supported on Windows", fg=typer.colors.YELLOW)
                typer.echo("Hint: Use '--foreground' or '-f' flag to run in foreground mode")
                raise typer.Exit(1)

            # Get daemon manager
            daemon_mgr = get_daemon_manager()

            # Check if already running
            if daemon_mgr.is_running():
                pid = daemon_mgr.get_pid()
                typer.secho(f"✗ Bridge is already running with PID {pid}", fg=typer.colors.YELLOW)
                typer.echo("Hint: Use 'castrel-proxy stop' to stop it first")
                raise typer.Exit(1)

            typer.echo("\nStarting bridge in background...")
            typer.echo(f"PID file: {daemon_mgr.pid_file}")
            typer.echo(f"Log file: {daemon_mgr.log_file}")

            # Daemonize the process
            try:
                daemon_mgr.daemonize()

                # Configure logging to file
                logging.basicConfig(
                    level=logging.INFO,
                    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
                    handlers=[logging.FileHandler(daemon_mgr.log_file), logging.StreamHandler()],
                )

                logger = logging.getLogger(__name__)
                logger.info("=== Bridge Service Started in Background ===")
                logger.info(f"Server: {server_url}")
                logger.info(f"Client ID: {client_id}")
                logger.info(f"Workspace ID: {workspace_id}")

                # Create and run WebSocket client
                ws_client = WebSocketClient(
                    server_url=server_url,
                    client_id=client_id,
                    verification_code=verification_code,
                    workspace_id=workspace_id,
                )

                asyncio.run(ws_client.run())

            except RuntimeError as e:
                # Already running error
                typer.secho(f"✗ {e}", fg=typer.colors.RED, err=True)
                raise typer.Exit(1)
            except Exception as e:
                if daemon_mgr.get_pid() is not None:
                    # Log error in daemon mode
                    logging.error(f"Runtime error: {e}", exc_info=True)
                raise typer.Exit(1)

            # This line is only reached by parent process before exit
            typer.secho("✓ Bridge started in background", fg=typer.colors.GREEN)
            typer.echo(f"PID: {daemon_mgr.get_pid()}")
            typer.echo("Hint: Use 'castrel-proxy logs -f' to follow logs")
            typer.echo("Hint: Use 'castrel-proxy stop' to stop service")

        else:
            typer.echo("\nRunning in foreground mode...")
            typer.echo("Connecting to server...")
            typer.echo("Hint: Press Ctrl+C to stop service\n")

            # Create WebSocket client
            ws_client = WebSocketClient(
                server_url=server_url,
                client_id=client_id,
                verification_code=verification_code,
                workspace_id=workspace_id,
            )

            # Run client
            try:
                asyncio.run(ws_client.run())
            except KeyboardInterrupt:
                typer.echo("\nStopping bridge...")
                typer.secho("✓ Bridge stopped", fg=typer.colors.GREEN)
            except Exception as e:
                typer.secho(f"\n✗ Runtime error: {e}", fg=typer.colors.RED, err=True)
                raise typer.Exit(1)

    except ConfigError as e:
        typer.secho(f"✗ {e}", fg=typer.colors.RED, err=True)
        typer.echo("Hint: Please pair first using 'castrel-proxy pair' command", err=True)
        raise typer.Exit(1)


@app.command()
def config():
    """
    View configuration information
    """
    config_obj = get_config()

    try:
        # Load configuration
        config_data = config_obj.load()

        typer.secho("=== Configuration Information ===", bold=True)
        typer.echo(f"Config file: {config_obj.config_file}")
        typer.echo(f"Server URL: {config_data['server_url']}")
        typer.echo(f"Verification code: {config_data['verification_code']}")
        typer.echo(f"Client ID: {config_data['client_id']}")
        typer.echo(f"Workspace ID: {config_data['workspace_id']}")

        if "paired_at" in config_data:
            typer.echo(f"Paired at: {config_data['paired_at']}")

    except ConfigError as e:
        typer.secho(f"✗ {e}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1)


@app.command()
def status():
    """
    View bridge running status
    """
    config = get_config()

    try:
        # Load configuration
        config_data = config.load()

        typer.secho("=== Bridge Status ===", bold=True)
        typer.echo("Pairing status: ", nl=False)
        typer.secho("Paired", fg=typer.colors.GREEN)
        typer.echo(f"Server: {config_data['server_url']}")
        typer.echo(f"Client ID: {config_data['client_id']}")
        typer.echo(f"Workspace ID: {config_data['workspace_id']}")

        if "paired_at" in config_data:
            typer.echo(f"Paired at: {config_data['paired_at']}")

        # Check running status
        daemon_mgr = get_daemon_manager()
        typer.echo("Running status: ", nl=False)

        if daemon_mgr.is_running():
            pid = daemon_mgr.get_pid()
            typer.secho(f"Running (PID: {pid})", fg=typer.colors.GREEN)
            typer.echo(f"PID file: {daemon_mgr.pid_file}")
            typer.echo(f"Log file: {daemon_mgr.log_file}")
            typer.echo("Hint: Use 'castrel-proxy logs -f' to follow logs")
        else:
            typer.secho("Not running", fg=typer.colors.YELLOW)
            typer.echo("Hint: Use 'castrel-proxy start' to start service")

    except ConfigError:
        typer.secho("=== Bridge Status ===", bold=True)
        typer.echo("Pairing status: ", nl=False)
        typer.secho("Not paired", fg=typer.colors.YELLOW)
        typer.echo("Hint: Use 'castrel-proxy pair' command to pair")


@app.command()
def stop():
    """
    Stop bridge service

    Stop the background daemon process if running.
    """
    daemon_mgr = get_daemon_manager()

    # Check if running
    if not daemon_mgr.is_running():
        typer.secho("✗ Bridge is not running", fg=typer.colors.YELLOW)
        # Clean up stale PID file if exists
        if daemon_mgr.pid_file.exists():
            daemon_mgr.pid_file.unlink()
            typer.echo("Cleaned up stale PID file")
        raise typer.Exit(0)

    pid = daemon_mgr.get_pid()
    typer.echo(f"Stopping bridge (PID: {pid})...")

    # Stop the daemon
    if daemon_mgr.stop():
        typer.secho("✓ Bridge stopped", fg=typer.colors.GREEN)
    else:
        typer.secho("✗ Failed to stop bridge", fg=typer.colors.RED, err=True)
        typer.echo(f"Hint: Try manually killing process {pid}")
        raise typer.Exit(1)


@app.command()
def unpair():
    """
    Unpair from server
    """
    config = get_config()

    try:
        # Check if configuration exists
        if not config.exists():
            typer.secho("✗ Pairing configuration not found", fg=typer.colors.YELLOW)
            raise typer.Exit(0)

        # Display current configuration information
        config_data = config.load()
        typer.echo(f"Currently paired server: {config_data['server_url']}")
        typer.echo(f"Client ID: {config_data['client_id']}")
        typer.echo(f"Workspace ID: {config_data['workspace_id']}")

        # Confirm deletion
        confirm = typer.confirm("Are you sure you want to unpair?")
        if confirm:
            # Stop the running daemon first so the server receives the WebSocket disconnect
            # and updates the node status to disconnected (fixes CAST-669)
            daemon_mgr = get_daemon_manager()
            if daemon_mgr.is_running():
                pid = daemon_mgr.get_pid()
                typer.echo(f"Stopping bridge service (PID: {pid})...")
                if daemon_mgr.stop():
                    typer.secho("✓ Bridge stopped", fg=typer.colors.GREEN)
                else:
                    typer.secho("⚠ Failed to stop bridge, proceeding with unpair anyway", fg=typer.colors.YELLOW)

            typer.echo("Unpairing...")
            config.delete()
            typer.secho("✓ Unpaired", fg=typer.colors.GREEN)
        else:
            typer.echo("Cancelled")

    except ConfigError as e:
        typer.secho(f"✗ {e}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1)


@app.command()
def logs(
    lines: int = typer.Option(50, "--lines", "-n", help="Display last N lines of logs"),
    follow: bool = typer.Option(False, "--follow", "-f", help="Follow logs in real-time"),
):
    """
    View bridge logs

    Display logs from the background daemon process.
    """
    daemon_mgr = get_daemon_manager()

    # Check if log file exists
    if not daemon_mgr.log_file.exists():
        typer.secho("✗ Log file not found", fg=typer.colors.YELLOW)
        typer.echo(f"Expected location: {daemon_mgr.log_file}")
        typer.echo("Hint: Start the bridge first using 'castrel-proxy start'")
        raise typer.Exit(1)

    if follow:
        # Follow logs in real-time
        typer.echo(f"Following logs from {daemon_mgr.log_file}... (Ctrl+C to exit)\n")
        try:
            import subprocess

            # Use tail -f to follow logs
            subprocess.run(["tail", "-f", str(daemon_mgr.log_file)])
        except KeyboardInterrupt:
            typer.echo("\nStopped following logs")
        except FileNotFoundError:
            # tail command not available, fallback to Python implementation
            typer.secho("✗ 'tail' command not found", fg=typer.colors.YELLOW)
            typer.echo("Hint: Install coreutils or use '--lines' without '--follow'")
            raise typer.Exit(1)
    else:
        # Display last N lines
        typer.echo(f"Last {lines} lines from {daemon_mgr.log_file}:\n")
        try:
            with open(daemon_mgr.log_file, "r") as f:
                all_lines = f.readlines()
                last_lines = all_lines[-lines:] if len(all_lines) > lines else all_lines
                for line in last_lines:
                    typer.echo(line.rstrip())
        except Exception as e:
            typer.secho(f"✗ Failed to read log file: {e}", fg=typer.colors.RED, err=True)
            raise typer.Exit(1)


@app.command()
def mcp_list():
    """
    List configured MCP services
    """
    mcp_manager = get_mcp_manager()
    servers = mcp_manager.get_server_list()

    if not servers:
        typer.echo("No MCP services configured")
        typer.echo(f"Config file: {mcp_manager.config_file}")
        typer.echo("Hint: Refer to mcp.json.example to create configuration file")
        return

    typer.secho("=== MCP Service List ===", bold=True)
    typer.echo(f"Config file: {mcp_manager.config_file}")
    typer.echo(f"Total {len(servers)} service(s):\n")

    for server in servers:
        name = server["name"]
        transport = server["transport"]

        typer.echo(f"📦 {name}")
        typer.echo(f"   Transport: {transport}")

        if transport == "stdio":
            command = server["command"]
            args = server["args"]
            typer.echo(f"   Command: {command} {' '.join(args)}")
            if server["env"]:
                typer.echo(f"   Environment variables: {len(server['env'])} var(s)")
        elif transport == "http":
            typer.echo(f"   URL: {server['url']}")

        typer.echo()


@app.command()
def mcp_sync():
    """
    Synchronize MCP tools information to server
    """
    config = get_config()

    try:
        # Load configuration
        config_data = config.load()
        server_url = config_data["server_url"]
        client_id = config_data["client_id"]
        verification_code = config_data["verification_code"]
        workspace_id = config_data["workspace_id"]

        typer.secho("=== Synchronizing MCP Tools ===", bold=True)
        typer.echo(f"Server: {server_url}")
        typer.echo(f"Client ID: {client_id}")
        typer.echo(f"Workspace ID: {workspace_id}\n")

        mcp_manager = get_mcp_manager()
        api_client = get_api_client()

        async def sync_mcp_tools():
            typer.echo("Reading MCP configurations...")
            raw_configs = mcp_manager.get_raw_configs()

            if not raw_configs:
                typer.secho("⚠ No MCP services configured, sending empty tool list", fg=typer.colors.YELLOW)
                typer.echo(f"Config file: {mcp_manager.config_file}")
                await api_client._send_client_info(server_url, client_id, verification_code, workspace_id, {})
                typer.secho("✓ MCP tool schemas synchronized (empty)", fg=typer.colors.GREEN)
                return

            typer.secho(f"✓ Discovered {len(raw_configs)} MCP server(s)", fg=typer.colors.GREEN)

            typer.echo("\nMCP servers overview:")
            for name, cfg in raw_configs.items():
                cmd = cfg.get("command", cfg.get("url", ""))
                typer.echo(f"  - {name}: {cmd}")

            typer.echo("\nConnecting to MCP servers to fetch tool schemas...")
            try:
                mcp_tools = await mcp_manager.get_tools_schema_resilient(raw_configs, timeout=15)
                tool_count = sum(len(v) for v in mcp_tools.values())
                synced_server_count = len(mcp_tools)
                failed_server_count = len(raw_configs) - synced_server_count

                if synced_server_count > 0:
                    typer.secho(
                        f"✓ Retrieved {tool_count} tool(s) from {synced_server_count} server(s)",
                        fg=typer.colors.GREEN,
                    )
                if failed_server_count > 0:
                    typer.secho(
                        f"⚠ Skipped {failed_server_count} MCP server(s) due to timeout or fetch failure",
                        fg=typer.colors.YELLOW,
                    )
                if synced_server_count == 0:
                    mcp_tools = {name: [] for name in raw_configs.keys()}
                    typer.secho("⚠ No MCP tool schemas retrieved, sending empty tool lists", fg=typer.colors.YELLOW)
            except SystemExit:
                raise
            except Exception as e:
                typer.secho(f"⚠ Failed to fetch tool schemas: {e}", fg=typer.colors.YELLOW)
                typer.echo("Falling back to empty tool lists")
                mcp_tools = {name: [] for name in raw_configs.keys()}

            typer.echo("\nSending to server...")
            await api_client._send_client_info(server_url, client_id, verification_code, workspace_id, mcp_tools)
            typer.secho("✓ MCP tool schemas synchronized", fg=typer.colors.GREEN)

        asyncio.run(sync_mcp_tools())

    except ConfigError as e:
        typer.secho(f"✗ {e}", fg=typer.colors.RED, err=True)
        typer.echo("Hint: Please pair first using 'castrel-proxy pair' command", err=True)
        raise typer.Exit(1)
    except (NetworkError, APIError) as e:
        typer.secho(f"✗ Synchronization failed: {e}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1)
    except Exception as e:
        typer.secho(f"✗ Unknown error: {e}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1)


@app.command()
def skills_sync():
    """
    Synchronize skills information to server
    """
    config = get_config()

    try:
        config_data = config.load()
        server_url = config_data["server_url"]
        client_id = config_data["client_id"]
        verification_code = config_data["verification_code"]
        workspace_id = config_data["workspace_id"]

        typer.secho("=== Synchronizing Skills ===", bold=True)
        typer.echo(f"Server: {server_url}")
        typer.echo(f"Client ID: {client_id}")
        typer.echo(f"Workspace ID: {workspace_id}\n")

        from ..skills.manager import get_skills_manager
        skills_manager = get_skills_manager()
        api_client = get_api_client()

        skills = skills_manager.scan_skills()

        if not skills:
            typer.secho("⚠ No skills found", fg=typer.colors.YELLOW)
            typer.echo(f"Skills directory: {skills_manager.skills_dir}")
            typer.echo("Hint: Place skill folders with SKILL.md in ~/.castrel/skills/")
            return

        typer.secho(f"✓ Discovered {len(skills)} skill(s)", fg=typer.colors.GREEN)
        typer.echo("\nSkills overview:")
        for name, info in skills.items():
            desc = info.get("description", "")
            if len(desc) > 60:
                desc = desc[:57] + "..."
            typer.echo(f"  - {name}: {desc}")

        typer.echo("\nSending to server...")

        async def do_sync():
            await api_client._send_client_info(
                server_url, client_id, verification_code, workspace_id, {}, skills=skills
            )

        asyncio.run(do_sync())
        typer.secho("✓ Skills information synchronized", fg=typer.colors.GREEN)

    except ConfigError as e:
        typer.secho(f"✗ {e}", fg=typer.colors.RED, err=True)
        typer.echo("Hint: Please pair first using 'castrel-proxy pair' command", err=True)
        raise typer.Exit(1)
    except (NetworkError, APIError) as e:
        typer.secho(f"✗ Synchronization failed: {e}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1)
    except Exception as e:
        typer.secho(f"✗ Unknown error: {e}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1)


@app.command()
def version():
    """
    Show version information
    """
    from .. import __author__, __version__
    typer.secho("castrel-proxy", bold=True, nl=False)
    typer.echo(f" v{__version__}")
    typer.echo(f"Author: {__author__}")


@app.command()
def uninstall(
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompt"),
    keep_logs: bool = typer.Option(False, "--keep-logs", help="Keep log files"),
):
    """
    Uninstall proxy - remove all local data, configuration, and the binary itself

    Stops the running service (if any), removes pairing configuration,
    PID file, log files from ~/.castrel/, and the castrel-proxy binary.
    """
    config_obj = get_config()
    daemon_mgr = get_daemon_manager()

    # Resolve binary path (works for both script-installed binary and pip-installed entry point)
    binary_path: Path | None = None
    resolved = shutil.which("castrel-proxy")
    if resolved:
        binary_path = Path(resolved).resolve()

    typer.secho("=== Uninstall Castrel Proxy ===", bold=True)
    typer.echo("\nThe following local data will be removed:")
    typer.echo(f"  Config file : {config_obj.config_file}")
    typer.echo(f"  PID file    : {daemon_mgr.pid_file}")
    if not keep_logs:
        typer.echo(f"  Log file    : {daemon_mgr.log_file}")
    else:
        typer.echo(f"  Log file    : {daemon_mgr.log_file} (kept)")
    if binary_path and binary_path.exists():
        typer.echo(f"  Binary      : {binary_path}")

    if not yes:
        confirm = typer.confirm("\nProceed with uninstall?")
        if not confirm:
            typer.echo("Cancelled")
            raise typer.Exit(0)

    # Stop daemon if running
    if daemon_mgr.is_running():
        pid = daemon_mgr.get_pid()
        typer.echo(f"\nStopping bridge service (PID: {pid})...")
        if daemon_mgr.stop():
            typer.secho("✓ Bridge stopped", fg=typer.colors.GREEN)
        else:
            typer.secho("⚠ Failed to stop bridge, proceeding anyway", fg=typer.colors.YELLOW)

    typer.echo("")

    # Remove config file
    if config_obj.exists():
        try:
            config_obj.delete()
            typer.secho("✓ Removed config file", fg=typer.colors.GREEN)
        except ConfigError as e:
            typer.secho(f"⚠ {e}", fg=typer.colors.YELLOW)
    else:
        typer.echo("  Config file not found, skipping")

    # Remove PID file
    if daemon_mgr.pid_file.exists():
        try:
            daemon_mgr.pid_file.unlink()
            typer.secho("✓ Removed PID file", fg=typer.colors.GREEN)
        except Exception as e:
            typer.secho(f"⚠ Failed to remove PID file: {e}", fg=typer.colors.YELLOW)

    # Remove log file
    if not keep_logs:
        if daemon_mgr.log_file.exists():
            try:
                daemon_mgr.log_file.unlink()
                typer.secho("✓ Removed log file", fg=typer.colors.GREEN)
            except Exception as e:
                typer.secho(f"⚠ Failed to remove log file: {e}", fg=typer.colors.YELLOW)

    # Remove binary (installed by install.sh)
    if binary_path and binary_path.exists():
        try:
            binary_path.unlink()
            typer.secho(f"✓ Removed binary: {binary_path}", fg=typer.colors.GREEN)
        except PermissionError:
            typer.secho(f"⚠ No permission to remove binary: {binary_path}", fg=typer.colors.YELLOW)
            typer.echo(f"  Run manually: rm {binary_path}")
        except Exception as e:
            typer.secho(f"⚠ Failed to remove binary: {e}", fg=typer.colors.YELLOW)

    typer.secho("\n✓ Castrel Proxy uninstalled successfully", fg=typer.colors.GREEN)


def run():
    """Entry point for the CLI application"""
    # If no arguments were given (only the program name), add --help
    if len(sys.argv) == 1:
        sys.argv.append("--help")
    app()


if __name__ == "__main__":
    run()
