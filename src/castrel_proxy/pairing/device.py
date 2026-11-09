"""Stable device identity used to reject pairing the same machine twice."""

from __future__ import annotations

import hashlib
import platform
import socket
import subprocess
from pathlib import Path


def _macos_platform_uuid() -> str:
    try:
        result = subprocess.run(
            ["ioreg", "-rd1", "-c", "IOPlatformExpertDevice"],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    for line in result.stdout.splitlines():
        if "IOPlatformUUID" in line:
            return line.split("=", 1)[-1].strip().strip('"')
    return ""


def _linux_machine_id() -> str:
    for name in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
        try:
            value = Path(name).read_text(encoding="ascii").strip()
        except OSError:
            continue
        if value:
            return value
    return ""


def device_id() -> str:
    """A stable, non-secret identifier; hashed so raw machine ids never leave the node."""
    raw = (
        _macos_platform_uuid()
        or _linux_machine_id()
        or f"{socket.gethostname()}:{platform.system()}:{platform.machine()}"
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


__all__ = ["device_id"]
