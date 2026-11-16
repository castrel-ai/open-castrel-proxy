"""`sandbox.docker.v1` handlers with a fake manager (no Docker required)."""

import asyncio
from pathlib import Path

import pytest
from castrel_proxy.sandbox.config import SandboxConfig
from castrel_proxy.sandbox.models import BootstrapResult, ExecResult, SandboxArtifact, SandboxCapability
from castrel_proxy.sandbox.workspace import SessionWorkspace
from castrel_proxy.security.policy import parse_policy
from castrel_proxy.transport import handlers
from castrel_proxy.transport.handlers import sandbox as sandbox_module
from castrel_proxy.transport.handlers.sandbox import SandboxHandler

SESSION = "proxy-sandbox-2001-s1"


class FakeManager:
    def __init__(self, workspace: SessionWorkspace, config: SandboxConfig) -> None:
        self.config = config
        self.workspace = workspace
        self.bootstrap_calls: list[dict] = []
        self.exec_calls: list[dict] = []
        self.destroyed: list[str] = []
        self.events: list[asyncio.Event] = []
        self.active_executions = 0
        self.session_count = 1

    async def bootstrap(self, session_id, *, image=None, network=None):
        self.bootstrap_calls.append({"session_id": session_id, "image": image, "network": network})
        return BootstrapResult(
            session_id=session_id,
            container_id="cid-1",
            image=image or self.config.image,
            engine="docker",
            workspace=str(self.workspace.root),
            reused=False,
        )

    async def execute_script(
        self,
        session_id,
        *,
        language,
        content=None,
        path=None,
        mode="untrusted",
        timeout=None,
        cancel_event=None,
    ):
        self.exec_calls.append({"language": language, "content": content, "timeout": timeout})
        if cancel_event is not None:
            self.events.append(cancel_event)
        return ExecResult(
            session_id=session_id,
            exit_code=0,
            stdout="out",
            stderr="",
            execution_time=0.1,
            artifacts=[SandboxArtifact(path="/artifacts/a.txt", size=3, modified_at=1.0)],
        )

    async def destroy(self, session_id):
        self.destroyed.append(session_id)
        return True

    def get_workspace(self, session_id):
        return self.workspace

    async def shutdown(self):
        return None


def handler_for(tmp_path: Path, policy: dict | None = None, monkeypatch=None) -> SandboxHandler:
    config = SandboxConfig(image="castrelai/castrel-sandbox:latest")
    workspace = SessionWorkspace(SESSION, tmp_path).ensure()
    manager = FakeManager(workspace, config)
    subject = SandboxHandler(
        lambda: parse_policy(policy or {}),
        config_factory=lambda: config,
    )
    subject._manager = manager  # type: ignore[attr-defined]
    if monkeypatch is not None:
        monkeypatch.setattr(
            sandbox_module,
            "detect",
            lambda _config: SandboxCapability(
                available=True, engine="docker", image=config.image, image_present=True
            ),
        )
    return subject


def call(subject: SandboxHandler, message_type: str, **data):
    return asyncio.run(subject.handle(message_type, {"request_id": "r1", **data}))


def test_status_reports_engine_and_sessions(tmp_path, monkeypatch):
    subject = handler_for(tmp_path, monkeypatch=monkeypatch)

    result = call(subject, "node.sandbox.status")

    assert result["ok"] is True
    assert result["available"] is True
    assert result["engine"] == "docker"
    assert result["image_present"] is True
    assert result["active_sessions"] == 1


def test_bootstrap_rejects_a_caller_image_by_default(tmp_path, monkeypatch):
    subject = handler_for(tmp_path, monkeypatch=monkeypatch)

    result = call(subject, "node.sandbox.bootstrap", session_id=SESSION, image="evil:latest")

    assert result["ok"] is False
    assert result["error_code"] == "policy_denied"
    assert subject.manager.bootstrap_calls == []


def test_bootstrap_allows_a_caller_image_when_policy_enables_it(tmp_path, monkeypatch):
    policy = {
        "sandbox": {"allow_caller_image": True, "allowed_images": ["custom:1"]}
    }
    subject = handler_for(tmp_path, policy, monkeypatch=monkeypatch)

    result = call(subject, "node.sandbox.bootstrap", session_id=SESSION, image="custom:1")

    assert result["ok"] is True
    assert result["container_id"] == "cid-1"


def test_disabled_sandbox_answers_sandbox_unavailable(tmp_path, monkeypatch):
    subject = handler_for(tmp_path, {"sandbox": {"enabled": False}}, monkeypatch=monkeypatch)

    result = call(subject, "node.sandbox.exec", session_id=SESSION, content="print(1)")

    assert result["ok"] is False
    assert result["error_code"] == "sandbox_unavailable"


def test_exec_returns_artifacts_and_truncation_flag(tmp_path, monkeypatch):
    subject = handler_for(tmp_path, monkeypatch=monkeypatch)

    result = call(
        subject, "node.sandbox.exec", session_id=SESSION, content="print('hi')", timeout_ms=5000
    )

    assert result["ok"] is True
    assert result["exit_code"] == 0
    assert result["artifacts"] == [
        {"path": "/artifacts/a.txt", "size": 3, "modified_at": 1000}
    ]
    assert subject.manager.exec_calls[-1]["timeout"] == 5.0


def test_read_file_maps_the_container_path_to_the_workspace(tmp_path, monkeypatch):
    (tmp_path / SESSION / "artifacts").mkdir(parents=True, exist_ok=True)
    (tmp_path / SESSION / "artifacts" / "out.txt").write_text("hello", encoding="utf-8")
    subject = handler_for(tmp_path, monkeypatch=monkeypatch)

    result = call(
        subject, "node.sandbox.read_file", session_id=SESSION, path="/workspace/artifacts/out.txt"
    )

    assert result["ok"] is True
    assert result["size_bytes"] == 5
    assert result["sha256"]
    assert result["eof"] is True


def test_read_file_rejects_a_path_outside_the_workspace(tmp_path, monkeypatch):
    subject = handler_for(tmp_path, monkeypatch=monkeypatch)

    result = call(subject, "node.sandbox.read_file", session_id=SESSION, path="/etc/passwd")

    assert result["ok"] is False
    assert result["error_code"] == "policy_denied"


def test_destroy_calls_the_manager(tmp_path, monkeypatch):
    subject = handler_for(tmp_path, monkeypatch=monkeypatch)

    result = call(subject, "node.sandbox.destroy", session_id=SESSION)

    assert result["ok"] is True
    assert result["destroyed"] is True
    assert subject.manager.destroyed == [SESSION]


def test_cancel_signals_the_running_exec(tmp_path, monkeypatch):
    monkeypatch.setattr(sandbox_module, "CANCEL_GRACE_SECONDS", 0)
    subject = handler_for(tmp_path, monkeypatch=monkeypatch)
    event = asyncio.Event()
    subject._requests["r1"] = (SESSION, event)  # type: ignore[index]

    async def scenario() -> None:
        subject.cancel("r1")
        await asyncio.sleep(0.01)

    asyncio.run(scenario())

    assert event.is_set()
    assert subject.manager.destroyed == [SESSION]
