"""Last policy received from the platform, persisted for `castrel-proxy proxy status`.

The daemon owns the live policy; the status command runs in a separate process, so the
limits are snapshotted to a small JSON file whenever the platform sends a policy.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_POLICY_STATE_FILE = Path.home() / ".castrel" / "node" / "policy.json"


def save_policy_state(
    path: Path | None, *, version: int, document: dict[str, Any]
) -> None:
    """Write the policy snapshot atomically; a failure must never break the connection."""
    if path is None:
        return
    payload = {
        "policy_version": int(version),
        "received_at": int(time.time() * 1000),
        "policy": dict(document),
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f"{path.name}.tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, path)
    except OSError:
        logger.warning("Unable to persist the proxy policy snapshot to %s", path, exc_info=True)


def load_policy_state(path: Path) -> dict[str, Any] | None:
    """Read the snapshot; missing or corrupt state reads as None."""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        payload = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    policy = payload.get("policy")
    if not isinstance(policy, dict):
        return None
    return {
        "policy_version": int(payload.get("policy_version") or 0),
        "received_at": int(payload.get("received_at") or 0),
        "policy": policy,
    }


__all__ = ["DEFAULT_POLICY_STATE_FILE", "load_policy_state", "save_policy_state"]
