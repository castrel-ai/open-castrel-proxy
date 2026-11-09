"""Stable device identity for duplicate-pairing detection."""

from castrel_proxy.pairing.device import device_id


def test_device_id_is_stable_and_hashed() -> None:
    first = device_id()
    assert first == device_id()
    assert len(first) == 32
    assert all(character in "0123456789abcdef" for character in first)
