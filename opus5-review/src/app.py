"""Reels-AutoPilot main loop.

Self-healing supervisor that scrapes reels, posts them from one or more
Instagram accounts, recycles/swaps content when it runs out, keeps sessions
warm, watches disk and network, recovers from crashes and shuts down
gracefully. Any single failure is logged and never kills the process.
"""
from __future__ import annotations

import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

import os
import random
import signal
import time
import traceback
from datetime import datetime, timedelta
from types import FrameType
from typing import Dict, List, Optional

import accounts as AccountManager
import auth
import config
import diskspace
import distributor
import helpers as Helper
import netcheck
import notifier
import poster
import purger
import reels
import remover
import shorts
import statefile
from db import Reel, Session
from logger import get_logger, setup_logging

setup_logging()
log = get_logger(__name__)

SHUTDOWN_WAIT_SECONDS = 120
STATE_SAVE_INTERVAL_SECONDS = 30

_shutdown_requested = False

# ---------------------------------------------------------------------------- #
# runtime state                                                                #
# ---------------------------------------------------------------------------- #
Helper.load_all_config()

now = datetime.now()
next_reels_scraper_run_at = now
next_poster_run_at = now                       # legacy single-account timer
next_remover_run_at = now
next_youtube_run_at = now
next_purge_run_at = now + timedelta(hours=10)
next_disk_log_at = now
next_state_save_at = now
api = None                                     # legacy single-account client
pool = AccountManager.AccountPool()
current_status = "starting"
last_post_at: Optional[datetime] = None


def request_shutdown(signum: int, _frame: Optional[FrameType]) -> None:
    """Handle SIGTERM/SIGINT: finish the current upload, then exit."""
    global _shutdown_requested
    _shutdown_requested = True
    log.warning(f"Received signal {signum}. Shutting down gracefully...")


signal.signal(signal.SIGTERM, request_shutdown)
signal.signal(signal.SIGINT, request_shutdown)


def set_status(status: str) -> None:
    """Update the live status shown on the dashboard."""
    global current_status
    current_status = status


def persist_state() -> None:
    """Save timers and live status so a restart resumes where it left off."""
    statefile.update_state(
        status=current_status,
        pid=os.getpid(),
        network=netcheck.status(),
        last_post_at=last_post_at.isoformat() if last_post_at else None,
        next_reels_scraper_run_at=next_reels_scraper_run_at.isoformat(),
        next_poster_run_at=next_poster_run_at.isoformat(),
        next_remover_run_at=next_remover_run_at.isoformat(),
        next_youtube_run_at=next_youtube_run_at.isoformat(),
        next_purge_run_at=next_purge_run_at.isoformat(),
        accounts={runtime.username: runtime.to_dict() for runtime in pool.active()},
    )


def restore_state() -> None:
    """Restore timers saved by a previous run."""
    global next_reels_scraper_run_at, next_poster_run_at, next_remover_run_at
    global next_youtube_run_at, next_purge_run_at, last_post_at

    state = statefile.load_state()
    if not state:
        return

    def parse(key: str, default: datetime) -> datetime:
        """Parse an ISO timestamp from the saved state."""
        value = state.get(key)
        if not value:
            return default
        try:
            return datetime.fromisoformat(str(value))
        except ValueError:
            return default

    reference = datetime.now()
    next_reels_scraper_run_at = parse("next_reels_scraper_run_at", reference)
    next_poster_run_at = parse("next_poster_run_at", reference)
    next_remover_run_at = parse("next_remover_run_at", reference)
    next_youtube_run_at = parse("next_youtube_run_at", reference)
    next_purge_run_at = parse("next_purge_run_at", reference + timedelta(hours=10))
    if state.get("last_post_at"):
        try:
            last_post_at = datetime.fromisoformat(str(state["last_post_at"]))
        except ValueError:
            last_post_at = None
    log.info("Restored runtime state from previous run.")


def ensure_login() -> Optional[object]:
    """Legacy single-account login with retries (kept for compatibility)."""
    global api
    max_retries = int(getattr(config, "MAX_LOGIN_ATTEMPTS", 3))
    for attempt in range(1, max_retries + 1):
        try:
            api = auth.login()
            return api
        except Exception as exc:
            log.error(f"[Login] Attempt {attempt}/{max_retries} failed: {type(exc).__name__}: {exc}")
            if attempt < max_retries:
                time.sleep(int(getattr(config, "LOGIN_RETRY_DELAY_SECONDS", 10)))
    log.error("[Login] All attempts failed. Will retry later.")
    api = None
    return None


def scraping_client() -> Optional[object]:
    """Return a client usable for scraping (first healthy posting account)."""
    runtime = pool.any_logged_in()
    if runtime is not None:
        return runtime.client
    for candidate in pool.active():
        client = candidate.ensure_login()
        if client is not None:
            return client
    return api


def posting_interval_seconds() -> int:
    """Return the configured posting interval with a small random jitter."""
    return (int(config.POSTING_INTERVAL_IN_MIN) * 60) + random.randint(5, 20)


def run_scrape() -> int:
    """Scrape all source accounts and distribute the new reels."""
    client = scraping_client()
    if client is None:
        log.warning("[Scraper] No logged-in account available for scraping.")
        return 0
    set_status("scraping")
    new_reels = reels.main(client)
    usernames = [runtime.username for runtime in pool.active()]
    if usernames:
        distributor.distribute_unassigned(usernames)
    set_status("idle")
    return new_reels


def total_pending() -> int:
    """Count unposted reels whose files still exist on disk."""
    session = Session()
    try:
        unposted = session.query(Reel).filter(Reel.is_posted == False).all()  # noqa: E712
        return sum(1 for reel in unposted if reel.file_path and os.path.exists(reel.file_path))
    finally:
        session.close()


def handle_exhaustion(runtimes: List[AccountManager.AccountRuntime]) -> None:
    """Refill the queue when every posting account has run out of reels.

    Order: fresh scrape -> swap between accounts -> recycle -> full reset.
    """
    usernames = [runtime.username for runtime in runtimes]
    log.info("[Poster] All accounts exhausted. Trying a fresh scrape first...")
    set_status("scraping")
    new_reels = run_scrape()
    if new_reels:
        log.info(f"[Poster] Fresh scrape brought {new_reels} new reels. No recycling needed.")
        for runtime in runtimes:
            runtime.is_recycling = False
        return

    if len(usernames) >= 2:
        swapped = distributor.swap_assignments(usernames)
        if swapped:
            for runtime in runtimes:
                runtime.is_recycling = True
            return

    recycled = distributor.recycle_round_robin()
    if recycled:
        distributor.distribute_unassigned(usernames)
        for runtime in runtimes:
            runtime.is_recycling = True
        log.info(f"[Poster] Recycled {recycled} older reels back into the queue.")
        return

    reset = distributor.reset_all(usernames)
    if reset:
        for runtime in runtimes:
            runtime.is_recycling = True
    else:
        log.error("[Poster] No content available at all. Add source accounts or check downloads folder.")
        notifier.send(
            "\u26a0\ufe0f No content available",
            "There are no reels on disk to post. Check the source accounts and the downloads folder.",
            color=notifier.COLOR_WARNING,
            dedupe_key="no_content",
            throttle_minutes=180,
        )


def post_multi_account(runtimes: List[AccountManager.AccountRuntime]) -> None:
    """Run one posting cycle for each posting account independently."""
    global last_post_at

    ready = [runtime for runtime in runtimes if runtime.next_post_at <= datetime.now()]
    if not ready:
        return

    pending: Dict[str, int] = {
        runtime.username: distributor.pending_count(runtime.username) for runtime in runtimes
    }
    if sum(pending.values()) == 0:
        handle_exhaustion(runtimes)
        pending = {runtime.username: distributor.pending_count(runtime.username) for runtime in runtimes}

    for runtime in ready:
        try:
            client = runtime.ensure_login()
            if client is None:
                log.warning(f"[Poster] @{runtime.username}: not logged in ({runtime.login_status}). Skipping.")
                runtime.next_post_at = datetime.now() + timedelta(
                    seconds=int(getattr(config, "LOGIN_FAILURE_RETRY_SECONDS", 300))
                )
                continue

            if pending.get(runtime.username, 0) == 0:
                log.info(f"[Poster] @{runtime.username}: queue empty, waiting for the next distribution.")
                runtime.next_post_at = datetime.now() + timedelta(seconds=posting_interval_seconds())
                continue

            set_status(f"posting:{runtime.username}")
            runtime.is_uploading = True
            success = poster.post_for_account(client, runtime.username)
            runtime.is_uploading = False
            runtime.next_post_at = datetime.now() + timedelta(seconds=posting_interval_seconds())
            if success:
                runtime.last_post_at = datetime.now()
                last_post_at = runtime.last_post_at
                AccountManager.mark_posted(runtime.username)
                log.info(
                    f"[Poster] @{runtime.username}: next post at {runtime.next_post_at:%H:%M:%S} "
                    f"({'recycled content' if runtime.is_recycling else 'fresh content'})"
                )
            else:
                log.warning(f"[Poster] @{runtime.username}: post failed or skipped. Retrying at {runtime.next_post_at:%H:%M:%S}")
        except Exception as exc:
            runtime.is_uploading = False
            log.error(f"[Poster] @{runtime.username} error: {type(exc).__name__}: {exc}")
            message = str(exc).lower()
            if "login_required" in message or "LoginRequired" in type(exc).__name__ or "challenge" in message:
                runtime.relogin()
            runtime.next_post_at = datetime.now() + timedelta(seconds=120)
        finally:
            set_status("idle")


def post_legacy() -> None:
    """Single-account posting path used when no posting accounts are stored."""
    global api, next_poster_run_at, last_post_at

    if next_poster_run_at > datetime.now():
        return
    try:
        if total_pending() == 0:
            log.info("[Poster] No pending reels. Trying a fresh scrape before recycling...")
            if run_scrape() == 0:
                distributor.recycle_round_robin()

        set_status("posting")
        log.info("[Poster] Attempting to post a reel...")
        success = poster.main(api)
        next_poster_run_at = datetime.now() + timedelta(seconds=posting_interval_seconds())
        if success:
            last_post_at = datetime.now()
            log.info(f"[Poster] Next post at: {next_poster_run_at:%H:%M:%S}")
        else:
            log.warning(f"[Poster] Post failed or skipped. Will retry at: {next_poster_run_at:%H:%M:%S}")
    except Exception as exc:
        log.error(f"[Poster] Error: {type(exc).__name__}: {exc}")
        next_poster_run_at = datetime.now() + timedelta(seconds=120)
        if "login_required" in str(exc).lower() or "LoginRequired" in type(exc).__name__:
            log.warning("[Poster] Session expired. Will re-login on next loop.")
            api = None
    finally:
        set_status("idle")


def startup_self_check() -> None:
    """Validate DB state and reconcile uploads interrupted by a crash."""
    set_status("self-check")
    log.info("[Startup] Running self-check...")

    AccountManager.bootstrap_from_legacy_config()
    runtimes = pool.refresh()

    # Reconcile pending-upload markers left behind by a crash
    for marker in statefile.all_pending_uploads():
        account = str(marker.get("account") or "")
        runtime = next((item for item in runtimes if item.username == account), None)
        client = runtime.ensure_login() if runtime else api
        try:
            poster.recover_pending_upload(client, account)
        except Exception as exc:
            log.warning(f"[Startup] Could not reconcile pending upload for @{account}: {exc}")
            statefile.clear_pending_upload(account)

    # Drop rows whose video file disappeared, and fix NULL flags
    session = Session()
    try:
        missing = 0
        for reel in session.query(Reel).all():
            if reel.is_posted is None:
                reel.is_posted = False
            if reel.swap_phase is None:
                reel.swap_phase = 0
            if not reel.is_posted and reel.file_path and not os.path.exists(reel.file_path):
                missing += 1
        session.commit()
        if missing:
            log.warning(f"[Startup] {missing} unposted reels have no file on disk; they will be skipped.")
    except Exception as exc:
        session.rollback()
        log.warning(f"[Startup] DB self-check issue: {exc}")
    finally:
        session.close()

    usernames = [runtime.username for runtime in runtimes]
    if usernames:
        distributor.distribute_unassigned(usernames)

    diskspace.log_usage()
    netcheck.check()
    log.info(
        f"[Startup] Self-check complete. Posting accounts: {', '.join(usernames) or 'legacy single account'}; "
        f"pending reels: {total_pending()}"
    )
    set_status("idle")


def main() -> None:
    """Run the supervisor loop until a shutdown signal arrives."""
    global api, next_reels_scraper_run_at, next_remover_run_at, next_youtube_run_at
    global next_purge_run_at, next_disk_log_at, next_state_save_at

    log.info("Reels-AutoPilot starting up.")
    restore_state()
    startup_self_check()

    was_offline = False

    while not _shutdown_requested:
        try:
            Helper.load_all_config()
            runtimes = pool.refresh()
            multi_account = bool(runtimes)

            online, transitioned = netcheck.check()
            if not online:
                was_offline = True
                set_status("offline")
                log.warning("[Network] Offline. Skipping Instagram operations for now.")
                persist_state()
                time.sleep(int(getattr(config, "NETWORK_RETRY_SECONDS", 60)))
                continue
            if was_offline and transitioned:
                log.warning("[Network] Back online. Forcing a fresh login for every account.")
                was_offline = False
                api = None
                for runtime in runtimes:
                    runtime.relogin()

            scraper_enabled = config.IS_ENABLED_REELS_SCRAPER == "1"
            poster_enabled = config.IS_ENABLED_AUTO_POSTER == "1"

            if scraper_enabled or poster_enabled:
                if multi_account:
                    for runtime in runtimes:
                        runtime.health_check()
                else:
                    if api is None:
                        api = ensure_login()
                        if api is None:
                            set_status("login-failed")
                            log.error(
                                "[Main] No API connection. Retrying in "
                                f"{getattr(config, 'LOGIN_FAILURE_RETRY_SECONDS', 300)}s..."
                            )
                            persist_state()
                            time.sleep(int(getattr(config, "LOGIN_FAILURE_RETRY_SECONDS", 300)))
                            continue
                    elif not auth.is_session_alive(api):
                        log.warning("[Main] Session health check failed. Re-logging in.")
                        api = ensure_login()

                if scraper_enabled and next_reels_scraper_run_at < datetime.now():
                    try:
                        log.info("[Scraper] Scraping reels...")
                        run_scrape()
                        next_reels_scraper_run_at = datetime.now() + timedelta(
                            seconds=int(config.SCRAPER_INTERVAL_IN_MIN) * 60
                        )
                        log.info(f"[Scraper] Next scrape at: {next_reels_scraper_run_at:%H:%M:%S}")
                    except Exception as exc:
                        log.error(f"[Scraper] Error: {type(exc).__name__}: {exc}")
                        next_reels_scraper_run_at = datetime.now() + timedelta(seconds=60)

                if poster_enabled:
                    if multi_account:
                        post_multi_account(runtimes)
                    else:
                        post_legacy()

            if config.IS_REMOVE_FILES == "1" and next_remover_run_at < datetime.now():
                try:
                    remover.main()
                    next_remover_run_at = datetime.now() + timedelta(
                        seconds=int(config.REMOVE_FILE_AFTER_MINS) * 60
                    )
                except Exception as exc:
                    log.error(f"[Remover] Error: {type(exc).__name__}: {exc}")
                    next_remover_run_at = datetime.now() + timedelta(seconds=300)

            if config.IS_ENABLED_YOUTUBE_SCRAPING == "1" and next_youtube_run_at < datetime.now():
                try:
                    shorts.main()
                    next_youtube_run_at = datetime.now() + timedelta(
                        seconds=int(config.SCRAPER_INTERVAL_IN_MIN) * 60
                    )
                except Exception as exc:
                    log.error(f"[YouTube] Error: {type(exc).__name__}: {exc}")
                    next_youtube_run_at = datetime.now() + timedelta(seconds=300)

            if next_purge_run_at < datetime.now():
                try:
                    log.info("[Purger] Running 10-hour scheduled purge...")
                    purger.purge_unposted()
                    next_purge_run_at = datetime.now() + timedelta(hours=10)
                except Exception as exc:
                    log.error(f"[Purger] Error: {type(exc).__name__}: {exc}")
                    next_purge_run_at = datetime.now() + timedelta(seconds=300)

            if next_disk_log_at < datetime.now():
                diskspace.log_usage()
                diskspace.ensure_free_space()
                next_disk_log_at = datetime.now() + timedelta(
                    seconds=int(getattr(config, "DISK_LOG_INTERVAL_SECONDS", 3600))
                )

            if next_state_save_at < datetime.now():
                persist_state()
                next_state_save_at = datetime.now() + timedelta(seconds=STATE_SAVE_INTERVAL_SECONDS)

        except Exception as exc:
            log.error(f"[Main] Unexpected error: {type(exc).__name__}: {exc}")
            log.debug(traceback.format_exc())
            time.sleep(30)

        time.sleep(int(getattr(config, "MAIN_LOOP_SLEEP_SECONDS", 1)))

    # ---- graceful shutdown ------------------------------------------------ #
    set_status("shutting-down")
    waited = 0
    while poster.is_uploading and waited < SHUTDOWN_WAIT_SECONDS:
        log.info("[Shutdown] Waiting for the current upload to finish...")
        time.sleep(2)
        waited += 2
    persist_state()
    log.info("[Shutdown] State saved. Goodbye.")


if __name__ == "__main__":
    main()
