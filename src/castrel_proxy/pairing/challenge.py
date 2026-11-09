"""Ed25519 challenge signing for the node WebSocket handshake."""

from __future__ import annotations

import base64

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
)


def _b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def auth_message(node_id: str, nonce: str, epoch: int) -> bytes:
    """The exact bytes signed: node_id || nonce || epoch."""
    return f"{node_id}{nonce}{epoch}".encode("utf-8")


def sign_challenge(private_key_b64: str, node_id: str, nonce: str, epoch: int) -> str:
    private = Ed25519PrivateKey.from_private_bytes(_b64decode(private_key_b64))
    return _b64encode(private.sign(auth_message(node_id, nonce, epoch)))


def private_key_b64(key: Ed25519PrivateKey) -> str:
    raw = key.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
    return _b64encode(raw)


def public_key_b64(key: Ed25519PrivateKey) -> str:
    return _b64encode(key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw))
