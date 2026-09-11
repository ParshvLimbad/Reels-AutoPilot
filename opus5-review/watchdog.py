#!/usr/bin/env python3
"""Reels-AutoPilot watchdog.

Runs as its own systemd service and every 5 minutes checks that:
  * `reels-autopilot.service` is active (restarts it otherwise);
  * the bot actually posted recently while reels were pending;
  * the web dashboard answers on /api/health (restarts `reels-web` otherwise).

Every intervention is logged and announced through the Discord webhook.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from datetime import datetime, timedelta
from typing import Optional

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

import requests as req  # noqa: E402

import config  # noqa: E402
import helpers as Helper  # noqa: E402
import notifier  # noqa: E402
from db import Reel, Session  # noqa: E402
from logger import get_logger, setup_logging  # noqa: E402

setup_logging()
log = get_logger("watchdog")

SYSTEMCTL = "/usr/bin/systemctl"
COMMAND_TIMEOUT_SECONDS = 60
HTTP_TIMEOUT_SECONDS = 10


def _run(command: list) -> subprocess.CompletedProcess:
    """Run a command, never raising on a non-zero exit code."""
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=COMMAND_TIMEOUT_SECONDS,
        check=False,
    )


def service_is_active(service: str) -> bool:
    """Return True when a systemd service is active."""
    try:
        result = _run([SYSTEMCTL, "is-active", "--quiet", service])
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning(f"Could not query {service}: {exc}")
        return True  # do not restart blindly when systemctl is unavailable


def restart_service(service: str, reason: str) -> bool:
    """Restart a systemd service and alert Discord."""
    log.warning(f"Restarting {service}: {reason}")
    try:
        result = _run([SYSTEMCTL, "restart", service])
        ok = result.returncode == 0
    except (OSError, subprocess.SubprocessError) as exc:
        log.error(f"Could not restart {service}: {exc}")
        ok = False
    notifier.alert_watchdog(
        f"restarted {service}" if ok else f"failed to restart {service}",
        reason,
    )
    return ok


def pending_reels() -> int:
    """Count unposted reels that still have a file on disk."""
    session = Session()
    try:
        unposted = session.query(Reel).filter(Reel.is_posted == False).all()  # noqa: E712
        return sum(1 for reel in unposted if reel.file_path and os.path.exists(reel.file_path))
    finally:
        session.close()


def last_post_time() -> Optional[datetime]:
    """Return the timestamp of the most recent successful post."""
    session = Session()
    try:
        reel = (
            session.query(Reel)
            .filter(Reel.is_posted == True)  # noqa: E712
            .filter(Reel.posted_at != None)  # noqa: E711
            .order_by(Reel.posted_at.desc())
            .first()
        )
        return reel.posted_at if reel else None
    finally:
        session.close()


def dashboard_healthy() -> bool:
    """Return True when the dashboard health endpoint responds."""
    url = getattr(config, "WATCHDOG_WEB_URL", "http://127.0.0.1:8080/api/health")
    try:
        response = req.get(url, timeout=HTTP_TIMEOUT_SECONDS)
        return response.status_code == 200
    except req.RequestException as exc:
        log.warning(f"Dashboard health check failed: {exc}")
        return False


def check_once() -> None:
    """Run a single watchdog pass."""
    Helper.load_all_config()

    autopilot = getattr(config, "AUTOPILOT_SERVICE", "reels-autopilot")
    web = getattr(config, "WEB_SERVICE", "reels-web")

    if not service_is_active(autopilot):
        restart_service(autopilot, "service was not running")
        return

    if str(config.IS_ENABLED_AUTO_POSTER) == "1":
        interval_minutes = int(config.POSTING_INTERVAL_IN_MIN) * int(
            getattr(config, "WATCHDOG_POST_STALL_MULTIPLIER", 5)
        )
        threshold = datetime.now() - timedelta(minutes=interval_minutes)
        last_post = last_post_time()
        if pending_reels() > 0 and (last_post is None or last_post < threshold):
            last_text = last_post.strftime("%Y-%m-%d %H:%M:%S") if last_post else "never"
            restart_service(
                autopilot,
                f"no post in the last {interval_minutes} minutes (last post: {last_text}) while reels are pending",
            )
            return

    if not dashboard_healthy():
        restart_service(web, "dashboard did not answer the health check")


def main() -> None:
    """Run the watchdog loop forever."""
    interval = int(getattr(config, "WATCHDOG_INTERVAL_SECONDS", 300))
    log.info(f"Watchdog started (every {interval}s).")
    if "--once" in sys.argv:
        check_once()
        return
    while True:
        try:
            check_once()
        except Exception as exc:  # the watchdog must never die
            log.error(f"Watchdog error: {type(exc).__name__}: {exc}")
        time.sleep(interval)


if __name__ == "__main__":
    main()
