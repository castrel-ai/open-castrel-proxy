"""Proxy CLI command surface and daemon file locations."""

from castrel_proxy.cli.proxy_commands import proxy_app
from castrel_proxy.core.daemon import get_proxy_daemon_manager


def test_proxy_cli_exposes_lifecycle_commands():
    names = {command.name for command in proxy_app.registered_commands}
    assert {"pair", "start", "status", "stop", "restart"} <= names


def test_proxy_daemon_uses_separate_files():
    manager = get_proxy_daemon_manager()
    assert manager.pid_file.name == "proxy-node.pid"
    assert manager.log_file.name == "proxy-node.log"


def test_status_renders_received_policy_limits():
    from castrel_proxy.cli.proxy_commands import _policy_status_lines

    lines = _policy_status_lines(
        {
            "policy_version": 5,
            "received_at": 1_700_000_000_000,
            "policy": {
                "max_concurrency": 8,
                "max_timeout_seconds": 600,
                "max_result_bytes": 2_097_152,
                "allowed_connectors": ["*"],
                "exec": {"enabled": True, "mode": "allow_all"},
            },
        }
    )

    assert lines[0].startswith("  Policy: version 5 (received ")
    assert "Max concurrency: 8" in lines[1]
    assert "Max timeout: 600s" in lines[2]
    assert "Max result bytes: 2,097,152" in lines[3]
    assert "mode=allow_all" in lines[4]
    assert "Allowed connectors: *" in lines[5]


def test_status_reports_a_missing_policy_snapshot():
    from castrel_proxy.cli.proxy_commands import _policy_status_lines

    assert _policy_status_lines(None) == ["  Policy: not received yet"]
