"""Node key and token storage.

The node holds two private keys: Ed25519 for the WebSocket challenge and X25519 for secret
envelopes. Both are raw 32-byte base64url values stored in one file with mode ``0600``.
"""

from __future__ import annotations

import base64
import json
import os
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass
class NodeKeys:
    node_id: str
    token: str
    ed25519_private: str
    x25519_private: str
    server_url: str = ""

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(asdict(self), indent=2, sort_keys=True) + "\n"
        descriptor, temporary = tempfile.mkstemp(prefix="keys-", dir=str(path.parent))
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(payload)
            os.replace(temporary, path)
        except Exception:
            with _suppress():
                os.unlink(temporary)
            raise


class KeysError(Exception):
    """The node key file is missing or malformed."""


def load_keys(path: Path) -> NodeKeys:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise KeysError(f"Node key file is unreadable: {path}") from error
    try:
        return NodeKeys(
            node_id=str(document["node_id"]),
            token=str(document["token"]),
            ed25519_private=str(document["ed25519_private"]),
            x25519_private=str(document["x25519_private"]),
            server_url=str(document.get("server_url") or ""),
        )
    except (KeyError, TypeError) as error:
        raise KeysError("Node key file is missing required fields") from error


def new_private_keys() -> tuple[str, str, str, str]:
    """Generate (ed25519_private, ed25519_public, x25519_private, x25519_public)."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

    def encode(value: bytes) -> str:
        return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")

    ed = Ed25519PrivateKey.generate()
    x = X25519PrivateKey.generate()
    ed_raw = ed.private_bytes(
        serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption()
    )
    x_raw = x.private_bytes(
        serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption()
    )
    ed_public = ed.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    x_public = x.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    return encode(ed_raw), encode(ed_public), encode(x_raw), encode(x_public)


class _suppress:
    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type, exc, traceback) -> bool:
        return exc_type is not None


__all__ = ["KeysError", "NodeKeys", "load_keys", "new_private_keys"]
