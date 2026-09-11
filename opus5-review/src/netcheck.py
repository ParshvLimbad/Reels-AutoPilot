"""Network connectivity checks and online/offline transition tracking."""
from __future__ import annotations

import socket
from datetime import datetime
from typing import Optional, Tuple

import config
from logger import get_logger

log = get_logger(__name__)

_last_state: Optional[bool] = None
_last_change: Optional[datetime] = None


def is_online() -> bool:
    """Return True when at least one well-known host is reachable."""
    timeout = float(getattr(config, "NETWORK_CHECK_TIMEOUT_SECONDS", 5))
    for host, port in getattr(config, "NETWORK_CHECK_HOSTS", [("1.1.1.1", 53)]):
        try:
            with socket.create_connection((host, int(port)), timeout=timeout):
                return True
        except OSError:
            continue
    return False


def check(log_transitions: bool = True) -> Tuple[bool, bool]:
    """Check connectivity.

    Returns a tuple of (online, transitioned) where `transitioned` is True when
    the state changed since the previous check (e.g. offline -> online).
    """
    global _last_state, _last_change
    online = is_online()
    transitioned = _last_state is not None and online != _last_state
    if _last_state is None or transitioned:
        _last_change = datetime.now()
        if log_transitions:
            if _last_state is None:
                log.info(f"Network status: {'online' if online else 'offline'}")
            else:
                log.warning(
                    f"Network status changed: {'offline' if not online else 'online'}"
                )
    _last_state = online
    return online, transitioned


def last_change() -> Optional[datetime]:
    """Return the timestamp of the last online/offline transition."""
    return _last_change


def status() -> str:
    """Return a human readable network status for the dashboard."""
    if _last_state is None:
        return "unknown"
    return "online" if _last_state else "offline"
