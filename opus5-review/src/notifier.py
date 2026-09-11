"""Discord webhook notifications with de-duplication and throttling.

All alerts are optional: when no webhook URL is configured every call is a
silent no-op so the bot keeps running exactly as before.
"""
from __future__ import annotations

import threading
from datetime import datetime, timedelta
from typing import Dict, Optional

import requests as req

import config
from logger import get_logger

log = get_logger(__name__)

DEFAULT_THROTTLE_MINUTES = 60
REQUEST_TIMEOUT_SECONDS = 10

COLOR_SUCCESS = 5763719
COLOR_INFO = 3447003
COLOR_WARNING = 16426522
COLOR_ERROR = 15548997

_last_sent: Dict[str, datetime] = {}
_lock = threading.Lock()


def _webhook_url() -> str:
    """Read the webhook URL from the DB config, falling back to config.py."""
    try:
        from db import Session, Config  # imported lazily to avoid import cycles

        session = Session()
        try:
            row = session.query(Config).filter_by(key="DISCORD_WEBHOOK_URL").first()
            if row and row.value and row.value.strip():
                return row.value.strip()
        finally:
            session.close()
    except Exception as exc:  # pragma: no cover - DB may not exist yet
        log.debug(f"Could not read webhook from DB: {exc}")
    return (getattr(config, "DISCORD_WEBHOOK_URL", "") or "").strip()


def _should_send(dedupe_key: Optional[str], throttle_minutes: int) -> bool:
    """Return True when a de-duplicated alert may be sent again."""
    if not dedupe_key:
        return True
    now = datetime.now()
    with _lock:
        last = _last_sent.get(dedupe_key)
        if last and now - last < timedelta(minutes=throttle_minutes):
            return False
        _last_sent[dedupe_key] = now
    return True


def reset_dedupe(dedupe_key: str) -> None:
    """Forget a de-duplication key so its alert can fire again immediately."""
    with _lock:
        _last_sent.pop(dedupe_key, None)


def send(
    title: str,
    description: str = "",
    color: int = COLOR_INFO,
    fields: Optional[list] = None,
    dedupe_key: Optional[str] = None,
    throttle_minutes: int = DEFAULT_THROTTLE_MINUTES,
) -> bool:
    """Send a Discord embed. Returns True when the message was delivered."""
    url = _webhook_url()
    if not url:
        return False
    if not _should_send(dedupe_key, throttle_minutes):
        log.debug(f"Alert '{title}' suppressed by throttle key {dedupe_key}")
        return False

    payload = {
        "embeds": [
            {
                "title": title[:250],
                "description": description[:1900],
                "color": color,
                "fields": fields or [],
                "footer": {"text": "Reels AutoPilot"},
                "timestamp": datetime.utcnow().isoformat(),
            }
        ]
    }
    try:
        req.post(url, json=payload, timeout=REQUEST_TIMEOUT_SECONDS)
        return True
    except req.RequestException as exc:
        log.warning(f"Discord notification failed: {exc}")
        return False


def alert_login_failed(account: str) -> bool:
    """Alert that every login method failed for an account."""
    return send(
        "\u26a0\ufe0f LOGIN FAILED \u2014 manual intervention needed",
        f"All login methods failed for **@{account}**. Update the password or paste a fresh SESSIONID in the dashboard.",
        color=COLOR_ERROR,
        dedupe_key=f"login_failed:{account}",
        throttle_minutes=30,
    )


def alert_recycling(account: str) -> bool:
    """Alert (once per window) that the bot started recycling old content."""
    return send(
        "\u2139\ufe0f No new content found. Recycling older reels.",
        f"Recycling older reels from **@{account}** because no fresh content was available.",
        color=COLOR_INFO,
        dedupe_key="recycling",
        throttle_minutes=180,
    )


def alert_swap(phase: int) -> bool:
    """Alert that reels were swapped between posting accounts."""
    return send(
        "\U0001f504 Swapping reels between accounts.",
        f"All posting accounts exhausted their queue. Phase {phase} starting.",
        color=COLOR_INFO,
        dedupe_key=f"swap:{phase}",
        throttle_minutes=180,
    )


def alert_scrape_failure(account: str, failures: int, error: str) -> bool:
    """Alert that a source account keeps failing to scrape."""
    return send(
        "\u26a0\ufe0f Source account failing",
        f"**@{account}** failed {failures} consecutive scrape attempts.\nLast error: `{error[:300]}`",
        color=COLOR_WARNING,
        dedupe_key=f"scrape_fail:{account}",
        throttle_minutes=120,
    )


def alert_disk_low(free_mb: float) -> bool:
    """Alert that the SD card is running out of space."""
    return send(
        "\U0001f4be Low disk space",
        f"Only {free_mb:.0f} MB free. Purging posted reel files.",
        color=COLOR_WARNING,
        dedupe_key="disk_low",
        throttle_minutes=120,
    )


def alert_watchdog(action: str, detail: str = "") -> bool:
    """Alert that the watchdog intervened."""
    return send(
        f"\U0001f415 Watchdog: {action}",
        detail,
        color=COLOR_WARNING,
        dedupe_key=f"watchdog:{action}",
        throttle_minutes=15,
    )


def notify_posted(reel_code: str, source_account: str, posted_by: str, caption: str = "") -> bool:
    """Notify a successful post (kept from the original behaviour)."""
    reel_url = f"https://www.instagram.com/reel/{reel_code}/"
    fields = [
        {"name": "Reel", "value": f"[{reel_code}]({reel_url})", "inline": True},
        {"name": "Source", "value": f"@{source_account}", "inline": True},
        {"name": "Posted by", "value": f"@{posted_by}", "inline": True},
    ]
    if caption and caption.strip():
        fields.append({"name": "Caption", "value": caption[:200], "inline": False})
    return send(
        "\u2705 Reel Posted!",
        f"**@{source_account}** \u2192 posted by **@{posted_by}**",
        color=COLOR_SUCCESS,
        fields=fields,
    )
