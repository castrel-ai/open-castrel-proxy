"""Versioned frame envelope for the proxy WebSocket protocol."""

from __future__ import annotations

import json
import time
import uuid
from typing import Any

PROTOCOL_VERSION = 2
SUPPORTED_PROTOCOL_VERSIONS = frozenset({1, 2})
EGRESS_CAPABILITY = "egress.tcp.v1"
MAX_FRAME_BYTES = 8 * 1024 * 1024


class ProtocolError(Exception):
    """A malformed or unsupported protocol frame."""

    def __init__(self, message: str, code: str = "protocol_error") -> None:
        super().__init__(message)
        self.code = code


def make_envelope(
    message_type: str,
    data: dict[str, Any],
    *,
    node_id: str | None = None,
    epoch: int | None = None,
    seq: int | None = None,
) -> dict[str, Any]:
    return {
        "v": PROTOCOL_VERSION,
        "id": uuid.uuid4().hex,
        "type": message_type,
        "ts": int(time.time() * 1000),
        "seq": seq,
        "node_id": node_id,
        "epoch": epoch,
        "data": dict(data),
    }


def parse_envelope(raw: str | bytes) -> dict[str, Any]:
    payload = raw.encode("utf-8") if isinstance(raw, str) else raw
    if len(payload) > MAX_FRAME_BYTES:
        raise ProtocolError("frame exceeds the protocol size limit", code="frame_too_large")
    try:
        decoded = json.loads(payload)
    except (UnicodeDecodeError, ValueError) as error:
        raise ProtocolError("frame is not valid JSON", code="invalid_json") from error
    if not isinstance(decoded, dict):
        raise ProtocolError("frame must be a JSON object", code="invalid_frame")
    if decoded.get("v") not in SUPPORTED_PROTOCOL_VERSIONS:
        raise ProtocolError("unsupported protocol version", code="protocol_unsupported")
    if not isinstance(decoded.get("type"), str) or not decoded["type"]:
        raise ProtocolError("frame type must not be empty", code="invalid_frame")
    if not isinstance(decoded.get("data", {}), dict):
        raise ProtocolError("frame data must be an object", code="invalid_frame")
    return decoded
