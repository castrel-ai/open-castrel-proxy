"""One TCP connection between the node's network and a platform egress stream."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from ..protocol.streams import STREAM_INITIAL_WINDOW_BYTES, encode_stream_data

logger = logging.getLogger(__name__)

SendFrame = Callable[[str, dict[str, Any]], Awaitable[None]]
OnClose = Callable[["EgressStream", str, str | None], Any]


class EgressStream:
    """Pumps bytes between the target socket and ``stream.*`` frames.

    Outbound (node -> platform) reads only while the platform granted credit. Inbound
    (platform -> node) lands in a bounded queue and is drained into the target socket, which
    also grants the platform more credit.
    """

    def __init__(
        self,
        *,
        stream_id: str,
        host: str,
        port: int,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        send: SendFrame,
        on_close: OnClose,
        chunk_bytes: int = 32 * 1024,
        initial_window: int = STREAM_INITIAL_WINDOW_BYTES,
        idle_timeout: float = 300.0,
        max_bytes: int = 64 * 1024 * 1024,
        connector_id: str = "",
        instance_id: str = "",
    ) -> None:
        self.stream_id = stream_id
        self.host = host
        self.port = port
        self.chunk_bytes = chunk_bytes
        self.initial_window = initial_window
        self.idle_timeout = idle_timeout
        self.max_bytes = max_bytes
        self.connector_id = connector_id
        self.instance_id = instance_id
        self.started_at = time.monotonic()
        self.last_activity = self.started_at
        self.bytes_in = 0
        self.bytes_out = 0
        self.closed = False
        self.error_code: str | None = None
        self._reader = reader
        self._writer = writer
        self._send = send
        self._on_close = on_close
        self._credit = initial_window
        self._credit_ready = asyncio.Event()
        self._credit_ready.set()
        self._out_seq = 0
        self._consumed = 0
        buffer_chunks = max(4, (initial_window // chunk_bytes) * 2)
        self._inbound: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=buffer_chunks)
        self._tasks = [
            asyncio.create_task(self._read_target(), name=f"egress-read-{stream_id[:8]}"),
            asyncio.create_task(self._write_target(), name=f"egress-write-{stream_id[:8]}"),
        ]

    def snapshot(self) -> dict[str, Any]:
        return {
            "stream_id": self.stream_id,
            "host": self.host,
            "port": self.port,
            "sent_bytes": self.bytes_in,
            "received_bytes": self.bytes_out,
            "duration_ms": int((time.monotonic() - self.started_at) * 1000),
            "connector_id": self.connector_id,
            "instance_id": self.instance_id,
            "error_code": self.error_code,
        }

    async def _read_target(self) -> None:
        try:
            while not self.closed:
                await self._acquire_credit()
                payload = await self._reader.read(self.chunk_bytes)
                if not payload:
                    await self.close("eof")
                    return
                self._credit -= len(payload)
                self.bytes_out += len(payload)
                self.last_activity = time.monotonic()
                await self._send(
                    "stream.data",
                    {
                        "stream_id": self.stream_id,
                        "seq": self._out_seq,
                        "data": encode_stream_data(payload),
                    },
                )
                self._out_seq += 1
                if self.bytes_in + self.bytes_out > self.max_bytes:
                    await self.close("too_large", "stream_too_large")
                    return
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 - any transport failure ends the stream
            logger.info("Egress stream %s read failed: %s", self.stream_id, error)
            await self.close("error", "stream_reset")

    async def _acquire_credit(self) -> None:
        while self._credit < self.chunk_bytes and not self.closed:
            self._credit_ready.clear()
            await self._credit_ready.wait()

    async def _write_target(self) -> None:
        try:
            while True:
                payload = await self._inbound.get()
                if payload is None:
                    return
                self._writer.write(payload)
                await self._writer.drain()
                self.bytes_in += len(payload)
                self._consumed += len(payload)
                self.last_activity = time.monotonic()
                if self._consumed >= max(1, self.initial_window // 2):
                    granted = self._consumed
                    self._consumed = 0
                    await self._send(
                        "stream.window",
                        {"stream_id": self.stream_id, "bytes": granted},
                    )
                if self.bytes_in + self.bytes_out > self.max_bytes:
                    await self.close("too_large", "stream_too_large")
                    return
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 - target write failures end the stream
            logger.info("Egress stream %s write failed: %s", self.stream_id, error)
            await self.close("error", "stream_reset")

    def feed(self, payload: bytes) -> bool:
        if self.closed:
            return False
        try:
            self._inbound.put_nowait(payload)
            return True
        except asyncio.QueueFull:
            return False

    def grant(self, credit: int) -> None:
        if self.closed or credit <= 0:
            return
        self._credit = min(self.initial_window * 2, self._credit + int(credit))
        self._credit_ready.set()

    def idle(self, now: float | None = None) -> bool:
        return (now or time.monotonic()) - self.last_activity >= self.idle_timeout

    async def close(self, reason: str, error_code: str | None = None) -> None:
        if self.closed:
            return
        self.closed = True
        if error_code is not None:
            self.error_code = error_code
        self._credit_ready.set()
        try:
            self._inbound.put_nowait(None)
        except asyncio.QueueFull:
            pass
        current = asyncio.current_task()
        for task in self._tasks:
            if task is not current:
                task.cancel()
        pending = [task for task in self._tasks if task is not current]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        try:
            self._writer.close()
            await self._writer.wait_closed()
        except Exception:
            pass
        message_type = "stream.error" if error_code else "stream.close"
        data = (
            {"stream_id": self.stream_id, "code": error_code, "message": reason}
            if error_code
            else {
                "stream_id": self.stream_id,
                "reason": reason[:200],
                "bytes_in": self.bytes_in,
                "bytes_out": self.bytes_out,
            }
        )
        try:
            await self._send(message_type, data)
        except Exception:
            logger.debug("Failed to send stream close for %s", self.stream_id)
        try:
            self._on_close(self, reason, error_code)
        except Exception:
            logger.exception("Egress stream close hook failed")


__all__ = ["EgressStream"]
