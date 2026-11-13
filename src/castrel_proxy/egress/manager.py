"""Node-side egress stream registry: policy, DNS validation and TCP connections."""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from typing import Any

from ..protocol.streams import (
    MAX_STREAM_CHUNK_BYTES,
    STREAM_CHUNK_BYTES,
    STREAM_IDLE_TIMEOUT_SECONDS,
    STREAM_INITIAL_WINDOW_BYTES,
    STREAM_MAX_BYTES,
    STREAM_MAX_STREAMS,
    decode_stream_data,
)
from ..security.policy import ProxyPolicy, check_connector
from .policy import (
    blocked_address,
    check_destination,
    check_stream_limits,
    resolve_destination,
)
from .stream import EgressStream

logger = logging.getLogger(__name__)

STREAM_ID = re.compile(r"^[0-9a-f]{32}$")

SendFrame = Callable[[str, dict[str, Any]], Awaitable[None]]
PolicyGetter = Callable[[], ProxyPolicy]


class EgressManager:
    def __init__(self, *, send: SendFrame, policy_getter: PolicyGetter) -> None:
        self._send = send
        self._policy_getter = policy_getter
        self.streams: dict[str, EgressStream] = {}
        self.chunk_bytes = STREAM_CHUNK_BYTES
        self.initial_window = STREAM_INITIAL_WINDOW_BYTES
        self.max_streams = STREAM_MAX_STREAMS
        self.idle_timeout = float(STREAM_IDLE_TIMEOUT_SECONDS)
        self.max_bytes = STREAM_MAX_BYTES
        self.policy_version = 1
        self._sweeper: asyncio.Task[None] | None = None

    def configure(
        self, network: dict[str, Any] | None, *, policy_version: int = 1
    ) -> None:
        document = network if isinstance(network, dict) else {}
        self.chunk_bytes = _clamped(
            document.get("chunk_bytes"), STREAM_CHUNK_BYTES, 1024, MAX_STREAM_CHUNK_BYTES
        )
        self.initial_window = _clamped(
            document.get("initial_window_bytes"),
            STREAM_INITIAL_WINDOW_BYTES,
            4096,
            8 * 1024 * 1024,
        )
        self.max_streams = _clamped(
            document.get("max_streams_per_node"), STREAM_MAX_STREAMS, 1, 1024
        )
        self.idle_timeout = float(
            _clamped(
                document.get("idle_timeout_seconds"),
                STREAM_IDLE_TIMEOUT_SECONDS,
                10,
                3600,
            )
        )
        self.max_bytes = _clamped(
            document.get("max_bytes_per_stream"),
            STREAM_MAX_BYTES,
            1024,
            4 * 1024 * 1024 * 1024,
        )
        self.policy_version = int(policy_version or self.policy_version)
        if self._sweeper is None or self._sweeper.done():
            self._sweeper = asyncio.create_task(
                self._sweep_loop(), name="egress-sweeper"
            )

    async def open(self, data: dict[str, Any]) -> None:
        stream_id = str(data.get("stream_id") or "")
        if not STREAM_ID.match(stream_id):
            logger.warning("Ignoring stream.open with an invalid id")
            return
        host = str(data.get("host") or "").strip()
        port = _int(data.get("port"))
        connector_id = str(data.get("connector_id") or "")
        instance_id = str(data.get("instance_id") or "")
        policy = self._policy_getter()
        if _int(data.get("policy_version")) < self.policy_version:
            await self._error(stream_id, "stream_denied", "Request carries a stale policy version.")
            return
        denial = check_connector(policy, connector_id) if connector_id else None
        if denial is not None:
            await self._error(stream_id, "stream_denied", denial)
            return
        if not host or port is None or not 1 <= port <= 65535:
            await self._error(stream_id, "stream_denied", "Invalid stream destination.")
            return
        denial = check_destination(policy, host, port)
        if denial is not None:
            await self._error(stream_id, "stream_denied", denial)
            return
        node_streams = len(self.streams)
        instance_streams = sum(
            1 for stream in self.streams.values() if stream.instance_id == instance_id
        )
        limits = check_stream_limits(policy, node_streams, instance_streams)
        if limits is not None:
            await self._error(stream_id, "stream_limit", limits)
            return
        node_limit = (
            policy.max_concurrency
            or policy.network.max_streams_per_node
            or self.max_streams
        )
        if node_streams >= node_limit:
            await self._error(
                stream_id,
                "stream_limit",
                "The node reached its configured concurrency limit.",
            )
            return

        try:
            addresses = await resolve_destination(host, port)
        except ValueError as error:
            await self._error(stream_id, "stream_dns_failed", str(error))
            return
        allowed = [address for address in addresses if not blocked_address(address)]
        if not allowed:
            await self._error(
                stream_id, "stream_denied", "The destination resolves to a blocked address."
            )
            return
        timeout = min(
            120.0,
            max(
                1.0,
                _int(data.get("connect_timeout_ms"), default=10_000) / 1000,
            ),
        )
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(allowed[0], port), timeout=timeout
            )
        except Exception as error:  # noqa: BLE001 - report connect failures to the platform
            await self._error(
                stream_id, "stream_connect_failed", f"Connection failed: {error}"
            )
            return

        network = policy.network
        stream = EgressStream(
            stream_id=stream_id,
            host=host,
            port=port,
            reader=reader,
            writer=writer,
            send=self._send,
            on_close=self._closed,
            chunk_bytes=self.chunk_bytes,
            initial_window=self.initial_window,
            idle_timeout=float(network.idle_timeout_seconds or self.idle_timeout),
            max_bytes=int(network.max_bytes_per_stream or self.max_bytes),
            connector_id=connector_id,
            instance_id=instance_id,
        )
        self.streams[stream_id] = stream
        self._configure_stream(stream, data)
        try:
            await self._send(
                "stream.opened",
                {
                    "stream_id": stream_id,
                    "resolved_ip": allowed[0],
                    "connect_ms": 0,
                },
            )
        except Exception:
            await stream.close("error", "stream_reset")

    def feed(self, data: dict[str, Any]) -> None:
        stream = self._stream(data)
        if stream is None:
            return
        try:
            payload = decode_stream_data(str(data.get("data") or ""))
        except ValueError:
            asyncio.create_task(stream.close("error", "stream_protocol_error"))
            return
        if not payload or len(payload) > self.chunk_bytes:
            asyncio.create_task(stream.close("error", "stream_protocol_error"))
            return
        if not stream.feed(payload):
            asyncio.create_task(stream.close("error", "stream_limit"))

    def window(self, data: dict[str, Any]) -> None:
        stream = self._stream(data)
        if stream is not None:
            stream.grant(_int(data.get("bytes"), default=0))

    async def close_stream(self, data: dict[str, Any]) -> None:
        stream = self._stream(data)
        if stream is not None:
            await stream.close(str(data.get("reason") or "closed"))

    async def close_all(self, reason: str = "reconnect") -> None:
        for stream in list(self.streams.values()):
            await stream.close(reason)

    async def stop(self) -> None:
        if self._sweeper is not None:
            self._sweeper.cancel()
            await asyncio.gather(self._sweeper, return_exceptions=True)
            self._sweeper = None
        await self.close_all("shutdown")

    async def sweep(self) -> None:
        for stream in list(self.streams.values()):
            if stream.idle():
                await stream.close("idle_timeout", "stream_timeout")

    def _stream(self, data: dict[str, Any]) -> EgressStream | None:
        return self.streams.get(str(data.get("stream_id") or ""))

    def _closed(self, stream: EgressStream, reason: str, error_code: str | None) -> None:
        self.streams.pop(stream.stream_id, None)
        logger.info(
            "Egress stream %s closed (%s, %s bytes in / %s out)",
            stream.stream_id,
            reason,
            stream.bytes_in,
            stream.bytes_out,
        )

    def _configure_stream(self, stream: EgressStream, data: dict[str, Any]) -> None:
        requested = _int(data.get("idle_timeout_ms"))
        if requested > 0:
            stream.idle_timeout = min(stream.idle_timeout, requested / 1000)
        max_bytes = _int(data.get("max_bytes"))
        if max_bytes > 0:
            stream.max_bytes = min(stream.max_bytes, max_bytes)

    async def _error(self, stream_id: str, code: str, message: str) -> None:
        try:
            await self._send(
                "stream.error",
                {"stream_id": stream_id, "code": code, "message": message},
            )
        except Exception:
            logger.debug("Failed to send stream error for %s", stream_id)

    async def _sweep_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(10)
                await self.sweep()
        except asyncio.CancelledError:
            raise


def _int(value: Any, *, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _clamped(value: Any, default: int, low: int, high: int) -> int:
    number = _int(value, default=default)
    return max(low, min(high, number))


__all__ = ["EgressManager"]
