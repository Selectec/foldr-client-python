"""Opt-in application policy for storage requests; not a DNS or network firewall."""

from collections.abc import Callable

import httpx

from .errors import ProtocolError


def check_destination(url: str, policy: Callable[[httpx.URL], bool] | None) -> None:
    """Require an explicit approval without exposing signed URLs in errors."""
    if policy is None:
        return
    try:
        approved = policy(httpx.URL(url))
    except Exception:
        raise ProtocolError("Storage destination policy failed") from None
    if approved is not True:
        raise ProtocolError("Storage destination rejected by policy")
