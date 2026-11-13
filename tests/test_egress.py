"""Node egress: policy, DNS safety, TCP round trips, reconnect cleanup and outbox exclusion."""

from __future__ import annotations

import asyncio
from typing import Any

import aiohttp
import pytest
from castrel_proxy.egress import EgressManager
from castrel_proxy.pairing.keys import NodeKeys
from castrel_proxy.protocol.streams import decode_stream_data, encode_stream_data
from castrel_proxy.security.policy import parse_policy
from castrel_proxy.transport.spool import Outbox
from castrel_proxy.transport.ws import ProxyNodeRuntime

STREAM_ID = "a" * 32


async def echo_server() -> tuple[asyncio.AbstractServer, int]:
    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        while True:
            payload = await reader.read(65536)
            if not payload:
                break
            writer.write(payload)
            await writer.drain()
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    return server, int(server.sockets[0].getsockname()[1])


async def wait_for_frame(
    frames: list[tuple[str, dict[str, Any]]], message_type: str, timeout: float = 2.0
) -> dict[str, Any]:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        for kind, data in frames:
            if kind == message_type:
                return data
        await asyncio.sleep(0.01)
    raise AssertionError(f"no {message_type} frame")


def manager(
    policy: dict[str, Any] | None = None,
    frames: list[tuple[str, dict[str, Any]]] | None = None,
) -> tuple[EgressManager, list[tuple[str, dict[str, Any]]]]:
    captured = frames if frames is not None else []

    async def send(message_type: str, data: dict[str, Any]) -> None:
        captured.append((message_type, data))

    instance = EgressManager(send=send, policy_getter=lambda: parse_policy(policy))
    instance.configure({}, policy_version=1)
    return instance, captured


@pytest.mark.asyncio
async def test_stream_forwards_bytes_and_grants_credit() -> None:
    server, port = await echo_server()
    egress, frames = manager({"network": {"allowed_destinations": ["127.0.0.1"]}})
    try:
        await egress.open(
            {
                "stream_id": STREAM_ID,
                "host": "127.0.0.1",
                "port": port,
                "connector_id": "prometheus",
                "instance_id": "3001",
                "policy_version": 1,
            }
        )
        opened = await wait_for_frame(frames, "stream.opened")
        assert opened["resolved_ip"] == "127.0.0.1"
        assert STREAM_ID in egress.streams

        egress.feed({"stream_id": STREAM_ID, "data": encode_stream_data(b"ping")})
        echoed = await wait_for_frame(frames, "stream.data")
        assert decode_stream_data(echoed["data"]) == b"ping"
        assert echoed["seq"] == 0

        await egress.close_stream({"stream_id": STREAM_ID, "reason": "done"})
        assert STREAM_ID not in egress.streams
        assert any(kind == "stream.close" for kind, _ in frames)
    finally:
        await egress.stop()
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_destination_and_port_policy_deny_the_stream() -> None:
    server, port = await echo_server()
    try:
        denied, frames = manager({"network": {"allowed_destinations": ["*.internal"]}})
        await denied.open(
            {"stream_id": STREAM_ID, "host": "127.0.0.1", "port": port, "policy_version": 1}
        )
        error = await wait_for_frame(frames, "stream.error")
        assert error["code"] == "stream_denied"
        await denied.stop()

        ports, frames = manager({"network": {"allowed_ports": [80]}})
        await ports.open(
            {"stream_id": STREAM_ID, "host": "127.0.0.1", "port": port, "policy_version": 1}
        )
        error = await wait_for_frame(frames, "stream.error")
        assert error["code"] == "stream_denied"
        await ports.stop()
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_metadata_address_is_blocked() -> None:
    egress, frames = manager({"network": {"allowed_destinations": ["*"]}})
    try:
        await egress.open(
            {
                "stream_id": STREAM_ID,
                "host": "169.254.169.254",
                "port": 80,
                "policy_version": 1,
            }
        )
        error = await wait_for_frame(frames, "stream.error")
        assert error["code"] == "stream_denied"
    finally:
        await egress.stop()


@pytest.mark.asyncio
async def test_stale_policy_version_and_disabled_network() -> None:
    egress, frames = manager({"network": {"allowed_destinations": ["*"]}})
    egress.policy_version = 3
    try:
        await egress.open(
            {"stream_id": STREAM_ID, "host": "127.0.0.1", "port": 80, "policy_version": 2}
        )
        error = await wait_for_frame(frames, "stream.error")
        assert error["code"] == "stream_denied"
    finally:
        await egress.stop()

    disabled, frames = manager({"network": {"enabled": False}})
    try:
        await disabled.open(
            {"stream_id": STREAM_ID, "host": "127.0.0.1", "port": 80, "policy_version": 1}
        )
        error = await wait_for_frame(frames, "stream.error")
        assert error["code"] == "stream_denied"
    finally:
        await disabled.stop()


@pytest.mark.asyncio
async def test_reconnect_cleanup_closes_open_streams() -> None:
    server, port = await echo_server()
    egress, frames = manager({"network": {"allowed_destinations": ["127.0.0.1"]}})
    try:
        await egress.open(
            {"stream_id": STREAM_ID, "host": "127.0.0.1", "port": port, "policy_version": 1}
        )
        await wait_for_frame(frames, "stream.opened")
        await egress.close_all("reconnect")
        assert egress.streams == {}
        assert any(kind == "stream.close" for kind, _ in frames)
    finally:
        await egress.stop()
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_stream_frames_are_never_spooled(tmp_path) -> None:
    keys = NodeKeys(node_id="2001", token="token", ed25519_private="", x25519_private="")

    runtime = ProxyNodeRuntime(
        server_url="http://127.0.0.1:1",
        keys=keys,
        outbox=Outbox(tmp_path / "outbox.jsonl"),
    )

    class FailingSocket:
        async def send_json(self, _frame: dict[str, Any]) -> None:
            raise aiohttp.ClientError("socket closed")

    socket = FailingSocket()
    with pytest.raises(aiohttp.ClientError):
        await runtime._send(socket, "stream.data", {"stream_id": STREAM_ID})
    assert len(runtime.outbox) == 0

    await runtime._send(socket, "heartbeat", {})
    assert len(runtime.outbox) == 1
    await runtime.egress.stop()


def test_node_policy_parses_the_network_block() -> None:
    policy = parse_policy(
        {
            "network": {
                "enabled": False,
                "allowed_destinations": ["*.internal"],
                "allowed_ports": [80, 9090, "bad"],
                "max_streams_per_node": 4,
            }
        }
    )
    assert policy.network.enabled is False
    assert policy.network.allowed_destinations == ["*.internal"]
    assert policy.network.allowed_ports == [80, 9090]
    assert policy.network.max_streams_per_node == 4


def test_legacy_destinations_map_into_network() -> None:
    policy = parse_policy({"allowed_destinations": ["10.0.0.0/8"]})
    assert policy.network.allowed_destinations == ["10.0.0.0/8"]


@pytest.mark.asyncio
async def test_custom_max_concurrency_limits_streams() -> None:
    server, port = await echo_server()
    policy = parse_policy(
        {"max_concurrency": 1, "network": {"allowed_destinations": ["127.0.0.1"]}}
    )
    captured: list[tuple[str, dict[str, Any]]] = []

    async def send(message_type: str, data: dict[str, Any]) -> None:
        captured.append((message_type, data))

    egress = EgressManager(send=send, policy_getter=lambda: policy)
    egress.configure({}, policy_version=1)
    frames = captured
    try:
        await egress.open(
            {"stream_id": "a" * 32, "host": "127.0.0.1", "port": port, "policy_version": 1}
        )
        await wait_for_frame(frames, "stream.opened")
        await egress.open(
            {"stream_id": "b" * 32, "host": "127.0.0.1", "port": port, "policy_version": 1}
        )
        error = await wait_for_frame(frames, "stream.error")
        assert error["code"] == "stream_limit"
        assert error["stream_id"] == "b" * 32
    finally:
        await egress.stop()
        server.close()
        await server.wait_closed()
