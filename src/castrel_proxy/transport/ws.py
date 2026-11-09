"""Node WebSocket session: handshake, heartbeat, stream egress and shell execution.

One outbound WebSocket carries every proxy message; the server never connects to the node.
Authentication is a node token plus an Ed25519 proof over a server nonce. Connector traffic
is forwarded as raw TCP streams; connector code itself runs in the platform.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import aiohttp

from ..core.executor import CommandExecutor
from ..core.policy_state import save_policy_state
from ..egress import EgressManager
from ..pairing.challenge import sign_challenge
from ..pairing.keys import NodeKeys
from ..protocol.envelope import (
    EGRESS_CAPABILITY,
    PROTOCOL_VERSION,
    make_envelope,
    parse_envelope,
)
from ..security.policy import (
    check_exec,
    effective_timeout,
    parse_policy,
)
from ..security.whitelist import is_command_allowed
from .spool import Outbox

logger = logging.getLogger(__name__)

CLOSE_AUTH_FAILED = 4001
CLOSE_REVOKED = 4002
CLOSE_PROTOCOL_UNSUPPORTED = 4003
CLOSE_SUPERSEDED = 4009
STOP_CODES = frozenset(
    {CLOSE_AUTH_FAILED, CLOSE_REVOKED, CLOSE_PROTOCOL_UNSUPPORTED, CLOSE_SUPERSEDED}
)
MAX_EXEC_OUTPUT_BYTES = 262_144
# Frames handled on the read loop itself; everything else is dispatched to a task.
_INLINE_FRAME_TYPES = frozenset(
    {
        "heartbeat",
        "policy.update",
        "token.rotate",
        "stream.data",
        "stream.window",
        "stream.close",
    }
)


class DispatchError(Exception):
    def __init__(self, message: str, code: str = "dispatch_failed") -> None:
        super().__init__(message)
        self.code = code


class ProxyNodeRuntime:
    """Connect outbound, serve stream and shell messages, and reconnect with backoff."""

    def __init__(
        self,
        *,
        server_url: str,
        keys: NodeKeys,
        keys_path: Path | None = None,
        policy_state_path: Path | None = None,
        outbox: Outbox | None = None,
        max_backoff: float = 60.0,
    ) -> None:
        self.server_url = server_url.rstrip("/")
        self.keys = keys
        self.keys_path = keys_path
        self.policy_state_path = policy_state_path
        self.outbox = outbox
        self.max_backoff = max_backoff
        self.epoch = 0
        self.network: dict[str, Any] = {}
        self.policy: dict[str, Any] = {}
        self.policy_version = 1
        self._policy = parse_policy({})
        self._reconnect = True
        self._tasks: set[asyncio.Task[None]] = set()
        self._send_lock = asyncio.Lock()
        # Cancellation hooks for running requests, keyed by request_id.
        self._inflight: dict[str, Callable[[], Any]] = {}
        self._active_ws: aiohttp.ClientWebSocketResponse | None = None
        self.egress = EgressManager(
            send=self._send_stream_frame,
            policy_getter=lambda: self._policy,
        )

    async def _send_stream_frame(self, message_type: str, data: dict[str, Any]) -> None:
        ws = self._active_ws
        if ws is None:
            raise ConnectionError("No active proxy WebSocket")
        await self._send(ws, message_type, data)

    def ws_url(self) -> str:
        base = self.server_url.replace("https://", "wss://").replace("http://", "ws://")
        return f"{base}/api/v1/proxies/ws"

    async def run(self) -> None:
        backoff = 1.0
        while self._reconnect:
            try:
                await self._session()
                backoff = 1.0
            except asyncio.CancelledError:
                raise
            except _StopReconnect as error:
                logger.error("Proxy node stopped: %s", error)
                return
            except Exception as error:  # noqa: BLE001 - reconnect unless the server says stop
                logger.warning("Proxy node connection failed: %s", error)
            if not self._reconnect:
                return
            delay = min(self.max_backoff, backoff) * (0.5 + random.random())
            await asyncio.sleep(delay)
            backoff = min(self.max_backoff, backoff * 2)

    def stop(self) -> None:
        self._reconnect = False
        for task in list(self._tasks):
            task.cancel()
        task = asyncio.create_task(self.egress.stop())
        task.add_done_callback(self._tasks.discard)
        self._tasks.add(task)

    async def _session(self) -> None:
        headers = {
            "Authorization": f"Bearer {self.keys.token}",
            "X-Castrel-Node-Id": self.keys.node_id,
            "X-Castrel-Protocol": str(PROTOCOL_VERSION),
        }
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(self.ws_url(), headers=headers, heartbeat=None) as ws:
                self._active_ws = ws
                try:
                    await self._handshake(ws)
                    self.egress.configure(self.network, policy_version=self.policy_version)
                    await self._drain_outbox(ws)
                    async for message in ws:
                        if message.type != aiohttp.WSMsgType.TEXT:
                            continue
                        await self._dispatch(parse_envelope(message.data), ws)
                    code = ws.close_code
                    if code in STOP_CODES:
                        raise _StopReconnect(f"server closed the connection with {code}")
                finally:
                    await self.egress.close_all("reconnect")
                    self._active_ws = None

    async def _dispatch(self, frame: dict[str, Any], ws: aiohttp.ClientWebSocketResponse) -> None:
        """Handle one frame without stalling the read loop."""
        if frame["type"] in _INLINE_FRAME_TYPES:
            await self._handle(frame, ws)
            return
        task = asyncio.create_task(self._handle(frame, ws))
        self._tasks.add(task)
        task.add_done_callback(self._frame_task_done)

    def _frame_task_done(self, task: asyncio.Task[None]) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            logger.warning("Proxy frame handler failed: %s", task.exception())

    async def _handshake(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        challenge = await asyncio.wait_for(ws.receive(), timeout=10)
        if challenge.type != aiohttp.WSMsgType.TEXT:
            raise DispatchError("server did not send auth.challenge", "handshake_failed")
        frame = parse_envelope(challenge.data)
        if frame["type"] != "auth.challenge":
            raise DispatchError("unexpected handshake message", "handshake_failed")
        data = frame["data"]
        signature = sign_challenge(
            self.keys.ed25519_private, self.keys.node_id, str(data["nonce"]), int(data["epoch"])
        )
        await ws.send_json(
            make_envelope(
                "auth.proof",
                {
                    "signature": signature,
                    "agent_version": _agent_version(),
                    "platform": _platform(),
                    "python_version": _python_version(),
                    "protocol_version": PROTOCOL_VERSION,
                    "capabilities": [EGRESS_CAPABILITY],
                },
                node_id=self.keys.node_id,
            )
        )
        ready = parse_envelope((await asyncio.wait_for(ws.receive(), timeout=10)).data)
        if ready["type"] != "session.ready":
            code = ready["data"].get("code") if isinstance(ready.get("data"), dict) else None
            raise DispatchError(f"handshake rejected ({code})", "handshake_rejected")
        self.epoch = int(ready["data"]["epoch"])
        self.network = dict(ready["data"].get("network") or {})
        self.policy = dict(ready["data"].get("policy") or {})
        self.policy_version = int(ready["data"].get("policy_version") or 1)
        self._policy = parse_policy(self.policy)
        self._remember_policy()

    def _remember_policy(self) -> None:
        """Snapshot the received policy so `proxy status` can show its limits."""
        save_policy_state(
            self.policy_state_path, version=self.policy_version, document=self.policy
        )

    async def _handle(self, frame: dict[str, Any], ws: aiohttp.ClientWebSocketResponse) -> None:
        message_type = frame["type"]
        data = frame.get("data") or {}
        if message_type == "heartbeat":
            await self._send(ws, "heartbeat", {"active_executions": 0, "load": 0.0})
            return
        if message_type == "policy.update":
            self.policy = dict(data.get("policy") or {})
            self.policy_version = int(data.get("version") or self.policy_version)
            self._policy = parse_policy(self.policy)
            self.egress.policy_version = self.policy_version
            self._remember_policy()
            return
        if message_type == "token.rotate":
            self.keys.token = str(data.get("token") or self.keys.token)
            if self.keys_path is not None:
                self.keys.save(self.keys_path)
            await self._send(ws, "token.rotate_ack", {"token_version": data.get("token_version")})
            return
        if message_type == "stream.open":
            await self.egress.open(data)
            return
        if message_type == "stream.data":
            self.egress.feed(data)
            return
        if message_type == "stream.window":
            self.egress.window(data)
            return
        if message_type == "stream.close":
            await self.egress.close_stream(data)
            return
        if message_type == "node.cancel":
            await self._cancel(data)
            return
        if message_type == "node.exec":
            await self._exec(data, ws)
            return

    async def _cancel(self, data: dict[str, Any]) -> None:
        """Stop the request the platform gave up on (timeout or caller cancellation)."""
        request_id = str(data.get("request_id") or "")
        cancel = self._inflight.pop(request_id, None)
        if cancel is None:
            logger.info("Cancellation for unknown request %s", request_id)
            return
        outcome = cancel()
        if asyncio.iscoroutine(outcome):
            await outcome
        logger.warning(
            "Cancelled proxy request %s (%s)",
            request_id,
            data.get("reason") or "cancelled",
        )

    async def _exec(self, data: dict[str, Any], ws: aiohttp.ClientWebSocketResponse) -> None:
        request_id = str(data.get("request_id") or "")
        command = str(data.get("command") or "")
        denial = check_exec(self._policy, command)
        if denial is not None:
            await self._exec_result(ws, request_id, 126, "", denial)
            return
        exec_policy = self._policy.exec
        if exec_policy.mode == "allowlist":
            override = set(exec_policy.allowlist) or None
            allowed, blocked = is_command_allowed(command, override)
            if not allowed:
                await self._exec_result(
                    ws,
                    request_id,
                    126,
                    "",
                    "Command is not allowed by the node whitelist: " + ", ".join(blocked),
                )
                return
        requested = max(1.0, float(data.get("timeout_ms") or 120_000) / 1000)
        timeout = min(
            effective_timeout(self._policy, requested, 3600.0),
            exec_policy.max_timeout_seconds,
        )
        started = time.monotonic()
        executor = CommandExecutor(
            session_id=str(data.get("session_id") or "proxy-exec"), timeout=timeout
        )
        self._inflight[request_id] = executor.cancel
        try:
            result = await executor.execute(command, cwd=data.get("cwd"))
        except asyncio.CancelledError:
            executor.cancel()
            raise
        finally:
            self._inflight.pop(request_id, None)
        stdout, stdout_cut = _cap_output(result.stdout, exec_policy.max_output_bytes)
        stderr, stderr_cut = _cap_output(result.stderr, exec_policy.max_output_bytes)
        await self._send(
            ws,
            "node.exec.result",
            {
                "request_id": request_id,
                "exit_code": result.exit_code,
                "stdout": stdout,
                "stderr": stderr,
                "duration_ms": int((time.monotonic() - started) * 1000),
                "truncated": stdout_cut or stderr_cut,
            },
        )

    async def _exec_result(
        self,
        ws: aiohttp.ClientWebSocketResponse,
        request_id: str,
        exit_code: int,
        stdout: str,
        stderr: str,
    ) -> None:
        await self._send(
            ws,
            "node.exec.result",
            {
                "request_id": request_id,
                "exit_code": exit_code,
                "stdout": stdout,
                "stderr": stderr,
                "duration_ms": 0,
                "truncated": False,
            },
        )

    async def _send(
        self, ws: aiohttp.ClientWebSocketResponse, message_type: str, data: dict[str, Any]
    ) -> None:
        frame = make_envelope(message_type, data, node_id=self.keys.node_id, epoch=self.epoch)
        # Stream bytes are never replayed: a stale chunk would corrupt the target connection.
        spool = not message_type.startswith("stream.")
        try:
            async with self._send_lock:
                await ws.send_json(frame)
        except (aiohttp.ClientError, ConnectionResetError):
            if self.outbox is not None and spool:
                self.outbox.append(frame)
                logger.warning("Proxy socket unavailable; frame spooled to the outbox")
            else:
                raise

    async def _drain_outbox(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        if self.outbox is None:
            return
        for frame in self.outbox.drain():
            await ws.send_json(frame)


class _StopReconnect(Exception):
    """The server closed the socket with a code that must not be retried."""


def _platform() -> str:
    import platform

    system = platform.system().lower()
    machine = platform.machine().lower()
    if system == "darwin":
        return "macos-arm64" if machine in ("arm64", "aarch64") else "macos-x86_64"
    return "linux-arm64" if machine in ("aarch64", "arm64") else "linux-x86_64"


def _cap_output(text: str, limit: int = MAX_EXEC_OUTPUT_BYTES) -> tuple[str, bool]:
    encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return text, False
    return (
        encoded[:limit].decode("utf-8", errors="replace") + "\n...[output truncated by Castrel]\n",
        True,
    )


def _agent_version() -> str:
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version("castrel-proxy")
    except PackageNotFoundError:
        return "0.0.0"


def _python_version() -> str:
    import platform

    version = platform.python_version_tuple()
    return f"{version[0]}.{version[1]}.{version[2]}"


__all__ = ["ProxyNodeRuntime"]
