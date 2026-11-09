"""Node transport: shell execution, heartbeat liveness and version reporting (no network)."""

import asyncio

import pytest
from castrel_proxy.pairing.keys import NodeKeys
from castrel_proxy.security.policy import parse_policy
from castrel_proxy.transport.ws import ProxyNodeRuntime


def runtime() -> ProxyNodeRuntime:
    return ProxyNodeRuntime(
        server_url="https://castrel.test",
        keys=NodeKeys(
            node_id="2001",
            token="token",
            ed25519_private="",
            x25519_private="",
        ),
    )


class _FakeWebSocket:
    def __init__(self):
        self.frames = []

    async def send_json(self, frame):
        self.frames.append(frame)


class _FakeCommandExecutor:
    def __init__(self, session_id, timeout):
        self.session_id = session_id
        self.timeout = timeout

    def cancel(self):
        return False

    async def execute(self, command, cwd=None):
        return type(
            "Result",
            (),
            {"exit_code": 0, "stdout": "hello\\n", "stderr": "", "execution_time": 0.1},
        )()


@pytest.mark.asyncio
async def test_node_exec_runs_with_policy_and_returns_a_result(monkeypatch):
    from castrel_proxy.transport import ws as transport

    node = runtime()
    node.policy = {"exec": {"enabled": True, "mode": "allow_all"}}
    node._policy = parse_policy(node.policy)
    monkeypatch.setattr(transport, "CommandExecutor", _FakeCommandExecutor)
    websocket = _FakeWebSocket()

    await node._exec({"request_id": "r1", "command": "echo hello", "timeout_ms": 5000}, websocket)

    frame = websocket.frames[-1]
    assert frame["type"] == "node.exec.result"
    assert frame["data"]["request_id"] == "r1"
    assert frame["data"]["exit_code"] == 0
    assert frame["data"]["stdout"] == "hello\\n"


@pytest.mark.asyncio
async def test_node_exec_rejects_a_disallowed_command(monkeypatch):
    node = runtime()
    node.policy = {"exec": {"enabled": True, "mode": "allowlist", "allowlist": ["ls"]}}
    node._policy = parse_policy(node.policy)
    websocket = _FakeWebSocket()

    await node._exec({"request_id": "r2", "command": "rm -rf /", "timeout_ms": 5000}, websocket)

    frame = websocket.frames[-1]
    assert frame["data"]["exit_code"] == 126
    assert "not allowed" in frame["data"]["stderr"]


def test_agent_version_is_reported():
    from castrel_proxy.transport.ws import _agent_version

    value = _agent_version()
    assert value and value != "0.0.0"


class _SlowCommandExecutor:
    started: asyncio.Event
    release: asyncio.Event

    def __init__(self, session_id, timeout):
        self.session_id = session_id
        self.timeout = timeout

    def cancel(self):
        return False

    async def execute(self, command, cwd=None):
        type(self).started.set()
        await type(self).release.wait()
        return type(
            "Result",
            (),
            {"exit_code": 0, "stdout": "done\n", "stderr": "", "execution_time": 0.5},
        )()


@pytest.mark.asyncio
async def test_heartbeat_is_answered_while_a_command_runs(monkeypatch):
    """A long command must not starve heartbeat replies; the server closes silent sockets."""
    from castrel_proxy.transport import ws as transport

    class SlowExecutor(_SlowCommandExecutor):
        started = asyncio.Event()
        release = asyncio.Event()

    monkeypatch.setattr(transport, "CommandExecutor", SlowExecutor)
    node = runtime()
    node.policy = {"exec": {"enabled": True, "mode": "allow_all"}}
    node._policy = parse_policy(node.policy)
    websocket = _FakeWebSocket()

    await node._dispatch(
        {
            "type": "node.exec",
            "data": {"request_id": "r1", "command": "du -sh ~", "timeout_ms": 5000},
        },
        websocket,
    )
    await asyncio.wait_for(SlowExecutor.started.wait(), timeout=1)

    await node._dispatch({"type": "heartbeat", "data": {}}, websocket)

    assert [frame["type"] for frame in websocket.frames] == ["heartbeat"]


@pytest.mark.asyncio
async def test_stream_frames_are_handled_inline(monkeypatch):
    node = runtime()
    calls: list[tuple[str, dict]] = []

    def feed(data):
        calls.append(("feed", data))

    def window(data):
        calls.append(("window", data))

    async def close_stream(data):
        calls.append(("close", data))

    node.egress.feed = feed  # type: ignore[method-assign]
    node.egress.window = window  # type: ignore[method-assign]
    node.egress.close_stream = close_stream  # type: ignore[method-assign]
    websocket = _FakeWebSocket()

    await node._dispatch({"type": "stream.data", "data": {"stream_id": "a" * 32}}, websocket)
    await node._dispatch({"type": "stream.window", "data": {"stream_id": "a" * 32}}, websocket)
    await node._dispatch({"type": "stream.close", "data": {"stream_id": "a" * 32}}, websocket)

    assert [kind for kind, _ in calls] == ["feed", "window", "close"]
