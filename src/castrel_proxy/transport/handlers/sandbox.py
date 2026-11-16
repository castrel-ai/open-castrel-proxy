"""``sandbox.docker.v1`` handlers: per-session containers, script exec and reads.

The handler owns no container logic of its own: it validates the node policy, maps request
fields onto :class:`ProxySandboxManager`, and turns manager failures into enum error codes.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import time
from collections.abc import Callable
from typing import Any

from ...sandbox.config import SandboxConfig
from ...sandbox.detector import detect
from ...sandbox.manager import ProxySandboxManager, SandboxError
from ...sandbox.workspace import CONTAINER_MOUNT
from ...security.policy import ProxyPolicy, SandboxPolicy

# A cancelled exec keeps running inside the container (the CLI model cannot signal it); after
# this grace period the session container is removed so the script cannot continue.
CANCEL_GRACE_SECONDS = 5.0


class SandboxRequestError(Exception):
    """A request the handler answers with an error code instead of a result payload."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def default_sandbox_config() -> SandboxConfig:
    """Sandbox settings from ``~/.castrel/config.yaml``; defaults when unreadable."""
    try:
        from ...core.config import Config

        return Config().get_sandbox_config()
    except Exception:  # noqa: BLE001 - a missing config must not break the handshake
        return SandboxConfig()


class SandboxHandler:
    """Serve ``node.sandbox.*`` against the node's container manager."""

    def __init__(
        self,
        policy_getter: Callable[[], ProxyPolicy],
        config_factory: Callable[[], SandboxConfig] | None = None,
    ) -> None:
        self._policy_getter = policy_getter
        self._config_factory = config_factory or default_sandbox_config
        self._manager: ProxySandboxManager | None = None
        # request id → (session id, cancel event) for the execs currently running.
        self._requests: dict[str, tuple[str, asyncio.Event]] = {}

    @property
    def policy(self) -> SandboxPolicy:
        return self._policy_getter().sandbox

    @property
    def manager(self) -> ProxySandboxManager:
        if self._manager is None:
            self._manager = ProxySandboxManager(self._config_factory())
        return self._manager

    @property
    def active_executions(self) -> int:
        return self._manager.active_executions if self._manager is not None else 0

    def capability_block(self) -> dict[str, Any]:
        """The ``auth.proof`` sandbox block: availability, engine and configured image."""
        config = self.manager.config
        capability = detect(config)
        return {
            "available": capability.available,
            "engine": capability.engine or "",
            "engine_version": capability.engine_version or "",
            "image": capability.image or config.image,
            "image_present": capability.image_present,
            "reason": capability.reason,
            "max_timeout_seconds": self.policy.max_timeout_seconds,
        }

    async def shutdown(self) -> None:
        """Stop the reaper and remove containers owned by this node."""
        if self._manager is not None:
            await self._manager.shutdown()

    async def handle(self, message_type: str, data: dict[str, Any]) -> dict[str, Any]:
        request_id = str(data.get("request_id") or "")
        started = time.monotonic()
        try:
            if not self.policy.enabled:
                raise SandboxRequestError(
                    "sandbox_unavailable", "Sandbox is disabled by the node policy."
                )
            if message_type == "node.sandbox.bootstrap":
                payload = await self._bootstrap(data)
            elif message_type == "node.sandbox.exec":
                payload = await self._exec(request_id, data)
            elif message_type == "node.sandbox.read_file":
                payload = await asyncio.to_thread(self._read_file, data)
            elif message_type == "node.sandbox.destroy":
                payload = await self._destroy(data)
            elif message_type == "node.sandbox.status":
                payload = await asyncio.to_thread(self._status)
            else:
                raise SandboxRequestError(
                    "unknown_message_type", f"Unsupported message type: {message_type}"
                )
        except SandboxRequestError as error:
            return _failure(request_id, error.code, str(error), started)
        except SandboxError as error:
            return _failure(request_id, _sandbox_code(str(error)), str(error), started)
        except OSError as error:
            return _failure(request_id, "io_error", f"Sandbox I/O error: {error}", started)
        return {
            "request_id": request_id,
            "ok": True,
            "duration_ms": _elapsed(started),
            **payload,
        }

    async def _bootstrap(self, data: dict[str, Any]) -> dict[str, Any]:
        session_id = _required_session(data)
        policy = self.policy
        image = str(data.get("image") or "") or None
        network = str(data.get("network") or "") or None
        if image is not None:
            if not policy.allow_caller_image:
                raise SandboxRequestError(
                    "policy_denied", "Caller-provided images are not allowed by the node policy."
                )
            if image not in policy.allowed_images:
                raise SandboxRequestError("policy_denied", f"Image '{image}' is not allowed.")
        if network is not None and not policy.allow_caller_network:
            raise SandboxRequestError(
                "policy_denied", "Caller-provided networks are not allowed by the node policy."
            )
        _require_runtime(self.manager)
        result = await self.manager.bootstrap(session_id, image=image, network=network)
        return {
            "session_id": result.session_id,
            "container_id": result.container_id,
            "image": result.image,
            "engine": result.engine,
            "workspace": result.workspace,
            "reused": bool(result.reused),
        }

    async def _exec(self, request_id: str, data: dict[str, Any]) -> dict[str, Any]:
        session_id = _required_session(data)
        policy = self.policy
        timeout_ms = data.get("timeout_ms")
        try:
            requested = max(1.0, float(timeout_ms) / 1000) if timeout_ms else None
        except (TypeError, ValueError) as error:
            raise SandboxRequestError("invalid_request", "timeout_ms must be a number.") from error
        timeout = min(requested or policy.max_timeout_seconds, policy.max_timeout_seconds)
        _require_runtime(self.manager)
        event = asyncio.Event()
        self._requests[request_id] = (session_id, event)
        try:
            result = await self.manager.execute_script(
                session_id,
                language=str(data.get("language") or "python"),
                content=data.get("content"),
                path=data.get("path"),
                mode=str(data.get("mode") or "untrusted"),
                timeout=timeout,
                cancel_event=event,
            )
        finally:
            self._requests.pop(request_id, None)
        if result.error:
            raise SandboxRequestError("invalid_request", result.error)
        limit = self.manager.config.max_output_bytes
        stdout, out_cut = _cap(result.stdout, limit)
        stderr, err_cut = _cap(result.stderr, limit)
        return {
            "session_id": session_id,
            "exit_code": result.exit_code,
            "stdout": stdout,
            "stderr": stderr,
            "truncated": out_cut or err_cut,
            "artifacts": [
                {
                    "path": artifact.path,
                    "size": artifact.size,
                    "modified_at": int(artifact.modified_at * 1000),
                }
                for artifact in result.artifacts
            ],
        }

    def _read_file(self, data: dict[str, Any]) -> dict[str, Any]:
        session_id = _required_session(data)
        workspace = self.manager.get_workspace(session_id)
        if workspace is None:
            raise SandboxRequestError("sandbox_unavailable", "Sandbox session is not running.")
        container_path = str(data.get("path") or "")
        if container_path.startswith("/") and not container_path.startswith(CONTAINER_MOUNT):
            raise SandboxRequestError(
                "policy_denied", "Only paths under /workspace are readable."
            )
        try:
            host_path = workspace.resolve_host(container_path)
        except Exception as error:  # noqa: BLE001 - WorkspaceError is a plain exception
            raise SandboxRequestError(
                "policy_denied", f"Path escapes the sandbox workspace: {error}"
            ) from error
        if not host_path.exists():
            raise SandboxRequestError("not_found", "File does not exist in the sandbox.")
        if host_path.is_dir():
            raise SandboxRequestError("is_a_directory", "Path is a directory, not a file.")
        size = host_path.stat().st_size
        offset = _non_negative_int(data.get("offset"))
        max_bytes = max(1, min(_non_negative_int(data.get("max_bytes")) or 262_144, 4 * 1024 * 1024))
        if offset > size:
            raise SandboxRequestError("invalid_request", "Offset is past the end of the file.")
        with host_path.open("rb") as handle:
            handle.seek(offset)
            chunk = handle.read(max_bytes)
        digest = hashlib.sha256()
        with host_path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        next_offset = offset + len(chunk)
        return {
            "session_id": session_id,
            "path": container_path,
            "size_bytes": size,
            "offset": offset,
            "next_offset": next_offset,
            "eof": next_offset >= size,
            "content_base64": base64.b64encode(chunk).decode("ascii"),
            "sha256": digest.hexdigest(),
        }

    async def _destroy(self, data: dict[str, Any]) -> dict[str, Any]:
        session_id = _required_session(data)
        workspace = self.manager.get_workspace(session_id)
        if workspace is not None and workspace.session_id != session_id:
            raise SandboxRequestError("policy_denied", "Session does not belong to this node.")
        destroyed = await self.manager.destroy(session_id)
        return {"session_id": session_id, "destroyed": bool(destroyed)}

    def _status(self) -> dict[str, Any]:
        config = self.manager.config
        capability = detect(config)
        return {
            "available": capability.available,
            "engine": capability.engine or "",
            "image": capability.image or config.image,
            "image_present": capability.image_present,
            "active_sessions": self.manager.session_count,
            "reason": capability.reason,
        }

    def cancel(self, request_id: str) -> None:
        """Stop a running script: signal the exec client, then reap the container."""
        entry = self._requests.get(request_id)
        if entry is None:
            return
        session_id, event = entry
        event.set()
        asyncio.create_task(self._destroy_after_grace(session_id, request_id))

    async def _destroy_after_grace(self, session_id: str, request_id: str) -> None:
        await asyncio.sleep(CANCEL_GRACE_SECONDS)
        if self._requests.pop(request_id, None) is None:
            return
        try:
            await self.manager.destroy(session_id)
        except Exception:  # noqa: BLE001 - best effort during cancellation
            pass


def _required_session(data: dict[str, Any]) -> str:
    session_id = str(data.get("session_id") or "").strip()
    if not session_id:
        raise SandboxRequestError("invalid_request", "session_id is required.")
    return session_id


def _non_negative_int(value: Any) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, number)


def _cap(text: str, limit: int) -> tuple[str, bool]:
    encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return text, False
    truncated = encoded[:limit].decode("utf-8", errors="replace")
    return truncated + "\n...[output truncated by Castrel]\n", True


def _require_runtime(manager: ProxySandboxManager) -> None:
    """Fail before touching Docker when the engine or the image is unusable."""
    capability = detect(manager.config)
    if not capability.available:
        raise SandboxRequestError(
            "sandbox_unavailable", capability.reason or "Sandbox is unavailable on this node."
        )
    if not manager.config.pull_image and not capability.image_present:
        raise SandboxRequestError(
            "sandbox_image_missing",
            f"Image '{manager.config.image}' is not present on the node.",
        )


def _sandbox_code(message: str) -> str:
    lowered = message.lower()
    if "max concurrent" in lowered:
        return "sandbox_limit"
    if "image" in lowered:
        return "sandbox_image_missing"
    return "sandbox_unavailable"


def _failure(request_id: str, code: str, message: str, started: float) -> dict[str, Any]:
    return {
        "request_id": request_id,
        "ok": False,
        "error_code": code,
        "error": message,
        "duration_ms": _elapsed(started),
    }


def _elapsed(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


__all__ = ["CANCEL_GRACE_SECONDS", "SandboxHandler", "default_sandbox_config"]
