"""Node-side policy parse and matching."""

from castrel_proxy.security.policy import (
    check_connector,
    check_exec,
    check_tool,
    effective_result_bytes,
    effective_timeout,
    parse_policy,
)


def test_empty_policy_is_permissive_except_exec_whitelist() -> None:
    policy = parse_policy({})
    assert check_connector(policy, "prometheus") is None
    assert check_tool(policy, "prometheus", "query") is None
    assert check_exec(policy, "ls") is None


def test_connector_and_tool_lists() -> None:
    policy = parse_policy(
        {
            "allowed_connectors": ["prometheus"],
            "allowed_tools": {"prometheus": ["query"]},
            "denied_tools": {"prometheus": ["delete_series"]},
        }
    )
    assert check_connector(policy, "elasticsearch")
    assert check_tool(policy, "prometheus", "query") is None
    assert check_tool(policy, "prometheus", "delete_series")


def test_exec_policy_and_limits() -> None:
    policy = parse_policy(
        {
            "max_timeout_seconds": 30,
            "max_result_bytes": 2048,
            "exec": {"mode": "allowlist", "allowlist": ["ls"], "max_output_bytes": 4096},
        }
    )
    assert check_exec(policy, "ls -la") is None
    assert check_exec(policy, "rm -rf /")
    assert effective_timeout(policy, 120, 600) == 30
    assert effective_result_bytes(policy, 1024 * 1024) == 2048
    assert policy.exec.max_output_bytes == 4096


def test_malformed_values_fall_back_safely() -> None:
    policy = parse_policy({"allowed_connectors": "nope", "max_timeout_seconds": -1})
    assert policy.allowed_connectors == ["*"]
    assert policy.max_timeout_seconds is None
