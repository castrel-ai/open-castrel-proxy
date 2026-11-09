"""CLI for the private-network proxy node: pair, run, inspect and restart."""

from __future__ import annotations

import asyncio
import platform
import socket
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
import typer

from ..core.daemon import DaemonManager, get_proxy_daemon_manager
from ..core.policy_state import DEFAULT_POLICY_STATE_FILE, load_policy_state
from ..pairing.keys import NodeKeys, load_keys, new_private_keys
from ..security.policy import parse_policy
from ..transport.spool import Outbox
from ..transport.ws import ProxyNodeRuntime

DEFAULT_KEY_FILE = Path.home() / ".castrel" / "node" / "keys.json"

proxy_app = typer.Typer(add_completion=False, help="Private-network proxy node commands")


def _platform() -> str:
    system = platform.system().lower()
    machine = platform.machine().lower()
    if system == "darwin":
        return "macos-arm64" if machine in ("arm64", "aarch64") else "macos-x86_64"
    return "linux-arm64" if machine in ("aarch64", "arm64") else "linux-x86_64"


def _limit_text(value: float | int | None, *, suffix: str = "", group: bool = False) -> str:
    if value is None:
        return "default"
    if group:
        return f"{int(value):,}{suffix}"
    return f"{float(value):g}{suffix}"


def _policy_status_lines(state: dict[str, Any] | None) -> list[str]:
    """Render the policy snapshot the node received from the platform."""
    if state is None:
        return ["  Policy: not received yet"]
    policy = parse_policy(state.get("policy"))
    received_at = int(state.get("received_at") or 0)
    received = (
        datetime.fromtimestamp(received_at / 1000).strftime("%Y-%m-%d %H:%M:%S")
        if received_at
        else "unknown"
    )
    lines = [
        f"  Policy: version {int(state.get('policy_version') or 0)} (received {received})",
        f"    Max concurrency: {_limit_text(policy.max_concurrency)}",
        f"    Max timeout: {_limit_text(policy.max_timeout_seconds, suffix='s')}",
        f"    Max result bytes: {_limit_text(policy.max_result_bytes, group=True)}",
    ]
    exec_state = "enabled" if policy.exec.enabled else "disabled"
    lines.append(
        f"    Exec: {exec_state} (mode={policy.exec.mode}, "
        f"max timeout {_limit_text(policy.exec.max_timeout_seconds, suffix='s')}, "
        f"max output {_limit_text(policy.exec.max_output_bytes, group=True)})"
    )
    lines.append(f"    Allowed connectors: {', '.join(policy.allowed_connectors) or '(none)'}")
    return lines


def _build_runtime(*, keys: NodeKeys, base_url: str, key_file: Path) -> ProxyNodeRuntime:
    return ProxyNodeRuntime(
        server_url=base_url,
        keys=keys,
        keys_path=key_file,
        policy_state_path=DEFAULT_POLICY_STATE_FILE,
        outbox=Outbox(Path.home() / ".castrel" / "node" / "outbox.jsonl"),
    )


def _run_foreground(
    *, keys: NodeKeys, base_url: str, key_file: Path, daemon: DaemonManager
) -> None:
    runtime = _build_runtime(
        keys=keys, base_url=base_url, key_file=key_file
    )
    try:
        asyncio.run(runtime.run())
    except KeyboardInterrupt:
        runtime.stop()
        typer.echo("Stopped.")
    finally:
        daemon._cleanup()  # foreground pid file


def _launch(
    *,
    keys: NodeKeys,
    base_url: str,
    key_file: Path,
    foreground: bool,
    daemon: DaemonManager,
) -> None:
    if daemon.is_running():
        typer.secho(
            f"Proxy node is already running with PID {daemon.get_pid()}",
            fg=typer.colors.YELLOW,
        )
        typer.echo("Hint: use 'castrel-proxy proxy restart' to restart it")
        raise typer.Exit(1)
    if not foreground and sys.platform == "win32":
        typer.secho(
            "Background mode is not supported on Windows; use --foreground.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(1)
    if foreground:
        daemon.register_current_process()
        typer.echo(f"Proxy node {keys.node_id} running in foreground (PID {daemon.get_pid()})")
        typer.echo("Press Ctrl+C to stop.")
        _run_foreground(
            keys=keys,
            base_url=base_url,
            key_file=key_file,
            daemon=daemon,
        )
        return

    typer.echo(f"Starting proxy node {keys.node_id} in background...")
    typer.echo(f"  PID file: {daemon.pid_file}")
    typer.echo(f"  Log file: {daemon.log_file}")
    sys.stdout.flush()
    sys.stderr.flush()
    daemon.daemonize()
    _run_foreground(
        keys=keys,
        base_url=base_url,
        key_file=key_file,
        daemon=daemon,
    )


def _load_and_launch(
    *,
    server_url: str,
    key_file: Path,
    foreground: bool,
    restart: bool,
) -> None:
    try:
        keys = load_keys(key_file)
    except Exception as error:
        typer.secho(f"Cannot load node keys: {error}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from error
    base_url = server_url or keys.server_url
    if not base_url:
        typer.secho("No server URL. Pass --server or pair first.", fg=typer.colors.RED, err=True)
        raise typer.Exit(1)
    daemon = get_proxy_daemon_manager()
    if restart and daemon.is_running():
        typer.echo(f"Stopping current proxy node (PID {daemon.get_pid()})...")
        daemon.stop()
    _launch(
        keys=keys,
        base_url=base_url,
        key_file=key_file,
        foreground=foreground,
        daemon=daemon,
    )


@proxy_app.command("pair")
def pair(
    code: str | None = typer.Argument(None, help="Pairing code (or use --code)"),
    server_url: str | None = typer.Argument(None, help="Castrel URL (or use --server)"),
    code_option: str = typer.Option("", "--code", help="Pairing code shown in the Castrel UI"),
    server_option: str = typer.Option(
        "", "--server", help="Castrel base URL, e.g. https://castrel.example.com"
    ),
    name: str = typer.Option("", "--name", help="Display name; defaults to the hostname"),
    key_file: Path = typer.Option(DEFAULT_KEY_FILE, "--key-file", help="Where to store node keys"),
    no_start: bool = typer.Option(False, "--no-start", help="Pair without starting the node"),
    foreground: bool = typer.Option(False, "--foreground", "-f", help="Run in the foreground"),
) -> None:
    """Redeem a pairing code, store keys and start the node in the background."""
    resolved_code = code_option or (code or "")
    resolved_server = server_option or (server_url or "")
    if not resolved_code or not resolved_server:
        typer.secho(
            "Both a pairing code and a server URL are required: "
            "pass --code/--server or two positional arguments.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(2)
    ed_private, ed_public, x_private, x_public = new_private_keys()
    payload = {
        "code": resolved_code,
        "public_key_ed25519": ed_public,
        "public_key_x25519": x_public,
        "name": name or socket.gethostname(),
        "platform": _platform(),
        "python_version": platform.python_version(),
        "protocol": 1,
    }
    url = f"{resolved_server.rstrip('/')}/api/v1/proxies/pairings/redeem"
    try:
        response = httpx.post(url, json=payload, timeout=30.0)
    except httpx.HTTPError as error:
        typer.secho(f"Pairing request failed: {error}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from error
    if response.status_code != 200:
        typer.secho(
            f"Pairing rejected ({response.status_code}): {response.text[:500]}",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(1)
    document = response.json()
    keys = NodeKeys(
        node_id=str(document["node_id"]),
        token=str(document["token"]),
        ed25519_private=ed_private,
        x25519_private=x_private,
        server_url=resolved_server.rstrip("/"),
    )
    keys.save(key_file)
    typer.secho("Paired.", fg=typer.colors.GREEN)
    typer.echo(f"  node_id: {keys.node_id}")
    typer.echo(f"  status: {document.get('status')}")
    typer.echo(f"  keys: {key_file}")
    if no_start:
        typer.echo("Node not started (--no-start). Run 'castrel-proxy proxy start'.")
        return
    if str(document.get("status")) != "active":
        typer.echo("Node is waiting for approval; start it after approval with 'proxy start'.")
        return
    daemon = get_proxy_daemon_manager()
    if daemon.is_running():
        typer.echo(f"Stopping previous proxy node (PID {daemon.get_pid()})...")
        daemon.stop()
    typer.echo("")
    _launch(
        keys=keys,
        base_url=keys.server_url,
        key_file=key_file,
        foreground=foreground,
        daemon=daemon,
    )


@proxy_app.command("start")
def start(
    server_url: str = typer.Option("", "--server", help="Override the paired server URL"),
    key_file: Path = typer.Option(DEFAULT_KEY_FILE, "--key-file"),
    foreground: bool = typer.Option(False, "--foreground", "-f", help="Run in the foreground"),
) -> None:
    """Start the proxy node in the background (default) or foreground."""
    _load_and_launch(
        server_url=server_url,
        key_file=key_file,
        foreground=foreground,
        restart=False,
    )


@proxy_app.command("status")
def status(
    key_file: Path = typer.Option(DEFAULT_KEY_FILE, "--key-file"),
) -> None:
    """Show whether the proxy node process is running."""
    daemon = get_proxy_daemon_manager()
    if daemon.is_running():
        typer.secho(f"Proxy node: running (PID {daemon.get_pid()})", fg=typer.colors.GREEN)
    else:
        typer.secho("Proxy node: stopped", fg=typer.colors.YELLOW)
    typer.echo(f"  PID file: {daemon.pid_file}")
    typer.echo(f"  Log file: {daemon.log_file}")
    try:
        keys = load_keys(key_file)
    except Exception:
        typer.echo("  Node keys: not paired")
        return
    typer.echo(f"  Node ID: {keys.node_id}")
    typer.echo(f"  Server: {keys.server_url or '(not stored)'}")
    for line in _policy_status_lines(load_policy_state(DEFAULT_POLICY_STATE_FILE)):
        typer.echo(line)


@proxy_app.command("stop")
def stop() -> None:
    """Stop the background proxy node."""
    daemon = get_proxy_daemon_manager()
    if not daemon.is_running():
        typer.secho("Proxy node is not running.", fg=typer.colors.YELLOW)
        raise typer.Exit(1)
    pid = daemon.get_pid()
    if daemon.stop():
        typer.secho(f"Stopped proxy node (PID {pid}).", fg=typer.colors.GREEN)
    else:
        typer.secho("Failed to stop the proxy node.", fg=typer.colors.RED, err=True)
        raise typer.Exit(1)


@proxy_app.command("restart")
def restart(
    server_url: str = typer.Option("", "--server", help="Override the paired server URL"),
    key_file: Path = typer.Option(DEFAULT_KEY_FILE, "--key-file"),
    foreground: bool = typer.Option(False, "--foreground", "-f", help="Run in the foreground"),
) -> None:
    """Restart the proxy node with the stored keys."""
    _load_and_launch(
        server_url=server_url,
        key_file=key_file,
        foreground=foreground,
        restart=True,
    )


__all__ = ["proxy_app"]
