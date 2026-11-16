"""
Sandbox backends
"""

from __future__ import annotations

import abc
import asyncio
import shlex
from typing import Any, Dict, List, Optional, Tuple

from .config import SandboxConfig, SandboxResources
from .workspace import CONTAINER_MOUNT

CONTAINER_USER_SKILLS_MOUNT = "/opt/castrel/skills/user"
SANDBOX_RUNTIME_USER = "1000:1000"

# Map an execute-class language to the in-container interpreter argv prefix.
LANGUAGE_INTERPRETERS = {
    "python": ["python3"],
    "python3": ["python3"],
    "node": ["node"],
    "javascript": ["node"],
    "bash": ["bash"],
    "sh": ["sh"],
    "shell": ["bash"],
}


class SandboxBackendError(Exception):
    """Raised when a backend operation fails."""


class SandboxBackend(abc.ABC):
    """Abstract lifecycle driver for per-session sandbox containers."""

    name: str = "abstract"

    @abc.abstractmethod
    async def create(
        self,
        *,
        container_name: str,
        workspace_host_path: str,
        image: str,
        resources: SandboxResources,
        network: str,
        user_skills_host_path: Optional[str] = None,
    ) -> str:
        """Create + start a container, returning its id."""

    @abc.abstractmethod
    async def exec(
        self,
        *,
        container_id: str,
        argv: List[str],
        timeout: float,
        workdir: str = CONTAINER_MOUNT,
        cancel_event: Optional[asyncio.Event] = None,
    ) -> Tuple[int, str, str]:
        """Execute a command inside the container, returning (exit_code, stdout, stderr).

        When ``cancel_event`` fires the backend kills the ``docker exec`` client; the process
        inside the container keeps running, which is a limit of the CLI execution model.
        """

    async def list_containers(self, *, prefix: str) -> List[str]:
        """Ids of containers whose name starts with ``prefix``; used to reap orphans."""
        return []

    @abc.abstractmethod
    async def remove(self, *, container_id: str) -> None:
        """Force-remove the container."""

    @abc.abstractmethod
    async def exists(self, *, container_id: str) -> bool:
        """Return True if the container still exists (running or stopped)."""

    async def ensure_image(self, image: str) -> bool:
        """Ensure ``image`` is present locally, pulling it if necessary.

        Returns True if a pull was performed, False if the image was already
        present.  Implementations should treat an already-present image as a
        no-op success even when the registry is unreachable.
        """
        raise NotImplementedError


async def _cli_ensure_image(engine: str, image: str) -> bool:
    """Shared docker/podman CLI image check + pull helper."""

    async def _run(args: List[str], timeout: float) -> Tuple[int, str, str]:
        try:
            proc = await asyncio.create_subprocess_exec(
                engine,
                *args,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            raise SandboxBackendError(f"Failed to spawn {engine}: {exc}") from exc
        try:
            out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return -1, "", f"{engine} {args[0] if args else ''} timed out after {timeout}s"
        return (
            proc.returncode if proc.returncode is not None else -1,
            out_b.decode("utf-8", errors="replace"),
            err_b.decode("utf-8", errors="replace"),
        )

    code, _out, _err = await _run(["image", "inspect", image], timeout=20.0)
    if code == 0:
        return False  # already present
    code, out, err = await _run(["pull", image], timeout=600.0)
    if code != 0:
        raise SandboxBackendError(f"Image pull failed for {image}: {err.strip() or out.strip()}")
    return True


class CliSandboxBackend(SandboxBackend):
    """Backend that drives Docker via its CLI."""

    def __init__(self):
        self.engine = "docker"
        self.name = "docker-cli"

    async def _run(
        self,
        args: List[str],
        timeout: float = 30.0,
        cancel_event: Optional[asyncio.Event] = None,
    ) -> Tuple[int, str, str]:
        try:
            proc = await asyncio.create_subprocess_exec(
                self.engine,
                *args,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            raise SandboxBackendError(f"Failed to spawn {self.engine}: {exc}") from exc

        try:
            out_b, err_b, cancelled = await asyncio.wait_for(
                self._communicate(proc, cancel_event), timeout=timeout
            )
        except asyncio.TimeoutError:
            proc.kill()
            try:
                await proc.wait()
            except Exception:  # noqa: BLE001 - the process may already be gone
                pass
            return -1, "", f"{self.engine} {args[0] if args else ''} timed out after {timeout}s"
        if cancelled:
            return -1, "", "cancelled by the platform"

        return (
            proc.returncode if proc.returncode is not None else -1,
            out_b.decode("utf-8", errors="replace"),
            err_b.decode("utf-8", errors="replace"),
        )

    @staticmethod
    async def _communicate(
        proc: asyncio.subprocess.Process,
        cancel_event: Optional[asyncio.Event],
    ) -> Tuple[bytes, bytes, bool]:
        """Wait for the process; return ``cancelled=True`` when the event fired first."""
        communicate = asyncio.ensure_future(proc.communicate())
        if cancel_event is None:
            out_b, err_b = await communicate
            return out_b, err_b, False
        canceller = asyncio.ensure_future(cancel_event.wait())
        try:
            done, _pending = await asyncio.wait(
                {communicate, canceller}, return_when=asyncio.FIRST_COMPLETED
            )
        finally:
            canceller.cancel()
        if communicate in done:
            out_b, err_b = communicate.result()
            return out_b, err_b, False
        proc.kill()
        try:
            await communicate
        except Exception:  # noqa: BLE001 - a killed client often ends with an error
            pass
        return b"", b"", True

    async def create(
        self,
        *,
        container_name: str,
        workspace_host_path: str,
        image: str,
        resources: SandboxResources,
        network: str,
        user_skills_host_path: Optional[str] = None,
    ) -> str:
        args: List[str] = [
            "run",
            "-d",
            "--name",
            container_name,
            "-v",
            f"{workspace_host_path}:{CONTAINER_MOUNT}",
            "-w",
            CONTAINER_MOUNT,
            "--network",
            network or "none",
            "--user",
            SANDBOX_RUNTIME_USER,
        ]
        if user_skills_host_path:
            args[1:1] = [
                "--mount",
                (
                    "type=bind,"
                    f"src={user_skills_host_path},"
                    f"dst={CONTAINER_USER_SKILLS_MOUNT},readonly"
                ),
            ]
        if resources.cpus:
            args += ["--cpus", str(resources.cpus)]
        if resources.memory:
            args += ["--memory", str(resources.memory)]
        if resources.pids_limit:
            args += ["--pids-limit", str(resources.pids_limit)]
        # Keep the container alive so we can exec repeatedly within the session.
        args += [image, "sleep", "infinity"]

        code, out, err = await self._run(args, timeout=60.0)
        if code != 0:
            raise SandboxBackendError(f"Container create failed: {err.strip() or out.strip()}")
        return out.strip()

    async def exec(
        self,
        *,
        container_id: str,
        argv: List[str],
        timeout: float,
        workdir: str = CONTAINER_MOUNT,
        cancel_event: Optional[asyncio.Event] = None,
    ) -> Tuple[int, str, str]:
        args = ["exec", "-w", workdir, container_id, *argv]
        return await self._run(args, timeout=timeout, cancel_event=cancel_event)

    async def list_containers(self, *, prefix: str) -> List[str]:
        code, out, _err = await self._run(
            ["ps", "-a", "--filter", f"name={prefix}", "--format", "{{.ID}}"], timeout=15.0
        )
        if code != 0:
            return []
        return [line.strip() for line in out.splitlines() if line.strip()]

    async def remove(self, *, container_id: str) -> None:
        await self._run(["rm", "-f", container_id], timeout=30.0)

    async def exists(self, *, container_id: str) -> bool:
        code, _out, _err = await self._run(["container", "inspect", container_id], timeout=15.0)
        return code == 0

    async def ensure_image(self, image: str) -> bool:
        return await _cli_ensure_image(self.engine, image)


class LlmSandboxBackend(SandboxBackend):
    """Backend driven by the ``llm-sandbox`` library (preferred).

    Holds one long-lived ``llm-sandbox`` session per container so scripts can be
    executed repeatedly against a persistent, bind-mounted workspace.  All
    blocking library calls are off-loaded to a thread so the event loop stays
    responsive.  Docker is the only supported engine.
    """

    def __init__(self):
        self.engine = "docker"
        self.name = "llm-sandbox:docker"
        # Map container id -> live llm-sandbox session object.
        self._sessions: Dict[str, Any] = {}

    def _backend_enum(self):
        from llm_sandbox import SandboxBackend as LSBackend

        return LSBackend.DOCKER

    @staticmethod
    def _runtime_configs(
        *,
        container_name: str,
        workspace_host_path: str,
        resources: SandboxResources,
        network: str,
        user_skills_host_path: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Build docker-SDK kwargs forwarded by llm-sandbox to container create."""
        configs: Dict[str, Any] = {
            "name": container_name,
            "network_mode": network or "none",
            "user": SANDBOX_RUNTIME_USER,
            "volumes": {workspace_host_path: {"bind": CONTAINER_MOUNT, "mode": "rw"}},
        }
        if user_skills_host_path:
            configs["volumes"][user_skills_host_path] = {
                "bind": CONTAINER_USER_SKILLS_MOUNT,
                "mode": "ro",
            }
        if resources.memory:
            configs["mem_limit"] = str(resources.memory)
        if resources.pids_limit:
            configs["pids_limit"] = int(resources.pids_limit)
        if resources.cpus:
            try:
                configs["nano_cpus"] = int(float(resources.cpus) * 1_000_000_000)
            except (TypeError, ValueError):
                pass
        return configs

    async def create(
        self,
        *,
        container_name: str,
        workspace_host_path: str,
        image: str,
        resources: SandboxResources,
        network: str,
        user_skills_host_path: Optional[str] = None,
    ) -> str:
        from llm_sandbox.session import create_session

        runtime_configs = self._runtime_configs(
            container_name=container_name,
            workspace_host_path=workspace_host_path,
            resources=resources,
            network=network,
            user_skills_host_path=user_skills_host_path,
        )

        def _open():
            session = create_session(
                backend=self._backend_enum(),
                image=image,
                lang="python",
                workdir=CONTAINER_MOUNT,
                keep_template=True,
                skip_environment_setup=True,
                runtime_configs=runtime_configs,
            )
            session.open()
            container = getattr(session, "container", None)
            container_id = getattr(container, "id", None) or container_name
            return session, container_id

        try:
            session, container_id = await asyncio.to_thread(_open)
        except Exception as exc:  # pragma: no cover - depends on engine/image
            raise SandboxBackendError(f"llm-sandbox container create failed: {exc}") from exc

        self._sessions[container_id] = session
        return container_id

    async def exec(
        self,
        *,
        container_id: str,
        argv: List[str],
        timeout: float,
        workdir: str = CONTAINER_MOUNT,
        cancel_event: Optional[asyncio.Event] = None,
    ) -> Tuple[int, str, str]:
        # The library owns the process handle, so cancellation cannot kill the client here;
        # the manager still tears the container down after the grace period.
        del cancel_event
        session = self._sessions.get(container_id)
        if session is None:
            return -1, "", f"No live sandbox session for container {container_id}"

        command = shlex.join(argv)

        def _exec():
            out = session.execute_command(command, workdir=workdir)
            return int(getattr(out, "exit_code", -1)), getattr(out, "stdout", ""), getattr(out, "stderr", "")

        try:
            return await asyncio.wait_for(asyncio.to_thread(_exec), timeout=timeout)
        except asyncio.TimeoutError:
            return -1, "", f"Sandbox command timed out after {timeout}s"
        except Exception as exc:  # pragma: no cover - depends on engine
            return -1, "", f"Sandbox exec failed: {exc}"

    async def remove(self, *, container_id: str) -> None:
        session = self._sessions.pop(container_id, None)
        if session is None:
            return

        def _close():
            try:
                session.close()
            except Exception:  # best-effort teardown
                pass

        await asyncio.to_thread(_close)

    async def exists(self, *, container_id: str) -> bool:
        return container_id in self._sessions

    async def ensure_image(self, image: str) -> bool:
        def _ensure() -> bool:
            import docker

            client = docker.from_env()
            try:
                client.images.get(image)
                return False  # already present
            except Exception:
                client.images.pull(image)
                return True

        try:
            return await asyncio.to_thread(_ensure)
        except Exception as exc:  # pragma: no cover - depends on registry
            # Fall back to the CLI so a present-but-SDK-unreachable image still works.
            try:
                return await _cli_ensure_image("docker", image)
            except Exception:
                raise SandboxBackendError(f"Image pull failed for {image}: {exc}") from exc


def interpreter_for_language(language: str) -> Optional[List[str]]:
    """Return the interpreter argv prefix for a language, or None if unknown."""
    return LANGUAGE_INTERPRETERS.get((language or "").strip().lower())


def _llm_sandbox_available() -> bool:
    try:
        import importlib.util

        return (
            importlib.util.find_spec("llm_sandbox") is not None
            and importlib.util.find_spec("docker") is not None
        )
    except Exception:  # pragma: no cover
        return False


def build_backend(_config: Optional[SandboxConfig] = None) -> SandboxBackend:
    """Factory: prefer the llm-sandbox driver, fall back to the Docker CLI driver."""
    if _llm_sandbox_available():
        try:
            return LlmSandboxBackend()
        except SandboxBackendError:
            pass
    return CliSandboxBackend()
