"""Local file locations and removal helpers shared by the CLI commands.

Every path is returned by a function rather than a module-level constant so the resolved
``HOME`` is read when a command runs, not when the module is imported. The legacy bridge and
the v2 proxy node keep separate state; both are described here so the node commands and the
root ``uninstall`` command clean the same set of files.
"""

from __future__ import annotations

import shutil
from pathlib import Path


def castrel_dir() -> Path:
    """Root of the local Castrel state, ``~/.castrel``."""
    return Path.home() / ".castrel"


def node_dir() -> Path:
    """Directory holding the v2 node identity and runtime snapshots."""
    return castrel_dir() / "node"


def node_key_file() -> Path:
    """Node id, token and private keys written by ``castrel-proxy proxy pair``."""
    return node_dir() / "keys.json"


def node_policy_file() -> Path:
    """Last policy the platform pushed, persisted for ``proxy status``/``proxy config``."""
    return node_dir() / "policy.json"


def node_outbox_file() -> Path:
    """Bounded offline outbox of frames that could not be sent while disconnected."""
    return node_dir() / "outbox.jsonl"


def bundles_dir() -> Path:
    """Installed connector bundles and their managed runtimes."""
    return castrel_dir() / "bundles"


def sandbox_workspace_dir() -> Path:
    """Default sandbox workspace root, used when ``config.yaml`` does not override it."""
    return castrel_dir() / "sandbox"


def proxy_pid_file() -> Path:
    """PID file of the background v2 node process."""
    return castrel_dir() / "proxy-node.pid"


def proxy_log_file() -> Path:
    """Log file of the background v2 node process."""
    return castrel_dir() / "proxy-node.log"


def legacy_pid_file() -> Path:
    """PID file of the legacy bridge daemon."""
    return castrel_dir() / "castrel-proxy.pid"


def legacy_log_file() -> Path:
    """Log file of the legacy bridge daemon."""
    return castrel_dir() / "castrel-proxy.log"


def legacy_config_file() -> Path:
    """Legacy bridge pairing configuration."""
    return castrel_dir() / "config.yaml"


def node_state_files() -> list[Path]:
    """Files that carry the node identity plus its runtime snapshots."""
    return [node_key_file(), node_policy_file(), node_outbox_file()]


def exec_session_dirs() -> list[Path]:
    """Per-session working directories ``node.exec`` creates under ``~/.castrel``."""
    root = castrel_dir()
    try:
        return sorted(
            entry
            for entry in root.iterdir()
            if entry.is_dir() and entry.name.startswith("proxy-exec-")
        )
    except OSError:
        return []


def remove_file(path: Path) -> tuple[bool, str]:
    """Remove one file.

    Returns ``(removed, message)``. Missing paths and filesystem errors are reported through
    the message instead of raising, so an uninstall never stops half-way with a traceback.
    """
    if not path.exists():
        return False, "not found, skipping"
    try:
        path.unlink()
    except OSError as error:
        return False, f"could not remove ({error}); run manually: rm {path}"
    return True, "removed"


def remove_tree(path: Path) -> tuple[bool, str]:
    """Remove a directory tree; same contract as :func:`remove_file`."""
    if not path.exists():
        return False, "not found, skipping"
    try:
        shutil.rmtree(path)
    except OSError as error:
        return False, f"could not remove ({error}); run manually: rm -rf {path}"
    return True, "removed"


NODE_STATE_LABELS = {
    "keys.json": "node keys",
    "policy.json": "policy snapshot",
    "outbox.jsonl": "offline outbox",
}


def remove_node_state() -> list[tuple[str, Path, bool, str]]:
    """Delete the node identity and its runtime snapshots.

    Both uninstall paths share this so removing the binary can no longer leave private keys
    and the node token on disk.
    """
    results: list[tuple[str, Path, bool, str]] = []
    for path in node_state_files():
        removed, message = remove_file(path)
        label = NODE_STATE_LABELS.get(path.name, path.name)
        results.append((label, path, removed, message))
    return results


__all__ = [
    "bundles_dir",
    "castrel_dir",
    "exec_session_dirs",
    "legacy_config_file",
    "legacy_log_file",
    "legacy_pid_file",
    "node_dir",
    "node_key_file",
    "node_outbox_file",
    "node_policy_file",
    "node_state_files",
    "proxy_log_file",
    "proxy_pid_file",
    "remove_file",
    "remove_node_state",
    "remove_tree",
    "sandbox_workspace_dir",
]
