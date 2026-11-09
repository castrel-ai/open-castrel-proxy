"""Stream frame constants and base64 helpers shared by the node egress modules."""

from __future__ import annotations

import base64
import binascii

STREAM_CHUNK_BYTES = 32 * 1024
MAX_STREAM_CHUNK_BYTES = 64 * 1024
STREAM_INITIAL_WINDOW_BYTES = 256 * 1024
STREAM_MAX_STREAMS = 16
STREAM_IDLE_TIMEOUT_SECONDS = 300
STREAM_MAX_BYTES = 64 * 1024 * 1024
STREAM_CONNECT_TIMEOUT_SECONDS = 10.0


def encode_stream_data(payload: bytes) -> str:
    return base64.urlsafe_b64encode(payload).rstrip(b"=").decode("ascii")


def decode_stream_data(value: str) -> bytes:
    try:
        return base64.b64decode(
            value + "=" * (-len(value) % 4), altchars=b"-_", validate=True
        )
    except (ValueError, binascii.Error) as error:
        raise ValueError("stream data is not valid base64url") from error


__all__ = [
    "MAX_STREAM_CHUNK_BYTES",
    "STREAM_CHUNK_BYTES",
    "STREAM_CONNECT_TIMEOUT_SECONDS",
    "STREAM_IDLE_TIMEOUT_SECONDS",
    "STREAM_INITIAL_WINDOW_BYTES",
    "STREAM_MAX_BYTES",
    "STREAM_MAX_STREAMS",
    "decode_stream_data",
    "encode_stream_data",
]
