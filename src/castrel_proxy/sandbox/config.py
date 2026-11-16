"""
Sandbox configuration

User-configurable sandbox settings loaded from the ``sandbox`` section of
``~/.castrel/config.yaml``.  Every field has a safe default so the sandbox
subsystem works out-of-the-box while remaining fully overridable.

The sandbox requires a Docker-compatible engine (Docker Engine, OrbStack,
Colima, Rancher Desktop, etc.).  Docker Desktop is **not** required; any
environment that exposes a standard Docker socket works.

Example ``config.yaml``::

    sandbox:
      enabled: true
      image: castrel/sandbox:latest
      workspace_root: ~/.castrel/sandbox
      network: none           # none | bridge
      idle_timeout: 1800      # seconds; reap idle containers
      reaper_interval: 60     # seconds; idle reaper scan interval
      max_concurrent: 4       # max simultaneous session containers
      exec_timeout: 300       # default per-execution timeout (seconds)
      resources:
        cpus: "2"
        memory: 2g
        pids_limit: 512

    skills:
      directory: ~/.castrel/skills
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional

# Default image includes the dependency-complete sandbox and built-in skills.
DEFAULT_IMAGE = "castrelai/castrel-sandbox:latest"
DEFAULT_WORKSPACE_ROOT = "~/.castrel/sandbox"
DEFAULT_SKILLS_DIR = "~/.castrel/skills"


@dataclass
class SandboxResources:
    """Container resource limits (all optional)."""

    cpus: Optional[str] = "2"
    memory: Optional[str] = "2g"
    pids_limit: Optional[int] = 512

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> "SandboxResources":
        data = data or {}
        pids = data.get("pids_limit", 512)
        try:
            pids_limit = int(pids) if pids is not None else None
        except (TypeError, ValueError):
            pids_limit = 512
        return cls(
            cpus=_opt_str(data.get("cpus", "2")),
            memory=_opt_str(data.get("memory", "2g")),
            pids_limit=pids_limit,
        )


@dataclass
class SandboxConfig:
    """Resolved sandbox configuration with defaults applied."""

    enabled: bool = True
    engine: str = "docker"  # always docker; field kept for forward-compat
    image: str = DEFAULT_IMAGE
    workspace_root: str = DEFAULT_WORKSPACE_ROOT
    user_skills_dir: str = DEFAULT_SKILLS_DIR
    network: str = "none"  # none | bridge
    idle_timeout: int = 1800
    reaper_interval: float = 60.0
    max_concurrent: int = 4
    exec_timeout: int = 300
    # Output cap for one exec; the platform applies the same limit on its side.
    max_output_bytes: int = 262144
    # Pull the image on demand; disable to require a pre-loaded image on the node.
    pull_image: bool = True
    create_timeout: float = 60.0
    pull_timeout: float = 600.0
    resources: SandboxResources = field(default_factory=SandboxResources)

    @property
    def workspace_root_path(self) -> Path:
        return Path(self.workspace_root).expanduser()

    @property
    def user_skills_dir_path(self) -> Path:
        return Path(self.user_skills_dir).expanduser()

    @classmethod
    def from_dict(
        cls,
        data: Optional[Dict[str, Any]],
        *,
        user_skills_dir: Optional[str] = None,
    ) -> "SandboxConfig":
        data = data or {}
        return cls(
            enabled=_coerce_bool(data.get("enabled", True)),
            engine=_opt_str(data.get("engine", "docker")) or "docker",
            image=_opt_str(data.get("image", DEFAULT_IMAGE)) or DEFAULT_IMAGE,
            workspace_root=_opt_str(data.get("workspace_root", DEFAULT_WORKSPACE_ROOT))
            or DEFAULT_WORKSPACE_ROOT,
            user_skills_dir=_opt_str(user_skills_dir or DEFAULT_SKILLS_DIR) or DEFAULT_SKILLS_DIR,
            network=_opt_str(data.get("network", "none")) or "none",
            idle_timeout=_coerce_int(data.get("idle_timeout"), 1800),
            reaper_interval=_coerce_float(data.get("reaper_interval"), 60.0),
            max_concurrent=_coerce_int(data.get("max_concurrent"), 4),
            exec_timeout=_coerce_int(data.get("exec_timeout"), 300),
            max_output_bytes=_coerce_int(data.get("max_output_bytes"), 262144),
            pull_image=_coerce_bool(data.get("pull_image", True)),
            create_timeout=_coerce_float(data.get("create_timeout"), 60.0),
            pull_timeout=_coerce_float(data.get("pull_timeout"), 600.0),
            resources=SandboxResources.from_dict(data.get("resources")),
        )


def _opt_str(value: object) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _coerce_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    if isinstance(value, (int, float)):
        return bool(value)
    return False


def _coerce_int(value: object, default: int) -> int:
    try:
        if value is None:
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def _coerce_float(value: object, default: float) -> float:
    try:
        if value is None:
            return default
        parsed = float(str(value))
        return parsed if parsed > 0 else default
    except (TypeError, ValueError):
        return default
