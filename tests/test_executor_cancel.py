"""Command cancellation terminates the whole process group."""

import asyncio

import pytest

from castrel_proxy.core.executor import CommandExecutor


@pytest.mark.asyncio
async def test_cancel_kills_a_running_command(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    marker = tmp_path / "survived"
    executor = CommandExecutor(session_id="cancel-test", working_dir=str(tmp_path), timeout=30)

    task = asyncio.create_task(executor.execute(f"sleep 1; touch {marker}"))
    await asyncio.sleep(0.2)
    assert executor.cancel() is True

    result = await asyncio.wait_for(task, timeout=5)
    assert result.exit_code != 0
    await asyncio.sleep(1.2)
    assert not marker.exists()


@pytest.mark.asyncio
async def test_cancel_before_start_stops_the_command(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    marker = tmp_path / "survived"
    executor = CommandExecutor(session_id="cancel-early", working_dir=str(tmp_path), timeout=30)
    assert executor.cancel() is False

    result = await asyncio.wait_for(executor.execute(f"sleep 1; touch {marker}"), timeout=5)
    assert result.exit_code != 0
    await asyncio.sleep(1.2)
    assert not marker.exists()
