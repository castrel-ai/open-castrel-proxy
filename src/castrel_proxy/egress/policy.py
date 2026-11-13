"""Destination checks and DNS validation for node egress streams."""

from __future__ import annotations

import asyncio
import ipaddress
import socket

from ..security.policy import ProxyPolicy

# Cloud metadata endpoints are never valid egress targets.
BLOCKED_ADDRESSES = frozenset(
    {
        "169.254.169.254",
        "100.100.100.200",
        "fd00:ec2::254",
    }
)


def _host_matches(pattern: str, host: str) -> bool:
    candidate = pattern.strip().lower().rstrip(".")
    target = host.strip().lower().rstrip(".")
    if candidate in ("", "*"):
        return True
    if candidate.startswith("*."):
        return target.endswith(candidate[1:]) and target != candidate[2:]
    if "/" in candidate:
        try:
            return ipaddress.ip_address(target) in ipaddress.ip_network(candidate, strict=False)
        except ValueError:
            return False
    try:
        return ipaddress.ip_address(target) == ipaddress.ip_address(candidate)
    except ValueError:
        return target == candidate


def check_destination(policy: ProxyPolicy, host: str, port: int) -> str | None:
    network = policy.network
    if not network.enabled:
        return "Network egress is disabled by the node policy."
    if not any(_host_matches(item, host) for item in network.allowed_destinations):
        return f"Target '{host}:{port}' is not allowed by the node policy."
    if network.allowed_ports and int(port) not in network.allowed_ports:
        return f"Port {port} on '{host}' is not allowed by the node policy."
    return None


def check_stream_limits(
    policy: ProxyPolicy, node_streams: int, instance_streams: int
) -> str | None:
    network = policy.network
    node_limit = policy.max_concurrency or network.max_streams_per_node
    if node_limit is not None and node_streams >= node_limit:
        return "The node reached its configured concurrency limit."
    if (
        network.max_streams_per_instance is not None
        and instance_streams >= network.max_streams_per_instance
    ):
        return "The connector instance reached its network stream limit."
    return None


def blocked_address(address: str) -> bool:
    if address in BLOCKED_ADDRESSES:
        return True
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        return True
    return parsed.is_link_local or parsed.is_multicast or parsed.is_unspecified


async def resolve_destination(host: str, port: int) -> list[str]:
    """Resolve once; the caller validates and connects to a returned IP (no rebinding)."""
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as error:
        raise ValueError(f"DNS resolution failed for '{host}'") from error
    addresses = sorted({str(info[4][0]) for info in infos if info[4]})
    if not addresses:
        raise ValueError(f"DNS resolution returned no addresses for '{host}'")
    return addresses


__all__ = [
    "BLOCKED_ADDRESSES",
    "blocked_address",
    "check_destination",
    "check_stream_limits",
    "resolve_destination",
]
