"""Node-side network egress: SOCKS-free TCP streams driven by the platform."""

from .manager import EgressManager
from .stream import EgressStream

__all__ = ["EgressManager", "EgressStream"]
