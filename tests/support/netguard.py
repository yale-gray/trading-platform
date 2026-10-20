"""Exception raised by the offline network guard (SPEC-005 section 2)."""

from __future__ import annotations


class NetworkAccessError(AssertionError):
    """Raised when a test tries to reach the network."""
