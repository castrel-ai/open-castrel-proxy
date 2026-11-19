"""CLI for the private-network proxy node: pair, run, inspect, log and remove."""

from __future__ import annotations

import asyncio
import hashlib
import platform
import shutil
import socket
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
import typer

from ..core.daemon import DaemonManager, get_daemon_manager, get_proxy_daemon_manager
from ..core.policy_state import default_policy_state_file, load_policy_state
from ..pairing.device import device_id
from ..pairing.keys import KeysError, NodeKeys, load_keys, new_private_keys
from ..security.policy import parse_policy
from ..transport.spool import Outbox
from ..transport.ws import ProxyNodeRuntime
from . import local_state
from .help import install_options_help

# Kept as a module-level alias; commands resolve the path at call time via _resolve_key_file.
DEFAULT_KEY_FILE = local_state.node_key_file()

KEY_FILE_HELP = "Node key file"

proxy_app = typer.Typer(
    add_completion=False,
    help="Private-network proxy node commands",
    no_args_is_help=True,
)
# The group help lists every subcommand's options as well; see ``cli/help.py``.
install_options_help(proxy_app)


def _resolve_key_file(value: Path | None) -> Path:
    """Resolve ``--key-file`` at call time so the default follows the current ``HOME``."""
    return value or local_state.node_key_file()


def _report(prefix: str, path: Path, removed: bool, message: str) -> None:
    """Print one per-path uninstall/unpair result in the legacy command's style."""
    if removed:
        typer.secho(f"✓ Removed {prefix}: {path}", fg=typer.colors.GREEN)
    elif "not found" in message:
        typer.echo(f"  {prefix}: not found, skipping ({path})")
    else:
        typer.secho(f"⚠ {prefix}: {message}", fg=typer.colors.YELLOW)


def _tail_lines(path: Path, lines: int) -> list[str]:
    """Read the last ``lines`` lines without loading the whole file into memory."""
    from file_read_backwards import (  # type: ignore[import-untyped]
        FileReadBackwards,
    )

    collected: list[str] = []
    try:
        with FileReadBackwards(str(path), encoding="utf-8") as reader:
            for line in reader:
                collected.append(line)
                if len(collected) >= lines:
                    break
    except (OSError, UnicodeDecodeError):
        text = path.read_bytes().decode("utf-8", errors="replace")
        return text.splitlines()[-lines:]
    collected.reverse()
    return collected


def _stop_daemon(kind: str, daemon: DaemonManager) -> None:
    """Stop one daemon before its state is deleted so the server sees the disconnect."""
    if not daemon.is_running():
        return
    pid = daemon.get_pid()
    typer.echo(f"Stopping {kind} (PID {pid})...")
    if daemon.stop():
        typer.secho(f"✓ Stopped {kind}", fg=typer.colors.GREEN)
    else:
        typer.secho(f"⚠ Failed to stop {kind}, proceeding anyway", fg=typer.colors.YELLOW)


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
        policy_state_path=_policy_state_file(),
        outbox=Outbox(local_state.node_outbox_file()),
    )


def _policy_state_file() -> Path:
    """Path of the policy snapshot the running node writes and the CLI reads."""
    return default_policy_state_file()


def _run_foreground(
    *, keys: NodeKeys, base_url: str, key_file: Path, daemon: DaemonManager
) -> None:
    runtime = _build_runtime(keys=keys, base_url=base_url, key_file=key_file)
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
    key_file: Path | None = typer.Option(None, "--key-file", help=KEY_FILE_HELP),
    no_start: bool = typer.Option(False, "--no-start", help="Pair without starting the node"),
    foreground: bool = typer.Option(False, "--foreground", "-f", help="Run in the foreground"),
    force: bool = typer.Option(
        False,
        "--force",
        help="Overwrite local keys after the previous node was revoked or deleted",
    ),
) -> None:
    """Redeem a pairing code, store keys and start the node in the background."""
    key_file = _resolve_key_file(key_file)
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
    try:
        existing = load_keys(key_file)
    except Exception:
        existing = None
    if existing is not None and not force:
        typer.secho(
            f"This device is already paired as node {existing.node_id}. "
            "Revoke or delete it in Castrel first, or pass --force after doing so.",
            fg=typer.colors.YELLOW,
            err=True,
        )
        typer.echo("Hint: use 'castrel-proxy proxy unpair' to remove the local pairing first.")
        raise typer.Exit(1)
    ed_private, ed_public, x_private, x_public = new_private_keys()
    payload = {
        "code": resolved_code,
        "public_key_ed25519": ed_public,
        "public_key_x25519": x_public,
        "name": name or socket.gethostname(),
        "platform": _platform(),
        "python_version": platform.python_version(),
        "device_id": device_id(),
        "protocol": 1,
    }
    url = f"{resolved_server.rstrip('/')}/api/v1/proxies/pairings/redeem"
    try:
        response = httpx.post(url, json=payload, timeout=30.0)
    except httpx.HTTPError as error:
        typer.secho(f"Pairing request failed: {error}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from error
    if response.status_code != 200:
        try:
            detail = response.json().get("detail") or {}
        except ValueError:
            detail = {}
        message = detail.get("message") if isinstance(detail, dict) else ""
        if response.status_code == 409:
            typer.secho(
                message
                or "This device is already paired. Revoke or delete the existing node first.",
                fg=typer.colors.RED,
                err=True,
            )
        else:
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
        # Success, not a failure: the documented Docker flow pairs in a throwaway container and
        # starts the node from the container created next. Say so, instead of reading like an
        # error that left the node down.
        typer.secho(
            "Pairing complete; this container did not start the node.",
            fg=typer.colors.GREEN,
        )
        typer.echo("  Start it with 'castrel-proxy proxy start', or continue with the next")
        typer.echo("  install step, which runs it in a detached container.")
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
    key_file: Path | None = typer.Option(None, "--key-file", help=KEY_FILE_HELP),
    foreground: bool = typer.Option(False, "--foreground", "-f", help="Run in the foreground"),
) -> None:
    """Start the proxy node in the background (default) or foreground."""
    _load_and_launch(
        server_url=server_url,
        key_file=_resolve_key_file(key_file),
        foreground=foreground,
        restart=False,
    )


@proxy_app.command("status")
def status(
    key_file: Path | None = typer.Option(None, "--key-file", help=KEY_FILE_HELP),
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
        keys = load_keys(_resolve_key_file(key_file))
    except KeysError:
        typer.echo("  Node keys: not paired")
        typer.echo("  Hint: run 'castrel-proxy proxy pair' to pair this node.")
        return
    typer.echo(f"  Node ID: {keys.node_id}")
    typer.echo(f"  Server: {keys.server_url or '(not stored)'}")
    for line in _policy_status_lines(load_policy_state(_policy_state_file())):
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
    key_file: Path | None = typer.Option(None, "--key-file", help=KEY_FILE_HELP),
    foreground: bool = typer.Option(False, "--foreground", "-f", help="Run in the foreground"),
) -> None:
    """Restart the proxy node with the stored keys."""
    _load_and_launch(
        server_url=server_url,
        key_file=_resolve_key_file(key_file),
        foreground=foreground,
        restart=True,
    )


@proxy_app.command("logs")
def logs(
    lines: int = typer.Option(50, "--lines", "-n", min=1, help="Show the last N lines"),
    follow: bool = typer.Option(False, "--follow", "-f", help="Follow the log in real time"),
) -> None:
    """Show the background node log, or follow it until Ctrl+C."""
    log_file = local_state.proxy_log_file()
    if not log_file.exists():
        typer.secho(f"Log file not found: {log_file}", fg=typer.colors.RED, err=True)
        if local_state.legacy_log_file().exists():
            typer.echo(
                "Hint: a legacy bridge log exists at "
                f"{local_state.legacy_log_file()}; it is not the proxy node log.",
                err=True,
            )
        typer.echo("Hint: start the node with 'castrel-proxy proxy start'.", err=True)
        raise typer.Exit(1)

    if follow:
        typer.echo(f"Following {log_file} (Ctrl+C to exit)")
        try:
            subprocess.run(["tail", "-f", str(log_file)], check=False)
        except KeyboardInterrupt:
            typer.echo("\nStopped following logs")
        except FileNotFoundError:
            typer.secho("The 'tail' command is not available", fg=typer.colors.YELLOW, err=True)
            typer.echo("Hint: omit '--follow' to print the last lines instead", err=True)
            raise typer.Exit(1) from None
        return

    typer.echo(f"Last {lines} lines from {log_file}:\n")
    try:
        for line in _tail_lines(log_file, lines):
            typer.echo(line.rstrip())
    except OSError as error:
        typer.secho(f"Failed to read the log file: {error}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from error


@proxy_app.command("config")
def config(
    key_file: Path | None = typer.Option(None, "--key-file", help=KEY_FILE_HELP),
) -> None:
    """Show the local node configuration, with secrets redacted."""
    resolved = _resolve_key_file(key_file)
    daemon = get_proxy_daemon_manager()

    typer.secho("=== Proxy Node Configuration ===", bold=True)
    typer.echo(f"Key file: {resolved}")
    try:
        mode = oct(resolved.stat().st_mode & 0o777)
        typer.echo(f"Key file mode: {mode}")
    except OSError:
        typer.echo("Key file mode: (missing)")
    typer.echo(f"PID file: {daemon.pid_file}")
    typer.echo(f"Log file: {daemon.log_file}")
    typer.echo(f"Bundles dir: {local_state.bundles_dir()}")
    typer.echo(f"Outbox: {local_state.node_outbox_file()}")
    typer.echo(f"Policy snapshot: {_policy_state_file()}")

    try:
        keys = load_keys(resolved)
    except KeysError:
        typer.secho("Pairing: not paired", fg=typer.colors.YELLOW)
        typer.echo("Hint: run 'castrel-proxy proxy pair' to pair this node.")
        return

    digest = hashlib.sha256(keys.token.encode("utf-8")).hexdigest()[:8]
    typer.secho("Pairing: paired", fg=typer.colors.GREEN)
    typer.echo(f"  Node ID: {keys.node_id}")
    typer.echo(f"  Server: {keys.server_url or '(not stored)'}")
    typer.echo(f"  Token: stored, sha256 {digest}... (value never printed)")
    for line in _policy_status_lines(load_policy_state(_policy_state_file())):
        typer.echo(line)


@proxy_app.command("unpair")
def unpair(
    key_file: Path | None = typer.Option(None, "--key-file", help=KEY_FILE_HELP),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt"),
) -> None:
    """Remove the local pairing: node keys, policy snapshot and offline outbox."""
    resolved = _resolve_key_file(key_file)
    try:
        keys = load_keys(resolved)
    except KeysError:
        typer.secho("This node is not paired.", fg=typer.colors.YELLOW)
        raise typer.Exit(0) from None

    typer.secho("=== Unpair Proxy Node ===", bold=True)
    typer.echo(f"  Node ID: {keys.node_id}")
    typer.echo(f"  Server: {keys.server_url or '(not stored)'}")
    for label, path in (
        ("Node keys", resolved),
        ("Policy snapshot", _policy_state_file()),
        ("Offline outbox", local_state.node_outbox_file()),
    ):
        typer.echo(f"  {label}: {path}")

    if not yes and not typer.confirm("Are you sure you want to unpair?"):
        typer.echo("Cancelled")
        raise typer.Exit(0)

    typer.echo("")
    # Stop first so the platform sees the disconnect and marks the node offline.
    _stop_daemon("proxy node", get_proxy_daemon_manager())

    typer.echo("")
    for label, path, removed, message in local_state.remove_node_state():
        _report(label, path, removed, message)

    typer.secho("\n✓ Unpaired", fg=typer.colors.GREEN)
    typer.echo(
        "The server record still exists. Revoke or delete this node in Castrel "
        "(/proxies) before pairing the machine again."
    )


@proxy_app.command("uninstall")
def uninstall(
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt"),
    keep_logs: bool = typer.Option(False, "--keep-logs", help="Keep log files"),
    keep_binary: bool = typer.Option(False, "--keep-binary", help="Keep the castrel-proxy binary"),
) -> None:
    """Remove the local node, legacy bridge state and the castrel-proxy binary."""
    node_daemon = get_proxy_daemon_manager()
    legacy_daemon = get_daemon_manager()
    binary = shutil.which("castrel-proxy")
    binary_path = Path(binary).resolve() if binary else None

    typer.secho("=== Uninstall Castrel Proxy ===", bold=True)
    typer.echo("\nThe following local data will be removed:")
    typer.echo(f"  Node keys       : {local_state.node_key_file()}")
    typer.echo(f"  Policy snapshot : {local_state.node_policy_file()}")
    typer.echo(f"  Offline outbox  : {local_state.node_outbox_file()}")
    typer.echo(f"  PID files       : {node_daemon.pid_file}, {legacy_daemon.pid_file}")
    if keep_logs:
        typer.echo(f"  Log files       : {node_daemon.log_file}, {legacy_daemon.log_file} (kept)")
    else:
        typer.echo(f"  Log files       : {node_daemon.log_file}, {legacy_daemon.log_file}")
    typer.echo(f"  Bundles         : {local_state.bundles_dir()}")
    typer.echo(f"  Session dirs    : {local_state.sandbox_workspace_dir()}, proxy-exec-*")
    typer.echo(f"  Legacy config   : {local_state.legacy_config_file()}")
    if binary_path and binary_path.exists():
        if keep_binary:
            typer.echo(f"  Binary          : {binary_path} (kept)")
        else:
            typer.echo(f"  Binary          : {binary_path}")
    typer.echo("  Kept            : ~/.castrel/skills/, mcp.json, whitelist.conf (user content)")

    if not yes and not typer.confirm("\nProceed with uninstall?"):
        typer.echo("Cancelled")
        raise typer.Exit(0)

    typer.echo("")
    _stop_daemon("proxy node", node_daemon)
    _stop_daemon("legacy bridge", legacy_daemon)

    typer.echo("")
    for label, path, removed, message in local_state.remove_node_state():
        _report(label, path, removed, message)
    _report("PID file", node_daemon.pid_file, *local_state.remove_file(node_daemon.pid_file))
    _report("PID file", legacy_daemon.pid_file, *local_state.remove_file(legacy_daemon.pid_file))
    if not keep_logs:
        _report("log file", node_daemon.log_file, *local_state.remove_file(node_daemon.log_file))
        _report(
            "log file", legacy_daemon.log_file, *local_state.remove_file(legacy_daemon.log_file)
        )
    _report(
        "bundles",
        local_state.bundles_dir(),
        *local_state.remove_tree(local_state.bundles_dir()),
    )
    _report(
        "sandbox workspace",
        local_state.sandbox_workspace_dir(),
        *local_state.remove_tree(local_state.sandbox_workspace_dir()),
    )
    for session_dir in local_state.exec_session_dirs():
        _report("session dir", session_dir, *local_state.remove_tree(session_dir))
    _report(
        "legacy config",
        local_state.legacy_config_file(),
        *local_state.remove_file(local_state.legacy_config_file()),
    )

    if binary_path and binary_path.exists() and not keep_binary:
        _report("binary", binary_path, *local_state.remove_file(binary_path))

    typer.secho("\n✓ Castrel Proxy uninstalled successfully", fg=typer.colors.GREEN)


__all__ = ["proxy_app"]
