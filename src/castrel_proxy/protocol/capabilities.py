"""Capability strings, result error codes and locale reporting for protocol v2.

``egress.tcp.v1`` is the capability the node has always reported; the other four are added as
their handlers land. The platform tolerates capability strings it does not know, so a newer
node may report more than an older platform understands.
"""

from __future__ import annotations

import os

from .envelope import EGRESS_CAPABILITY

FILES_CAPABILITY = "files.v1"
SANDBOX_CAPABILITY = "sandbox.docker.v1"
MCP_CAPABILITY = "mcp.v1"
SKILLS_CAPABILITY = "skills.v1"

# Capabilities whose handlers are implemented. Each handler phase appends its own entry here;
# reporting a capability before its handler exists would make the platform send frames the node
# cannot answer.
ENABLED_OPERATIONS: tuple[str, ...] = (FILES_CAPABILITY, SANDBOX_CAPABILITY)

DEFAULT_LOCALE = "en-US"

# Locale suffixes the platform ships skill bundles for.
SUPPORTED_LOCALES = frozenset({"en-US", "zh-CN"})


def build_capabilities() -> list[str]:
    """Capabilities to report in ``auth.proof``: egress first, then operation handlers."""
    return [EGRESS_CAPABILITY, *ENABLED_OPERATIONS]


class ErrorCode:
    """Error codes carried in ``<type>.result.error_code``.

    Mirrors ``castrel_platform.proxies.protocol.ProxyErrorCode``; keep both lists in sync.
    """

    UNSUPPORTED_CAPABILITY = "unsupported_capability"
    POLICY_DENIED = "policy_denied"
    INVALID_REQUEST = "invalid_request"
    NOT_FOUND = "not_found"
    NOT_A_FILE = "not_a_file"
    IS_A_DIRECTORY = "is_a_directory"
    PATH_DENIED = "path_denied"
    TOO_LARGE = "too_large"
    BAD_ENCODING = "bad_encoding"
    IO_ERROR = "io_error"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    SANDBOX_UNAVAILABLE = "sandbox_unavailable"
    SANDBOX_LIMIT = "sandbox_limit"
    SANDBOX_IMAGE_MISSING = "sandbox_image_missing"
    MCP_UNAVAILABLE = "mcp_unavailable"
    SKILL_INVALID = "skill_invalid"
    UNKNOWN_MESSAGE_TYPE = "unknown_message_type"
    INTERNAL_ERROR = "internal_error"


def detect_locale() -> str:
    """Locale the platform uses to pick a skill bundle.

    ``CASTREL_LOCALE`` wins, then ``LC_ALL`` / ``LANG``; anything the platform has no bundle
    for falls back to ``en-US`` so the handshake never carries an unusable tag.
    """
    raw = os.environ.get("CASTREL_LOCALE") or os.environ.get("LC_ALL") or os.environ.get("LANG")
    tag = _normalize_locale(raw or "")
    return tag if tag in SUPPORTED_LOCALES else DEFAULT_LOCALE


def _normalize_locale(value: str) -> str:
    """Turn ``zh_CN.UTF-8`` / ``en_us`` into a BCP 47 tag such as ``zh-CN``."""
    cleaned = value.strip().split(".")[0].split("@")[0]
    if not cleaned:
        return ""
    parts = cleaned.replace("_", "-").split("-")
    language = parts[0].lower()
    if not language.isalpha() or len(language) != 2:
        return ""
    if len(parts) == 1:
        return language
    region = parts[1].upper()
    if not region.isalpha() or len(region) != 2:
        return language
    return f"{language}-{region}"


# Platform limits the node applies when the server omits them (older platforms).
DEFAULT_MAX_FILE_BYTES = 10 * 1024 * 1024
DEFAULT_MAX_FILE_READ_BYTES = 256 * 1024
DEFAULT_MAX_SANDBOX_OUTPUT_BYTES = 256 * 1024
DEFAULT_CHUNK_BYTES = 32 * 1024

__all__ = [
    "DEFAULT_CHUNK_BYTES",
    "DEFAULT_LOCALE",
    "DEFAULT_MAX_FILE_BYTES",
    "DEFAULT_MAX_FILE_READ_BYTES",
    "DEFAULT_MAX_SANDBOX_OUTPUT_BYTES",
    "EGRESS_CAPABILITY",
    "ENABLED_OPERATIONS",
    "ErrorCode",
    "FILES_CAPABILITY",
    "MCP_CAPABILITY",
    "SANDBOX_CAPABILITY",
    "SKILLS_CAPABILITY",
    "SUPPORTED_LOCALES",
    "build_capabilities",
    "detect_locale",
]
