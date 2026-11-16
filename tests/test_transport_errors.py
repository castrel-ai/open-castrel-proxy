"""Bounded dispatch and failure replies for operation frames (design 4.10, 5.1)."""

import asyncio

import pytest
from castrel_proxy.pairing.keys import NodeKeys
from castrel_proxy.transport.ws import MAX_INFLIGHT_FRAMES, ProxyNodeRuntime


def runtime() -> ProxyNodeRuntime:
    return ProxyNodeRuntime(
        server_url="https://castrel.test",
        keys=NodeKeys(node_id="2001", token="token", ed25519_private="", x25519_private=""),
    )


class _FakeWebSocket:
    def __init__(self):
        self.frames = []

    async def send_json(self, frame):
        self.frames.append(frame)


@pytest.mark.asyncio
async def test_unknown_message_type_answers_with_an_error_frame():
    node = runtime()
    websocket = _FakeWebSocket()

    await node._handle(
        {"type": "node.http.request", "data": {"request_id": "r1", "url": "https://example.test"}},
        websocket,
    )

    frame = websocket.frames[-1]
    assert frame["type"] == "error"
    assert frame["data"]["request_id"] == "r1"
    assert frame["data"]["code"] == "unknown_message_type"


@pytest.mark.asyncio
async def test_handler_exception_answers_with_internal_error(monkeypatch):
    node = runtime()
    websocket = _FakeWebSocket()

    async def boom(_frame, _websocket):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(node, "_handle", boom)

    await node._handle_frame({"type": "node.fs.write", "data": {"request_id": "r2"}}, websocket)

    frame = websocket.frames[-1]
    assert frame["type"] == "node.fs.write.result"
    assert frame["data"]["request_id"] == "r2"
    assert frame["data"]["error_code"] == "internal_error"


@pytest.mark.asyncio
async def test_dispatch_bounds_concurrent_frame_handlers(monkeypatch):
    node = runtime()
    websocket = _FakeWebSocket()
    running = 0
    peak = 0

    async def slow(_frame, _websocket):
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.02)
        running -= 1

    monkeypatch.setattr(node, "_handle", slow)

    for index in range(MAX_INFLIGHT_FRAMES * 2):
        await node._dispatch(
            {"type": "node.fs.list", "data": {"request_id": f"r{index}"}}, websocket
        )
    await asyncio.gather(*list(node._tasks), return_exceptions=True)

    assert peak <= MAX_INFLIGHT_FRAMES


@pytest.mark.asyncio
async def test_disconnect_cancels_inflight_requests():
    node = runtime()
    cancelled = []

    node._inflight["r1"] = lambda: cancelled.append("r1")

    await node._cancel_inflight()

    assert cancelled == ["r1"]
    assert node._inflight == {}
