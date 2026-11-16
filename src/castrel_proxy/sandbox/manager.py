"""
Proxy sandbox manager

Owns the lifecycle of per-session sandbox containers on this proxy node:

* one container per chat session (never a shared pool — bind mounts are fixed
  at container creation time, so a per-session container is the correct unit);
* bootstrap (create or reuse), execute-class script runs, and teardown;
* a concurrency gate (``max_concurrent``) and idle-TTL reaping.

The manager is backend-agnostic: it composes a :class:`SandboxBackend`
(currently the docker/podman CLI driver) and a :class:`SessionWorkspace` per
session.  It performs no policy/whitelist decisions itself — those belong to
the server-side authorization layer and the host execution channel.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from .backends import SandboxBackend, build_backend, interpreter_for_language
from .config import SandboxConfig
from .detector import select_engine
from .models import BootstrapResult, ExecResult
from .workspace import SessionWorkspace

logger = logging.getLogger(__name__)

_LANG_EXT = {
    "python": "py",
    "python3": "py",
    "node": "js",
    "javascript": "js",
    "bash": "sh",
    "sh": "sh",
    "shell": "sh",
}


def _container_name(session_id: str) -> str:
    safe = re.sub(r"[^a-zA-Z0-9_.-]", "-", session_id)[:48]
    return f"castrel-sbx-{safe}"


@dataclass
class _SessionRecord:
    session_id: str
    container_id: str
    workspace: SessionWorkspace
    image: str
    user_skills_host_path: str = ""
    created_at: float = field(default_factory=time.monotonic)
    last_used: float = field(default_factory=time.monotonic)
    active_execs: int = 0


class SandboxError(Exception):
    """Raised for sandbox manager-level failures."""


class ProxySandboxManager:
    """Manages per-session sandbox containers for a proxy node."""

    def __init__(self, config: SandboxConfig, backend: Optional[SandboxBackend] = None):
        self.config = config
        self._backend = backend
        self._engine: Optional[str] = backend.name if backend else None
        self._sessions: Dict[str, _SessionRecord] = {}
        self._lock = asyncio.Lock()
        self._semaphore = asyncio.Semaphore(max(1, config.max_concurrent))
        self._reaper_task: Optional[asyncio.Task[None]] = None

    # -- backend resolution -------------------------------------------------
    def _ensure_backend(self) -> SandboxBackend:
        if self._backend is None:
            engine = select_engine(self.config)
            if engine is None:
                raise SandboxError("Docker not found or daemon not reachable")
            self._engine = engine
            self._backend = build_backend(self.config)
        return self._backend

    @property
    def engine(self) -> Optional[str]:
        return self._engine

    @property
    def active_executions(self) -> int:
        """Execs currently running inside sandboxes on this node."""
        return sum(record.active_execs for record in self._sessions.values())

    @property
    def session_count(self) -> int:
        """Sandbox sessions this node currently owns."""
        return len(self._sessions)

    async def reconcile(self) -> int:
        """Remove ``castrel-sbx-*`` containers left behind by a previous daemon run."""
        backend = self._ensure_backend()
        known = {record.container_id for record in self._sessions.values()}
        removed = 0
        for container_id in await backend.list_containers(prefix="castrel-sbx-"):
            if container_id in known:
                continue
            try:
                await backend.remove(container_id=container_id)
                removed += 1
            except Exception as exc:  # noqa: BLE001 - best-effort cleanup
                logger.warning(
                    "[SANDBOX-RECONCILE] container=%s error=%s", container_id[:12], exc
                )
        if removed:
            logger.info("[SANDBOX-RECONCILE] removed %d orphan container(s)", removed)
        return removed

    # -- startup preparation ------------------------------------------------
    async def prepare(self) -> Dict[str, Any]:
        """Warm up the sandbox subsystem at proxy startup.

        Resolves the container engine and pre-pulls the configured image so the
        first ``bootstrap`` does not pay the pull latency (and so a missing
        image surfaces as an early, actionable log rather than a failed tool
        call).  Best-effort: never raises, returns a status dict for logging.
        """
        status: Dict[str, Any] = {
            "enabled": self.config.enabled,
            "engine": None,
            "image": self.config.image,
            "image_pulled": False,
            "ready": False,
            "error": None,
        }
        if not self.config.enabled:
            return status
        try:
            backend = self._ensure_backend()
            status["engine"] = self._engine
            pulled = await backend.ensure_image(self.config.image)
            status["image_pulled"] = pulled
            status["ready"] = True
            status["orphans_removed"] = await self.reconcile()
            await self.start()
            logger.info(
                "[SANDBOX-PREPARE] engine=%s image=%s pulled=%s ready=True",
                self._engine,
                self.config.image,
                pulled,
            )
        except Exception as exc:
            status["error"] = str(exc)
            logger.warning(
                "[SANDBOX-PREPARE-WARN] Sandbox not ready at startup: image=%s error=%s",
                self.config.image,
                exc,
            )
        return status

    # -- lifecycle ----------------------------------------------------------
    async def bootstrap(
        self,
        session_id: str,
        *,
        image: Optional[str] = None,
        network: Optional[str] = None,
    ) -> BootstrapResult:
        """Create the session container (or return the existing one)."""
        if not session_id:
            raise SandboxError("session_id is required")
        backend = self._ensure_backend()
        image = image or self.config.image
        network = network or self.config.network
        user_skills_path = self.config.user_skills_dir_path
        try:
            if user_skills_path.exists() and not user_skills_path.is_dir():
                raise SandboxError(f"Configured skills path is not a directory: {user_skills_path}")
            user_skills_path.mkdir(parents=True, exist_ok=True)
            user_skills_host_path = str(user_skills_path.resolve())
        except OSError as exc:
            raise SandboxError(
                f"Unable to prepare configured skills directory: {user_skills_path}"
            ) from exc

        async with self._lock:
            existing = self._sessions.get(session_id)
            if existing and await backend.exists(container_id=existing.container_id):
                if existing.user_skills_host_path == user_skills_host_path:
                    existing.last_used = time.monotonic()
                    return BootstrapResult(
                        session_id=session_id,
                        container_id=existing.container_id,
                        image=existing.image,
                        engine=self._engine or backend.name,
                        workspace=str(existing.workspace.root),
                        reused=True,
                    )
                await backend.remove(container_id=existing.container_id)
            # Stale record: drop it before recreating.
            if existing:
                self._sessions.pop(session_id, None)

            if len(self._sessions) >= self.config.max_concurrent:
                await self._reap_idle_locked(backend)
            if len(self._sessions) >= self.config.max_concurrent:
                raise SandboxError(
                    f"Max concurrent sandboxes reached ({self.config.max_concurrent})"
                )

            workspace = SessionWorkspace(session_id, self.config.workspace_root_path).ensure()
            container_id = await backend.create(
                container_name=_container_name(session_id),
                workspace_host_path=str(workspace.root),
                image=image,
                resources=self.config.resources,
                network=network,
                user_skills_host_path=user_skills_host_path,
            )
            record = _SessionRecord(
                session_id=session_id,
                container_id=container_id,
                workspace=workspace,
                image=image,
                user_skills_host_path=user_skills_host_path,
            )
            self._sessions[session_id] = record
            await self.start()
            logger.info(
                "[SANDBOX-BOOTSTRAP] session=%s container=%s image=%s engine=%s",
                session_id,
                container_id[:12],
                image,
                self._engine,
            )
            return BootstrapResult(
                session_id=session_id,
                container_id=container_id,
                image=image,
                engine=self._engine or backend.name,
                workspace=str(workspace.root),
                reused=False,
            )

    async def execute_script(
        self,
        session_id: str,
        *,
        language: str,
        content: Optional[str] = None,
        path: Optional[str] = None,
        mode: str = "untrusted",
        timeout: Optional[float] = None,
        cancel_event: Optional[asyncio.Event] = None,
    ) -> ExecResult:
        """Run an execute-class script inside the session sandbox."""
        interpreter = interpreter_for_language(language)
        if interpreter is None:
            return ExecResult(
                session_id=session_id,
                exit_code=-1,
                stdout="",
                stderr="",
                execution_time=0.0,
                mode=mode,
                error=f"Unsupported language: {language!r}",
            )

        # Ensure the container exists (auto-bootstrap for convenience).
        record = self._sessions.get(session_id)
        if record is None or not await self._ensure_backend().exists(
            container_id=record.container_id
        ):
            await self.bootstrap(session_id)
            record = self._sessions[session_id]

        backend = self._ensure_backend()
        timeout = float(timeout or self.config.exec_timeout)

        if content is None and path is None:
            return ExecResult(
                session_id=session_id,
                exit_code=-1,
                stdout="",
                stderr="",
                execution_time=0.0,
                mode=mode,
                error="Either 'content' or 'path' is required",
            )

        # Acquire an execution lease so the idle reaper cannot remove a
        # container while a command is still running.
        async with self._lock:
            current = self._sessions.get(session_id)
            if current is not record:
                raise SandboxError(f"Sandbox session disappeared: {session_id}")
            record.active_execs += 1
            record.last_used = time.monotonic()

        try:
            # Materialize the script if inline content was supplied.
            if content is not None:
                ext = _LANG_EXT.get(language.strip().lower(), "txt")
                logical = f"/scripts/{uuid.uuid4().hex}.{ext}"
                record.workspace.write_script(logical, content)
            else:
                if path is None:
                    raise SandboxError("Either 'content' or 'path' is required")
                logical = path

            container_script_path = record.workspace.container_path(logical)
            argv = [*interpreter, container_script_path]

            async with self._semaphore:
                start = time.monotonic()
                code, stdout, stderr = await backend.exec(
                    container_id=record.container_id,
                    argv=argv,
                    timeout=timeout,
                    cancel_event=cancel_event,
                )
                elapsed = time.monotonic() - start
        finally:
            async with self._lock:
                if self._sessions.get(session_id) is record:
                    record.active_execs = max(0, record.active_execs - 1)
                    record.last_used = time.monotonic()

        artifacts = record.workspace.list_artifacts()
        logger.info(
            "[SANDBOX-EXEC] session=%s lang=%s exit=%s elapsed=%.2fs artifacts=%d",
            session_id,
            language,
            code,
            elapsed,
            len(artifacts),
        )
        return ExecResult(
            session_id=session_id,
            exit_code=code,
            stdout=stdout,
            stderr=stderr,
            execution_time=elapsed,
            mode=mode,
            artifacts=artifacts,
        )

    async def destroy(self, session_id: str) -> bool:
        """Tear down the session container. Returns True if one existed."""
        async with self._lock:
            record = self._sessions.pop(session_id, None)
        if record is None:
            return False
        try:
            await self._ensure_backend().remove(container_id=record.container_id)
        except Exception as exc:  # best-effort teardown
            logger.warning("[SANDBOX-DESTROY] session=%s error=%s", session_id, exc)
        logger.info(
            "[SANDBOX-DESTROY] session=%s container=%s", session_id, record.container_id[:12]
        )
        return True

    async def destroy_all(self) -> None:
        for session_id in list(self._sessions.keys()):
            await self.destroy(session_id)

    async def start(self) -> None:
        """Start the local idle reaper if sandbox management is enabled."""
        if not self.config.enabled:
            return
        if self._reaper_task and not self._reaper_task.done():
            return
        self._reaper_task = asyncio.create_task(
            self._reaper_loop(),
            name="castrel-sandbox-reaper",
        )

    async def shutdown(self) -> None:
        """Stop the idle reaper and remove all containers owned by this manager."""
        task = self._reaper_task
        self._reaper_task = None
        if task and not task.done():
            if task.get_loop() is asyncio.get_running_loop():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            elif task.get_loop().is_closed():
                logger.debug("[SANDBOX-SHUTDOWN] Reaper loop already closed")
            else:
                task.cancel()
        await self.destroy_all()

    async def _reaper_loop(self) -> None:
        interval = max(0.1, float(self.config.reaper_interval))
        while True:
            await asyncio.sleep(interval)
            try:
                await self.reap_idle()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("[SANDBOX-REAPER] Idle reaper iteration failed")

    def get_workspace(self, session_id: str) -> Optional["SessionWorkspace"]:
        """Return the SessionWorkspace for an active session, or None if not bootstrapped."""
        record = self._sessions.get(session_id)
        return record.workspace if record else None

    async def touch(self, session_id: str) -> bool:
        """Refresh the idle lease for an active session."""
        async with self._lock:
            record = self._sessions.get(session_id)
            if record is None:
                return False
            record.last_used = time.monotonic()
            return True

    async def reap_idle(self) -> int:
        """Remove containers idle longer than ``idle_timeout``. Returns count reaped."""
        async with self._lock:
            return await self._reap_idle_locked(self._ensure_backend())

    async def _reap_idle_locked(self, backend: SandboxBackend) -> int:
        now = time.monotonic()
        stale = [
            sid
            for sid, rec in self._sessions.items()
            if rec.active_execs == 0 and now - rec.last_used > self.config.idle_timeout
        ]
        for sid in stale:
            rec = self._sessions.pop(sid, None)
            if rec is None:
                continue
            try:
                await backend.remove(container_id=rec.container_id)
            except Exception as exc:
                logger.warning("[SANDBOX-REAP] session=%s error=%s", sid, exc)
        if stale:
            logger.info("[SANDBOX-REAP] reaped %d idle sandbox(es)", len(stale))
        return len(stale)
