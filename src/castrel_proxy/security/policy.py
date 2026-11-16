"""Node-side policy parse and re-checks.

The server always enforces its own policy before dispatch. The node repeats the checks it
can enforce locally (connector/tool allowlists, timeout, result size, exec mode) and rejects
requests carrying a policy version older than the one it knows.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ExecPolicy:
    enabled: bool = True
    mode: str = "allowlist"
    allowlist: list[str] = field(default_factory=list)
    max_timeout_seconds: float = 600.0
    max_output_bytes: int = 262_144


@dataclass
class NetworkPolicy:
    enabled: bool = True
    allowed_destinations: list[str] = field(default_factory=lambda: ["*"])
    allowed_ports: list[int] = field(default_factory=list)
    max_streams_per_node: int | None = None
    max_streams_per_instance: int | None = None
    idle_timeout_seconds: int | None = None
    max_bytes_per_stream: int | None = None


@dataclass
class FilesPolicy:
    enabled: bool = True
    roots: list[str] = field(default_factory=lambda: ["~"])
    deny_paths: list[str] = field(default_factory=lambda: ["~/.castrel", "~/.ssh"])
    allow_symlinks: bool = False
    max_file_bytes: int = 10 * 1024 * 1024
    max_read_bytes: int = 262_144


@dataclass
class SandboxPolicy:
    enabled: bool = True
    network: str = "none"
    allowed_images: list[str] = field(
        default_factory=lambda: ["castrelai/castrel-sandbox:latest"]
    )
    allow_caller_image: bool = False
    allow_caller_network: bool = False
    max_timeout_seconds: float = 300.0


@dataclass
class ProxyPolicy:
    allowed_connectors: list[str] = field(default_factory=lambda: ["*"])
    allowed_tools: dict[str, list[str]] = field(default_factory=dict)
    denied_tools: dict[str, list[str]] = field(default_factory=dict)
    max_concurrency: int | None = None
    max_timeout_seconds: float | None = None
    max_result_bytes: int | None = None
    network: NetworkPolicy = field(default_factory=NetworkPolicy)
    exec: ExecPolicy = field(default_factory=ExecPolicy)
    files: FilesPolicy = field(default_factory=FilesPolicy)
    sandbox: SandboxPolicy = field(default_factory=SandboxPolicy)


def _string_list(value: Any, fallback: list[str]) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        return fallback
    return [str(item) for item in value]


def _tool_map(value: Any) -> dict[str, list[str]]:
    if not isinstance(value, dict):
        return {}
    return {
        str(key): _string_list(items, []) for key, items in value.items() if isinstance(key, str)
    }


def parse_policy(document: dict[str, Any] | None) -> ProxyPolicy:
    document = document if isinstance(document, dict) else {}
    exec_document = document.get("exec") if isinstance(document.get("exec"), dict) else {}
    network_document = (
        document.get("network") if isinstance(document.get("network"), dict) else {}
    )
    if not network_document and document.get("allowed_destinations"):
        # Legacy top-level destination list; map it into the network allowlist.
        network_document = {"allowed_destinations": document["allowed_destinations"]}
    network = NetworkPolicy(
        enabled=bool(network_document.get("enabled", True)),
        allowed_destinations=_string_list(
            network_document.get("allowed_destinations"), ["*"]
        ),
        allowed_ports=_int_list(network_document.get("allowed_ports")),
        max_streams_per_node=_positive_int(network_document.get("max_streams_per_node")),
        max_streams_per_instance=_positive_int(
            network_document.get("max_streams_per_instance")
        ),
        idle_timeout_seconds=_positive_int(network_document.get("idle_timeout_seconds")),
        max_bytes_per_stream=_positive_int(
            network_document.get("max_bytes_per_stream")
        ),
    )
    files_document = (
        document.get("filesystem") if isinstance(document.get("filesystem"), dict) else {}
    )
    sandbox_document = (
        document.get("sandbox") if isinstance(document.get("sandbox"), dict) else {}
    )
    return ProxyPolicy(
        allowed_connectors=_string_list(document.get("allowed_connectors"), ["*"]),
        allowed_tools=_tool_map(document.get("allowed_tools")),
        denied_tools=_tool_map(document.get("denied_tools")),
        max_concurrency=_positive_int(document.get("max_concurrency")),
        max_timeout_seconds=_positive_float(document.get("max_timeout_seconds")),
        max_result_bytes=_positive_int(document.get("max_result_bytes")),
        network=network,
        exec=ExecPolicy(
            enabled=bool(exec_document.get("enabled", True)),
            mode=str(exec_document.get("mode") or "allowlist"),
            allowlist=_string_list(exec_document.get("allowlist"), []),
            max_timeout_seconds=_positive_float(exec_document.get("max_timeout_seconds")) or 600.0,
            max_output_bytes=_positive_int(exec_document.get("max_output_bytes")) or 262_144,
        ),
        files=FilesPolicy(
            enabled=bool(files_document.get("enabled", True)),
            roots=_string_list(files_document.get("roots"), ["~"]),
            deny_paths=_string_list(
                files_document.get("deny_paths"), ["~/.castrel", "~/.ssh"]
            ),
            allow_symlinks=bool(files_document.get("allow_symlinks", False)),
            max_file_bytes=_positive_int(files_document.get("max_file_bytes")) or 10 * 1024 * 1024,
            max_read_bytes=_positive_int(files_document.get("max_read_bytes")) or 262_144,
        ),
        sandbox=SandboxPolicy(
            enabled=bool(sandbox_document.get("enabled", True)),
            network=str(sandbox_document.get("network") or "none"),
            allowed_images=_string_list(
                sandbox_document.get("allowed_images"),
                ["castrelai/castrel-sandbox:latest"],
            ),
            allow_caller_image=bool(sandbox_document.get("allow_caller_image", False)),
            allow_caller_network=bool(sandbox_document.get("allow_caller_network", False)),
            max_timeout_seconds=_positive_float(sandbox_document.get("max_timeout_seconds"))
            or 300.0,
        ),
    )


def _int_list(value: Any) -> list[int]:
    if not isinstance(value, list):
        return []
    ports: list[int] = []
    for item in value:
        try:
            port = int(item)
        except (TypeError, ValueError):
            continue
        if 1 <= port <= 65535:
            ports.append(port)
    return ports


def _positive_int(value: Any) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _positive_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _matches(items: list[str], value: str) -> bool:
    return "*" in items or value in items


def check_connector(policy: ProxyPolicy, connector_id: str) -> str | None:
    if not _matches(policy.allowed_connectors, connector_id):
        return f"Connector '{connector_id}' is not allowed by the node policy."
    return None


def check_tool(policy: ProxyPolicy, connector_id: str, tool_name: str) -> str | None:
    denied = policy.denied_tools.get(connector_id) or policy.denied_tools.get("*") or []
    if _matches(denied, tool_name):
        return f"Tool '{tool_name}' is denied by the node policy."
    allowed = policy.allowed_tools.get(connector_id) or policy.allowed_tools.get("*") or ["*"]
    if not _matches(allowed, tool_name):
        return f"Tool '{tool_name}' is not allowed by the node policy."
    return None


def check_exec(policy: ProxyPolicy, command: str) -> str | None:
    if not policy.exec.enabled or policy.exec.mode == "deny_all":
        return "Command execution is disabled by the node policy."
    if policy.exec.mode == "allowlist" and policy.exec.allowlist:
        base = command.strip().split()[0] if command.strip() else ""
        if not _matches(policy.exec.allowlist, base):
            return f"Command '{base or '(empty)'}' is not allowed by the node policy."
    return None


def effective_timeout(policy: ProxyPolicy, requested: float, cap: float) -> float:
    limit = min(value for value in (requested, cap, policy.max_timeout_seconds or cap))
    return max(0.001, limit)


def effective_result_bytes(policy: ProxyPolicy, fallback: int) -> int:
    return int(policy.max_result_bytes or fallback)


__all__ = [
    "ExecPolicy",
    "FilesPolicy",
    "NetworkPolicy",
    "ProxyPolicy",
    "SandboxPolicy",
    "check_connector",
    "check_exec",
    "check_tool",
    "effective_result_bytes",
    "effective_timeout",
    "parse_policy",
]
