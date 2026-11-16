"""Lifecycle commands for the v2 proxy node: logs, config, unpair and uninstall.

Paths are redirected to ``tmp_path`` through ``castrel_proxy.cli.local_state``, the daemon
managers are replaced, and the legacy ``Config`` object is stubbed, so no test can touch the
developer's real ``~/.castrel`` or signal a live node process.
"""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from castrel_proxy.cli import commands as root_commands
from castrel_proxy.cli import local_state, proxy_commands
from castrel_proxy.cli.commands import app as root_app
from castrel_proxy.cli.proxy_commands import proxy_app
from castrel_proxy.core.daemon import DaemonManager

runner = CliRunner()


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """Point every local_state path at ``tmp_path`` and stub both daemon managers."""
    castrel = tmp_path / ".castrel"
    (castrel / "node").mkdir(parents=True)
    paths = {
        "castrel_dir": lambda: castrel,
        "node_dir": lambda: castrel / "node",
        "node_key_file": lambda: castrel / "node" / "keys.json",
        "node_policy_file": lambda: castrel / "node" / "policy.json",
        "node_outbox_file": lambda: castrel / "node" / "outbox.jsonl",
        "bundles_dir": lambda: castrel / "bundles",
        "sandbox_workspace_dir": lambda: castrel / "sandbox",
        "proxy_pid_file": lambda: castrel / "proxy-node.pid",
        "proxy_log_file": lambda: castrel / "proxy-node.log",
        "legacy_pid_file": lambda: castrel / "castrel-proxy.pid",
        "legacy_log_file": lambda: castrel / "castrel-proxy.log",
        "legacy_config_file": lambda: castrel / "config.yaml",
    }
    for name, factory in paths.items():
        monkeypatch.setattr(local_state, name, factory)

    node_daemon = DaemonManager(castrel / "proxy-node.pid", castrel / "proxy-node.log")
    legacy_daemon = DaemonManager(castrel / "castrel-proxy.pid", castrel / "castrel-proxy.log")
    monkeypatch.setattr(proxy_commands, "get_proxy_daemon_manager", lambda: node_daemon)
    monkeypatch.setattr(proxy_commands, "get_daemon_manager", lambda: legacy_daemon)
    # The root ``uninstall`` command imports both helpers directly, so stub them there too.
    monkeypatch.setattr(root_commands, "get_proxy_daemon_manager", lambda: node_daemon)
    monkeypatch.setattr(root_commands, "get_daemon_manager", lambda: legacy_daemon)

    # Legacy Config and the installed binary are the two other things the root command
    # deletes; stub them so a test run can never remove real files outside tmp_path.
    legacy_config = castrel / "config.yaml"
    legacy_config.write_text("server_url: https://castrel.example.com\n", encoding="utf-8")

    class _StubConfig:
        config_file = legacy_config

        def exists(self):
            return legacy_config.exists()

        def delete(self):
            legacy_config.unlink()

    monkeypatch.setattr(root_commands, "get_config", lambda: _StubConfig())
    monkeypatch.setattr(root_commands.shutil, "which", lambda name: None)

    policy_path = castrel / "node" / "policy.json"
    monkeypatch.setattr(proxy_commands, "_policy_state_file", lambda: policy_path)

    return {
        "dir": castrel,
        "paths": paths,
        "node_daemon": node_daemon,
        "legacy_daemon": legacy_daemon,
        "legacy_config": legacy_config,
    }


def _write_keys(path, *, token="secret-node-token"):
    path.write_text(
        json.dumps(
            {
                "node_id": "node-1",
                "token": token,
                "ed25519_private": "ed-private-value",
                "x25519_private": "x-private-value",
                "server_url": "https://castrel.example.com",
            }
        ),
        encoding="utf-8",
    )


def test_proxy_cli_exposes_the_bridge_parity_commands():
    names = {command.name for command in proxy_app.registered_commands}

    assert {
        "pair",
        "start",
        "status",
        "stop",
        "restart",
        "logs",
        "config",
        "unpair",
        "uninstall",
    } <= names


def test_unpair_stops_the_node_then_removes_the_node_state(sandbox):
    castrel = sandbox["dir"]
    keys_file = castrel / "node" / "keys.json"
    policy_file = castrel / "node" / "policy.json"
    outbox = castrel / "node" / "outbox.jsonl"
    log_file = castrel / "proxy-node.log"
    _write_keys(keys_file)
    policy_file.write_text("{}", encoding="utf-8")
    outbox.write_text("", encoding="utf-8")
    log_file.write_text("node log\n", encoding="utf-8")

    calls = []
    daemon = sandbox["node_daemon"]
    daemon.is_running = lambda: True
    daemon.get_pid = lambda: 4242

    def fake_stop():
        calls.append(keys_file.exists())
        return True

    daemon.stop = fake_stop

    result = runner.invoke(proxy_app, ["unpair", "--yes"])

    assert result.exit_code == 0, result.output
    # The daemon was stopped while the keys still existed, so the platform sees a disconnect.
    assert calls == [True]
    assert not keys_file.exists()
    assert not policy_file.exists()
    assert not outbox.exists()
    assert log_file.exists()
    assert "Unpaired" in result.output
    assert "Revoke or delete this node" in result.output


def test_unpair_is_idempotent_when_not_paired(sandbox):
    result = runner.invoke(proxy_app, ["unpair", "--yes"])

    assert result.exit_code == 0
    assert "not paired" in result.output
    assert not (sandbox["dir"] / "node" / "keys.json").exists()


def test_unpair_can_be_cancelled(sandbox):
    keys_file = sandbox["dir"] / "node" / "keys.json"
    _write_keys(keys_file)

    result = runner.invoke(proxy_app, ["unpair"], input="n\n")

    assert result.exit_code == 0
    assert "Cancelled" in result.output
    assert keys_file.exists()


def test_config_never_prints_secrets(sandbox):
    keys_file = sandbox["dir"] / "node" / "keys.json"
    _write_keys(keys_file, token="secret-node-token")

    result = runner.invoke(proxy_app, ["config"])

    assert result.exit_code == 0, result.output
    assert "node-1" in result.output
    assert "castrel.example.com" in result.output
    assert "secret-node-token" not in result.output
    assert "ed-private-value" not in result.output
    assert "x-private-value" not in result.output
    assert "sha256" in result.output


def test_logs_prints_the_requested_tail(sandbox):
    log_file = sandbox["dir"] / "proxy-node.log"
    log_file.write_text("".join(f"line{i}\n" for i in range(1, 11)), encoding="utf-8")

    result = runner.invoke(proxy_app, ["logs", "-n", "3"])

    assert result.exit_code == 0, result.output
    assert "line8" in result.output
    assert "line9" in result.output
    assert "line10" in result.output
    assert "line7" not in result.output


def test_logs_reports_a_missing_log_file(sandbox):
    result = runner.invoke(proxy_app, ["logs"])

    assert result.exit_code == 1
    assert "Log file not found" in result.output
    assert "proxy start" in result.output


def test_logs_survives_undecodable_bytes(sandbox):
    log_file = sandbox["dir"] / "proxy-node.log"
    log_file.write_bytes(b"\xff\xfeok\nlast line\n")

    result = runner.invoke(proxy_app, ["logs", "-n", "2"])

    assert result.exit_code == 0, result.output
    assert "last line" in result.output


def test_proxy_uninstall_removes_state_and_keeps_logs_and_binary(sandbox):
    castrel = sandbox["dir"]
    keys_file = castrel / "node" / "keys.json"
    policy_file = castrel / "node" / "policy.json"
    log_file = castrel / "proxy-node.log"
    bundles = castrel / "bundles"
    session_dir = castrel / "proxy-exec-1"
    _write_keys(keys_file)
    policy_file.write_text("{}", encoding="utf-8")
    log_file.write_text("node log\n", encoding="utf-8")
    bundles.mkdir()
    (bundles / "manifest.json").write_text("{}", encoding="utf-8")
    session_dir.mkdir()

    result = runner.invoke(proxy_app, ["uninstall", "--yes", "--keep-logs", "--keep-binary"])

    assert result.exit_code == 0, result.output
    assert not keys_file.exists()
    assert not policy_file.exists()
    assert not bundles.exists()
    assert not session_dir.exists()
    # Both escape hatches keep their files.
    assert log_file.exists()
    assert "uninstalled successfully" in result.output


def test_proxy_uninstall_mentions_the_content_it_keeps(sandbox):
    result = runner.invoke(proxy_app, ["uninstall", "--yes", "--keep-binary"])

    assert result.exit_code == 0, result.output
    assert "skills/" in result.output
    assert "whitelist.conf" in result.output


def test_uninstall_reports_permission_errors_without_raising(sandbox, monkeypatch):
    castrel = sandbox["dir"]
    bundles = castrel / "bundles"
    bundles.mkdir()
    keys_file = castrel / "node" / "keys.json"
    _write_keys(keys_file)

    original_unlink = type(keys_file).unlink

    def stubborn_unlink(self, *args, **kwargs):
        if self == keys_file:
            raise PermissionError("operation not permitted")
        return original_unlink(self, *args, **kwargs)

    monkeypatch.setattr(type(keys_file), "unlink", stubborn_unlink)

    result = runner.invoke(proxy_app, ["uninstall", "--yes", "--keep-binary"])

    assert result.exit_code == 0, result.output
    assert "could not remove" in result.output
    assert "run manually: rm" in result.output
    assert "uninstalled successfully" in result.output
    # A later step still runs after the failure.
    assert not bundles.exists()


def test_uninstall_removes_node_state_and_keeps_logs_and_binary(sandbox):
    castrel = sandbox["dir"]
    keys_file = castrel / "node" / "keys.json"
    log_file = castrel / "proxy-node.log"
    bundles = castrel / "bundles"
    session_dir = castrel / "proxy-exec-1"
    _write_keys(keys_file)
    log_file.write_text("node log\n", encoding="utf-8")
    bundles.mkdir()
    (bundles / "manifest.json").write_text("{}", encoding="utf-8")
    session_dir.mkdir()

    result = runner.invoke(root_app, ["uninstall", "--yes", "--keep-logs"])

    assert result.exit_code == 0, result.output
    # The root command removes the node identity, so no private key outlives the binary.
    assert not keys_file.exists()
    assert not bundles.exists()
    assert log_file.exists()
    assert "uninstalled successfully" in result.output
