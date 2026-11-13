"""The node reports a usable built-in command whitelist to the platform."""

from castrel_proxy.security.whitelist import get_default_whitelist


def test_builtin_command_whitelist_is_available() -> None:
    commands = get_default_whitelist()
    assert len(commands) > 10
    assert "ls" in commands
    assert len(commands) == len(set(commands))
